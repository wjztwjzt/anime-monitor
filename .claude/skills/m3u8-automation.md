---
name: m3u8-automation
description: 多频道动漫/影视监控自动化流水线：配置驱动多频道→搜索比对→Telethon频道核验→解析m3u8→下载→上传对应频道(正确宽高比)→删除本地文件。可复用于其他动漫资源监控项目。
---

# 多频道监控全自动流水线 Skill (v3.1)

## 一、整体架构

```
┌──────────────────────────────────────────────────────────────┐
│  auto_run.py / config_monitor.py --loop                       │
│  (定时器，默认30分钟，支持 --channel 频道过滤)                    │
├──────────────────────────────────────────────────────────────┤
│  ① 监控层: jiankong/config_monitor.py                         │
│     └─ 读取 config.yaml monitor.channels 配置                  │
│     └─ 遍历频道→剧集: search_keyword 调用 provider_compare     │
│     └─ 与 SQLite show_monitor_state 比对集数                   │
│     └─ 集数上涨 → Telethon 扫频道核实（#标签+「第N集」）        │
│     └─ 有变更 → 发送 Telegram Bot 通知（按频道分组）            │
├──────────────────────────────────────────────────────────────┤
│  ② 核验层: jiankong/channel_episode_telethon.py               │
│     └─ 拉取频道历史消息，按 #tag + 集数文案推断已发最大集       │
│     └─ 双重确认：首次拉取 + 二次确认，避免虚高触发流水线        │
├──────────────────────────────────────────────────────────────┤
│  ③ 解析层: jiankong/pipeline.py → m3u8_resolve.py             │
│     └─ /api/search → 匹配 source_name → 读取 episodes          │
│     └─ 输出: {集数: m3u8_url} → 写入 episode_jobs 表           │
├──────────────────────────────────────────────────────────────┤
│  ④ 下载层: app/download_worker.py                             │
│     └─ 从 data/urls/<channel>/<show>.txt 加载手动 URL          │
│     └─ N_m3u8DL-RE 子进程 (m3u8 → mp4)                        │
│     └─ 构建 show_id → (telegram_chat_id, cover) 映射表        │
├──────────────────────────────────────────────────────────────┤
│  ⑤ 上传层: telegram_manager.py                                │
│     └─ ffprobe 提取宽/高/时长 → DocumentAttributeVideo         │
│     └─ 上传到对应频道的 chat_id，使用频道封面                   │
│     └─ FastTelethon 并行分片 (parallel=true 时启用)            │
│     └─ 上传完成 → 标记 upload_status → 删除本地 mp4            │
└──────────────────────────────────────────────────────────────┘
```

## 二、核心模块

### 2.1 配置驱动监控 (`jiankong/config_monitor.py`)

通过 config.yaml 定义频道和剧集，搜索 API 检测更新。

```python
run_monitor(config_path=None, *, channel_filter=None) -> int
# ① 加载 config.yaml monitor.channels
# ② 遍历 channel → show: search_keyword → provider_compare.search_all_providers()
# ③ 与 SQLite show_monitor_state.last_episode_count 比对
# ④ 集数上涨 → Telethon 扫频道核实（避免虚高触发管道）
# ⑤ 有变更 → 记录 changes 列表 → 发送 Telegram 通知 → 触发 pipeline

main() -> int
# CLI: --loop, --interval, --channel, --once
```

**变更字典格式**：
```python
{
    "show_id": str, "channel_id": str, "telegram_chat_id": str,
    "search_keyword": str, "title": str, "display_name": str,
    "old_total": int, "new_total": int,
    "source_name": str, "source_id": str, "vod_id": str,
}
```

### 2.2 频道集数核验 (`jiankong/channel_episode_telethon.py`)

用 Telethon 拉取目标频道历史消息，按 `#标签` +「第N集」文案推断频道已发布的最大集数。

```python
scan_channel_max_episode_blocking(*, telegram_chat_id, hashtag, message_limit=400) -> int
# 同步封装，内部 asyncio.run

async scan_channel_max_episode(*, telegram_chat_id, hashtag, message_limit=400) -> int
# 拉取最近 N 条消息 → 正则「第X集/第X话/Episode #X」→ 取最大集数
```

**核实流程**（`detect_show_changes` 内）：
1. 站点集数上涨 → 检查库中是否有频道扫描记录
2. 无记录 → 首次 Telethon 拉取，写入 `channel_latest_ep` + `channel_ep_checked_at`
3. 站点 ≤ 频道记录 → 同步基线（更新 `last_episode_count`），跳过流水线
4. 站点 > 频道记录 → 二次 Telethon 拉取确认
5. 二次确认仍 > → 触发流水线；否则同步基线跳过

### 2.3 流水线 (`jiankong/pipeline.py`)

```python
run_pipeline_for_changes(changes: list[dict]) -> None
# 变更字典直接包含 show_id、channel_id、telegram_chat_id
# 写入 episode_jobs 时携带 channel_id
# 调用 run_download_upload(upload_enabled_override=True)
```

