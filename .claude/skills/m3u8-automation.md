---
name: m3u8-automation
description: 动漫监控自动化流水线：监控收藏→多供应商比较→解析m3u8→下载→上传Telegram(正确宽高比/视频气泡)→删除本地文件。可复用于其他动漫资源监控项目。
---

# 动漫监控全自动流水线 Skill

## 一、整体架构

```
┌─────────────────────────────────────────────────────────┐
│  auto_run.py / favorites_notify.py --loop               │
│  (定时器，默认30分钟)                                     │
├─────────────────────────────────────────────────────────┤
│  ① 监控层: favorites_notify.py                          │
│     └─ GET /api/favorites → SQLite 比对 → 发现变更       │
│     └─ provider_compare.py: 多供应商同动漫比较，取最高集数  │
├─────────────────────────────────────────────────────────┤
│  ② 解析层: pipeline.py → m3u8_resolve.py                 │
│     └─ /api/search → 匹配 source_name → 读取 episodes    │
│     └─ 输出: {集数: m3u8_url} → 写入 episode_jobs 表     │
├─────────────────────────────────────────────────────────┤
│  ③ 下载层: download_worker.py                           │
│     └─ N_m3u8DL-RE 子进程 (m3u8 → mp4)                  │
│     └─ 文件存在性校验 + 异常边界处理                       │
│     └─ 状态写入 SQLite (download_status)                 │
├─────────────────────────────────────────────────────────┤
│  ④ 上传层: telegram_manager.py                          │
│     └─ ffprobe 提取宽/高/时长 → DocumentAttributeVideo   │
│     └─ Telethon send_file (视频气泡 + 流式播放)           │
│     └─ FastTelethon 并行分片 (parallel=true 时启用)      │
│     └─ 上传完成 → 标记 upload_status → 删除本地 mp4       │
└─────────────────────────────────────────────────────────┘
```

## 二、核心模块

### 2.1 监控模块 (`jiankong/favorites_notify.py`)

```python
CONFIG = {
    "BASE_URL": "https://xxx.xxx",
    "FAVORITES_SCRIPT_TOKEN": "",       # Bearer 令牌（推荐）
    "FAVORITES_SCRIPT_USERNAME": "",    # MoonTV 用户名
    "TELEGRAM_BOT_TOKEN": "",           # 变更通知 Bot
    "TELEGRAM_CHAT_ID": "",             # 通知接收 chat_id
    "PIPELINE_ENABLED": "1",            # 启用自动下载上传
    "M3U8_RESOLVER_MODE": "moon_tv",    # m3u8 解析模式
}

run() -> int                            # 单次检查
main() -> int                           # CLI 入口，支持 --loop --interval
```

**认证机制**：推荐 Bearer Token（服务端需配置同名环境变量），回退 Cookie。

### 2.2 多供应商比较 (`jiankong/provider_compare.py`)

同一动漫在不同供应商有不同的 source_id → 按 display_name 分组 → 搜索所有供应商 → 取最高集数。解决各供应商更新速度不一致的问题。

```python
compare_and_get_max_episodes(favorites_data, alias_map) -> dict[str, dict]
# 返回: {display_name: {"key": str, "total": int, "source_name": str, ...}}
```

### 2.3 流水线 (`jiankong/pipeline.py`)

```python
run_pipeline_for_changes(changes: list[dict]) -> None
# ① 遍历变更 → m3u8_resolve 解析新集 m3u8 URL
# ② 写入 episode_jobs 表（download_status='pending'）
# ③ 直接调用 run_download_upload(upload_enabled_override=True)

load_item_key_to_show_id() -> dict[str, str]  # item_key → show_id 映射
```

### 2.4 下载模块 (`app/download_worker.py`)

```python
download_m3u8_re(url, out_path, dl_cfg, *, working_dir, clean_proxy)
# → N_m3u8DL-RE 子进程下载，完成后校验文件存在

run_download_upload(upload_enabled_override=None)
# → 遍历 show_profiles → 检查 episode_jobs → 下载 + 上传

async upload_via_telegram_manager(file_path, caption, *,
    target, thumb_path, progress_callback) -> bool
# → asyncio.run() 包装，每次调用独立 event loop
```

**下载要点**：
- N_m3u8DL-RE 放在 `xiazai/` 或 PATH
- 下载时清空代理环境变量（`_download_subprocess_env`）
- 下载失败自动清理残骸文件 (`_delete_local_file`)

**进度追踪**（v1.1 新增）：
- `_count_planned_uploads()` — 预计算本轮上传总数，作为进度分母
- `_make_show_upload_progress_cb()` — 单文件 + 本剧总进度双维度显示

### 2.5 Telegram 管理器 (`telegram_manager.py`)

```python
class TelegramManager:
    async login(force_relogin=False)        # session 优先 → API 接码 → 交互式
    async upload_video(file_path, caption, *, thumb_path, progress_callback)
    async update_profile(name, bio, avatar, username)
    async join_groups(groups)
    async disconnect()

# CLI
python telegram_manager.py login
python telegram_manager.py profile --name "xx" --bio "xx"
python telegram_manager.py join --groups "@ch1,@ch2"
```

**上传视频格式优化**（v1.1 核心改进）：

```
ffprobe 提取视频元信息
  → width / height / duration
  → 构建 DocumentAttributeVideo
  → Telethon send_file(attributes=..., supports_streaming=True, force_document=False)
  → Telegram 客户端展示为视频气泡（非文件附件）
  → 正确显示时长、分辨率，可直接内联播放
```

**双路径上传**：
| 路径 | 触发条件 | 特点 |
|------|---------|------|
| FastTelethon 并行 | `fastupload.parallel: true` + 已安装 FastTelethonhelper | 多连接分片上传（加速大文件） |
| Telethon 内置 | 默认 | 单连接，稳定可靠，仍带正确视频属性 |

