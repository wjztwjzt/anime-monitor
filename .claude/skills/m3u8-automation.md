---
name: m3u8-automation
description: 多频道动漫/影视监控自动化流水线 v3.2：配置驱动多频道→MoonTVPlus API 豆瓣ID精确匹配→Telethon频道核验→快通道m3u8提取/解析→下载→上传对应频道(正确宽高比)→删除本地文件。可复用于其他动漫资源监控项目。
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
│     └─ show.filters 存在 → V2 精确匹配（moon.658877.xyz）       │
│     └─ show.filters 不存在 → V1 多供应商比较（tv.658877.xyz）    │
│     └─ V2: moon_api.search → match_best_show (豆瓣ID/年份评分)  │
│     └─ V2: extract_new_episode_m3u8s() 直接从搜索提取 m3u8     │
│     └─ 与 SQLite show_monitor_state 比对集数                   │
│     └─ 集数上涨 → Telethon 扫频道核实（#标签+「第N集」）        │
│     └─ 有变更 → 发送 Telegram Bot 通知（按频道分组）            │
├──────────────────────────────────────────────────────────────┤
│  ② 核验层: jiankong/channel_episode_telethon.py               │
│     └─ 拉取频道历史消息，按 #tag + 集数文案推断已发最大集       │
│     └─ 双重确认：首次拉取 + 二次确认，避免虚高触发流水线        │
├──────────────────────────────────────────────────────────────┤
│  ③ 解析层: jiankong/pipeline.py → m3u8_resolve.py / moon_api  │
│     └─ V2 快通道: _episode_urls_direct → 直接写入 episode_jobs  │
│     └─ V1 回退: /api/search → 匹配 source_name → 读取 episodes │
│     └─ 输出: {集数: m3u8_url} → 写入 episode_jobs 表           │
├──────────────────────────────────────────────────────────────┤
│  ④ 下载层: app/download_worker.py                             │
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

通过 config.yaml 定义频道和剧集，搜索 API 检测更新。show.filters 存在时走 V2 精确匹配（moon.658877.xyz），否则回退 V1 多供应商比较。

```python
search_show_episode_count(show, base_url, *, expected_episode_count=0) -> dict | None
# show.filters 存在 → V2: search_all_providers_v2() → match_best_show (豆瓣ID评分)
# show.filters 不存在 → V1: search_all_providers() 多供应商比较
# V2 返回额外包含 _moon_result (MoonShowResult) 供 URL 提取

detect_show_changes(show, channel, base_url, conn, *, mc=None) -> dict | None
# V2 路径: 从 _moon_result 调用 extract_new_episode_m3u8s()
#         将结果写入 change["_episode_urls_direct"] 供 pipeline 快通道

run_monitor(config_path=None, *, channel_filter=None) -> int
# ① 加载 config.yaml monitor.channels
# ② 遍历 channel → show: V2/V1 搜索 → 比对集数
# ③ 集数上涨 → Telethon 扫频道核实
# ④ 有变更 → 发送 Telegram 通知 → 触发 pipeline

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

### 2.5 流水线 (`jiankong/pipeline.py`)

```python
run_pipeline_for_changes(changes: list[dict]) -> None
# 变更字典直接包含 show_id、channel_id、telegram_chat_id
# V2 快通道: change["_episode_urls_direct"] 存在 → 跳过 m3u8_resolve，直接写入 episode_jobs
# V1 回退: _episode_urls_direct 不存在 → resolve_new_episode_m3u8_urls()
# 写入 episode_jobs 时携带 channel_id
# 调用 run_download_upload(upload_enabled_override=True)
```

环境变量控制：
```bash
PIPELINE_ENABLED=1              # 启用自动下载上传
PIPELINE_SKIP_XIAZAI=1          # 只解析 m3u8，不下载上传
```

### 2.3 MoonTVPlus API 客户端 (`jiankong/moon_api.py`) ← v3.2 新增

调用 moon.658877.xyz `/api/search`，一次 API 请求完成搜索+精确匹配+获取 m3u8 URL。通过 douban_id/year/class/source_name/type_name 等元数据评分，消除同名不同剧的误匹配。

```python
@dataclass
class MoonShowResult:
    id: str; title: str; poster: str; episodes: list[str]
    source: str; source_name: str; class_tags: list[str]
    year: str; douban_id: str; type_name: str; desc: str
    vod_remarks: str; vod_total: int; proxy_mode: bool; weight: int

search_moon_api(keyword, base_url, *, max_retries, retry_delay) -> list[MoonShowResult]
# HTTP GET /api/search?q=keyword，带重试

score_match(result, *, title, douban_id, year_min, year_max,
            class_keywords, source_preference, type_name) -> tuple[int, str]
# 评分策略: douban_id匹配=1000, title完全匹配=500, 包含=200,
#          year范围内=300, class_keywords=50/条, source_preference=200, type_name=100
# 排除规则: 空白标题、不同剧标记(短剧/剧场版)、标题长度比异常

match_best_show(results, *, title, filters) -> MoonShowResult | None
# 扫描所有结果，返回评分最高的匹配，无匹配返回 None

extract_new_episode_m3u8s(result, old_total, new_total) -> dict[int, str]
# 从 episodes 数组提取 (old, new] 集的 m3u8 URL（0-indexed 数组）
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
| v3.2 | 2026-05-12 | MoonTVPlus API 精确匹配：moon.658877.xyz 搜索+豆瓣ID/年份/分类元数据评分；快通道：搜索直接返回 m3u8 URL 跳过 m3u8_resolve；show.filters 配置块控制 V1/V2 路径切换；向后兼容无 filters 时回退 V1 |