环境变量控制：
```bash
PIPELINE_ENABLED=1              # 启用自动下载上传
PIPELINE_SKIP_XIAZAI=1          # 只解析 m3u8，不下载上传
```

### 2.4 多供应商比较 (`jiankong/provider_compare.py`)

同一关键词在不同供应商有不同结果 → 按 display_name/title 分组 → 搜索所有供应商 → 取最高集数。

```python
search_all_providers(keyword: str, base_url: str) -> list[dict]
# 返回按集数降序排列的结果列表
```

### 2.5 下载模块 (`app/download_worker.py`) ← 多频道上传 + 频道封面

```python
seed_shows(conn, cfg, root) -> None
# 从 config.monitor.channels[*].shows 读取剧集配置
# 从 data/urls/<channel>/<show>.txt 加载手动 URL（一行一个，行号=集数）

download_m3u8_re(url, out_path, dl_cfg, *, working_dir, clean_proxy)
# → N_m3u8DL-RE 子进程下载，完成后校验文件存在

run_download_upload(upload_enabled_override=None)
# → 构建 show_id → (telegram_chat_id, cover_path) 映射
# → 按频道 cover 上传，回退全局 paths.cover
# → 遍历 show_profiles → 每剧解析上传目标 → 下载 + 上传到对应频道

async upload_via_telegram_manager(file_path, caption, *,
    target, thumb_path, progress_callback) -> bool
# → 支持 target 参数，上传到指定 chat_id
```

**URL txt 文件格式**（`data/urls/<channel>/<show>.txt`）：
```
# 一行一个 m3u8 URL，第 N 行 = 第 N 集
https://example.com/ep1.m3u8
https://example.com/ep2.m3u8
              ← 空行表示该集无手动 URL
https://example.com/ep4.m3u8
```

### 2.6 Telegram 管理器 (`telegram_manager.py`)

```python
class TelegramManager:
    async login(force_relogin=False)
    async upload_video(file_path, caption, *, thumb_path, target, progress_callback)
    async disconnect()
```

**上传视频格式**：ffprobe → width/height/duration → DocumentAttributeVideo → 视频气泡（非文件附件）。

**双路径上传**：
| 路径 | 条件 | 特点 |
|------|------|------|
| FastTelethon 并行 | `fastupload.parallel: true` | 多连接分片（大文件加速） |
| Telethon 内置 | 默认 | 单连接稳定，仍带视频属性 |

### 2.7 CLI 管理工具 (`manage_channels.py`)

直接读写 config.yaml monitor 段 + txt 文件，支持频道/剧集/URL/状态管理：

```bash
# 频道管理
python manage_channels.py list-channels
python manage_channels.py add-channel --id anime --name "动漫频道" --chat-id "-100xxx" --cover "covers/anime.jpeg"
python manage_channels.py delete-channel --id anime

# 剧集管理
python manage_channels.py list-shows --channel anime
python manage_channels.py add-show --channel anime --id test --search "关键词" --topic "名称" --urls-file "data/urls/anime/test.txt"
python manage_channels.py edit-show --id test --search "新关键词"
python manage_channels.py delete-show --id test

# 手动 URL 管理（读写 txt 文件）
python manage_channels.py add-url --show-id mushenji --ep 69 --url "https://..."
python manage_channels.py remove-url --show-id mushenji --ep 69
python manage_channels.py list-urls --show-id mushenji

# 监控状态
python manage_channels.py show-state --show-id mushenji
python manage_channels.py reset-state --show-id mushenji --episode-count 0
```

### 2.8 SQLite 数据模型 (`app/store.py`) ← v3.1

```sql
-- 频道表
channels (channel_id TEXT PK, channel_name TEXT, telegram_chat_id TEXT,
          channel_type TEXT DEFAULT '', cover TEXT DEFAULT '', sort_order INT)

-- 剧集配置
show_profiles (show_id TEXT PK, topic_name TEXT, anime_prefix TEXT,
               caption_file TEXT, download_dir TEXT, urls_file TEXT DEFAULT '',
               sort_order INT)

-- 待下载/上传任务
episode_jobs (show_id TEXT, episode INT, url TEXT,
              download_status TEXT, upload_status TEXT, updated_at TEXT,
              channel_id TEXT DEFAULT '',
              PRIMARY KEY (show_id, episode))

-- 监控状态（搜索比对基线）
show_monitor_state (show_id TEXT PK, channel_id TEXT, search_keyword TEXT,
                    last_episode_count INT, source_name TEXT, source_id TEXT,
                    vod_id TEXT, title TEXT, updated_at TEXT,
                    channel_latest_ep INT DEFAULT 0, channel_ep_checked_at TEXT DEFAULT '')
```

**状态机**：
```
download_status: '' → downloading → downloaded / download_failed
upload_status:   '' → uploaded / upload_failed
```

## 三、配置清单

### config.yaml 完整结构 (v3.0)