**环境变量控制**：
```bash
TELEGRAM_DISABLE_FAST_UPLOAD=1    # 强制走内置上传
TELEGRAM_UPLOAD_CONNECTIONS=16    # 连接数
TELEGRAM_FAST_MAX_CONNECTIONS=8   # 并行上限
```

**辅助模块**：
- `_stderr_upload_progress(label)` — 实时上传速度/百分比到 stderr
- `_get_telethon_fast_upload_speed_module()` — 加载 Telethon-FastUpload 辅助脚本
- `ft._infer_nosound_video(fp)` — ffprobe 检测无音轨
- `ft._build_document_attributes(fp, thumb, nosound_hint)` — 构建视频属性（宽高比核心）

### 2.6 SQLite 数据模型 (`app/store.py`)

```sql
fav_items (item_key TEXT PK, total_episodes INT, title TEXT, last_total INT)
fav_display_names (item_key TEXT PK, display_name TEXT)
show_profiles (show_id TEXT PK, moon_item_key TEXT UNIQUE, topic_name TEXT,
               anime_prefix TEXT, caption_file TEXT, download_dir TEXT)
episode_jobs (show_id TEXT, episode INT, url TEXT,
              download_status TEXT, upload_status TEXT, updated_at TEXT,
              PRIMARY KEY (show_id, episode))
```

**状态机**：
```
download_status: '' → downloading → downloaded / download_failed
upload_status:   '' → uploaded / upload_failed
```

## 三、配置清单

### config.yaml 完整结构
```yaml
database:
  path: data/app_state.sqlite

proxy:
  upload:
    enabled: true
    socks5: { host: 127.0.0.1, port: 10809, username: '', password: '' }

telegram:
  api_id: 'YOUR_API_ID'
  api_hash: 'YOUR_API_HASH'
  target: '-100xxxxxxxxx'
  session_path: Telethon-FastUpload/session.session
  phone: '+1xxxxxxxxx'

# v1.1 新增：FastTelethon 上传加速配置
fastupload:
  enabled: true          # 启用 FastTelethon 模块
  parallel: false        # 并行分片上传（默认关闭，需显式开启）
  connections: 16        # 连接数（parallel=true 时生效）

m3u8dl_re:
  executable: N_m3u8DL-RE
  auto_select: true
  ffmpeg_binary_path: ''
  log_level: ERROR

runtime:
  upload_enabled: true
  upload_retries: 5

shows:
  - id: panlong           # 与 pipeline_config 的 ITEM_KEY_TO_SHOW_ID 一致
    moon_item_key: '23+81489'
    topic_name: 盘龙
    anime_prefix: 盘龙第
    caption_file: Telethon-FastUpload/盘龙.txt
    download_dir: xiazai/downloads/panlong
    sort_order: 1
    urls: [...]           # 可选：手动指定 m3u8 URL 列表
```

### pipeline_config.py
```python
ITEM_KEY_TO_SHOW_ID = {
    "23+81489": "panlong",       # MoonTV item_key → config.yaml shows[].id
    "maotaizy+91": "mushenji",
    ...
}
```

## 四、服务端部署

### systemd
```ini
[Unit]
Description=Anime Monitor Pipeline
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/anime-monitor
ExecStart=/usr/bin/python3 auto_run.py --interval 1800
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Docker 关键点
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
2. ffmpeg + ffprobe 必须安装（下载合并 + 视频元信息提取）
3. 本地 `python telegram_manager.py login` 生成 session，scp 到服务器
4. config.yaml 不提交 git（在 .gitignore），手动部署
5. 国内服务器需 SOCKS5 代理（`proxy.upload.socks5`）

## 五、复用到新项目的步骤

1. **复制核心模块**：
   ```
   jiankong/favorites_notify.py   → 改 BASE_URL + API 格式
   jiankong/provider_compare.py   → 适配新站点 API
   jiankong/pipeline.py           → 改 m3u8 解析逻辑
   jiankong/m3u8_resolve.py       → 注册新解析模式
   app/download_worker.py         → 保持不变（复用 N_m3u8DL-RE）
   app/store.py                   → 保持不变
   telegram_manager.py            → 保持不变（session/上传/视频属性）
   Telethon-FastUpload/           → 保持不变（FastTelethon 辅助模块）
   ```

2. **适配站点 API**：改 `favorites_notify.py` 的 `http_get_favorites()` 和 `moon_tv_m3u8.py` 的搜索解析

3. **配置映射**：更新 `pipeline_config.py` → `config.yaml` shows 列表 → `favorites_notify.py` CONFIG

4. **生成 session**：`python telegram_manager.py login`

5. **测试**：`python auto_run.py --once` → 确认无误后 `python auto_run.py`

## 六、调试

```bash
LOG_LEVEL=DEBUG python auto_run.py --once           # 详细日志
PIPELINE_SKIP_XIAZAI=1 python auto_run.py --once    # 只解析 m3u8 不下载
M3U8_RESOLVER_MODE=placeholder python auto_run.py --once  # 占位符调试
PIPELINE_ITEM_KEYS="23+81489,maotaizy+91" python auto_run.py --once  # 白名单
TELEGRAM_DISABLE_FAST_UPLOAD=1 python auto_run.py --once  # 强制内置上传
```

## 七、版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-05-11 | 初版：监控→解析→下载→上传→删除 全自动化 |
| v1.1 | 2026-05-12 | 上传视频注入宽高比/时长属性（DocumentAttributeVideo）；FastTelethon 并行上传；下载文件校验增强；上传进度双维度显示 |