```yaml
database:
  path: data/app_state.sqlite

proxy:
  download:
    enabled: false
  upload:
    enabled: true
    socks5: { host: 127.0.0.1, port: 10809, username: '', password: '' }

paths:
  cover: Telethon-FastUpload/cover.jpeg          # 全局默认封面（频道未指定时使用）

telegram:
  api_id: 'YOUR_API_ID'
  api_hash: 'YOUR_API_HASH'
  target: '-100xxxxxxxxx'
  session_path: Telethon-FastUpload/session.session
  phone: '+1xxxxxxxxx'

fastupload:
  enabled: true
  parallel: false
  connections: 16

m3u8dl_re:
  executable: N_m3u8DL-RE
  auto_select: true
  log_level: ERROR

runtime:
  upload_enabled: true
  upload_retries: 5

# ===== v3.0 配置驱动多频道监控 =====
monitor:
  notify_bot_token: "YOUR_BOT_TOKEN"
  notify_chat_id: "YOUR_CHAT_ID"
  base_url: "https://tv.658877.xyz"
  interval: 1800

  telegram_channel_verify:
    enabled: true
    scan_message_limit: 400

  channels:
    - id: anime
      name: "动漫频道"
      telegram_chat_id: "-1003966238914"
      cover: "covers/anime.jpeg"               # 频道封面图（可选，回退 paths.cover）
      sort_order: 1
      shows:
        - id: mushenji
          search_keyword: "牧神记"
          topic_name: "牧神记"
          anime_prefix: "牧神记第"
          caption_file: "Telethon-FastUpload/牧神记.txt"
          download_dir: "xiazai/downloads/mushenji"
          urls_file: "data/urls/anime/牧神记.txt"   # 手动 URL txt 文件（一行一集）
          sort_order: 1

    - id: american_tv
      name: "美剧频道"
      telegram_chat_id: "-100xxx"
      cover: "covers/american.jpeg"
      sort_order: 2
      shows: []
```

## 四、运行方式

```bash
# 循环监控
python auto_run.py                         # 默认 30 分钟
python auto_run.py --interval 600          # 每 10 分钟
python auto_run.py --once                  # 单次检查
python auto_run.py --channel anime         # 仅检查指定频道

# 直接调用
python jiankong/config_monitor.py --once
python jiankong/config_monitor.py --loop --interval 1800

# 仅下载上传（不监控）
python run.py --upload
python run.py --download
```

## 五、服务端部署

### systemd
```ini
[Unit]
Description=Multi-Channel Monitor Pipeline v3.0
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/anime-monitor
ExecStart=/usr/bin/python3 auto_run.py --interval 1800
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1
Environment=PIPELINE_ENABLED=1

[Install]
WantedBy=multi-user.target
```

### Docker
```dockerfile
FROM python:3.11-slim
RUN apt update && apt install -y ffmpeg ffprobe wget
COPY xiazai/N_m3u8DL-RE /app/xiazai/N_m3u8DL-RE
COPY . .
RUN pip install -r requirements.txt
CMD ["python", "auto_run.py", "--interval", "1800"]
```

### 服务端 checklist
1. N_m3u8DL-RE 二进制放 `xiazai/` 目录
2. ffmpeg + ffprobe 必须安装
3. `python telegram_manager.py login` 生成 session，scp 到服务器
4. config.yaml 手动部署（含 monitor 段），不提交 git
5. 国内服务器需 SOCKS5 代理
6. URL txt 文件放在 `data/urls/<channel>/` 目录，一行一个 m3u8 链接

## 六、复用到新项目的步骤

1. **配置频道**：编辑 config.yaml monitor.channels，定义频道和剧集
2. **管理剧集**：`python manage_channels.py add-show --channel xxx --id xxx --search "关键词" --urls-file "data/urls/xxx.txt"`
3. **添加 URL**：`python manage_channels.py add-url --show-id xxx --ep 1 --url "https://..."`  或直接编辑 txt 文件
4. **生成 session**：`python telegram_manager.py login`
5. **测试**：`python auto_run.py --once` → 确认后 `python auto_run.py`

## 七、调试

```bash
LOG_LEVEL=DEBUG python auto_run.py --once
PIPELINE_SKIP_XIAZAI=1 python auto_run.py --once     # 只解析不下载
TELEGRAM_DISABLE_FAST_UPLOAD=1 python auto_run.py --once
```

## 八、版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-05-11 | 初版：监控→解析→下载→上传→删除 全自动化 |
| v1.1 | 2026-05-12 | 上传视频注入宽高比/时长；FastTelethon 并行上传；下载文件校验增强；上传进度双维度 |
| v2.0 | 2026-05-12 | 配置驱动多频道监控；移除 favorites API 依赖；config_monitor 搜索比对；manage_channels CLI 管理；多频道独立上传目标；SQLite schema 扩展 |
| v3.0 | 2026-05-12 | URL 移至 txt 文件存储（一行一集）；移除 v1.1 完全兼容（favorites_notify / pipeline_config / --legacy）；按频道指定封面图；SQLite schema 清理（移除 fav_items / fav_display_names / moon_item_key） |
| v3.1 | 2026-05-12 | Telethon 频道集数核验（#标签+「第N集」正则推断，双重确认避免虚高触发）；upload 重试改用单事件循环；修复核验跳过时 last_episode_count 未同步基线；新增 scripts/clean_urls_txt.py |
