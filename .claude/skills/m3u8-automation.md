---
name: m3u8-automation
description: 动漫监控自动化流水线：监控收藏→多供应商比较→解析m3u8→下载→上传Telegram→删除本地文件。可复用于其他动漫资源监控项目。
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
│     └─ 状态写入 SQLite (download_status)                 │
├─────────────────────────────────────────────────────────┤
│  ④ 上传层: telegram_manager.py                          │
│     └─ Telethon 直连 (session 复用，避免重复登录)         │
│     └─ send_file() 支持视频流式播放、封面缩略图           │
│     └─ 上传完成 → 标记 upload_status → 删除本地 mp4       │
└─────────────────────────────────────────────────────────┘
```

## 二、核心模块

### 2.1 监控模块 (`jiankong/favorites_notify.py`)

```python
# 核心数据结构
CONFIG = {
    "BASE_URL": "https://xxx.xxx",        # MoonTV 站点
    "FAVORITES_SCRIPT_TOKEN": "",         # Bearer 令牌（推荐）
    "FAVORITES_SCRIPT_USERNAME": "",      # MoonTV 用户名
    "TELEGRAM_BOT_TOKEN": "",             # Telegram Bot Token
    "TELEGRAM_CHAT_ID": "",               # 通知接收 chat_id
    "PIPELINE_ENABLED": "1",              # 启用自动下载上传
    "M3U8_RESOLVER_MODE": "moon_tv",      # m3u8 解析模式
}

# 关键函数
run() -> int                              # 单次检查
main() -> int                             # CLI 入口，支持 --loop --interval
```

**认证机制**：
- 推荐 Bearer Token：服务端设置 `FAVORITES_SCRIPT_TOKEN` + `FAVORITES_SCRIPT_USERNAME`
- 回退 Cookie：`MOONTV_COOKIE_HEADER` 或 `MOONTV_COOKIE_FILE`

### 2.2 多供应商比较 (`jiankong/provider_compare.py`)

```python
# 核心思路：同一动漫在不同供应商有不同的 source_id
# → 按 display_name 分组 → 搜索所有供应商 → 取最高集数
compare_and_get_max_episodes(favorites_data, alias_map) -> dict[str, dict]
# 返回: {display_name: {"key": str, "total": int, "source_name": str, ...}}
```

### 2.3 流水线 (`jiankong/pipeline.py`)

```python
# 监听变更 → 解析 m3u8 → 写入统一 SQLite → 触发下载
run_pipeline_for_changes(changes: list[dict]) -> None

# 关键映射：item_key → show_id (config.yaml shows[].id)
load_item_key_to_show_id() -> dict[str, str]
```

### 2.4 下载模块 (`app/download_worker.py`)

```python
# 核心函数
download_m3u8_re(url, out_path, dl_cfg, working_dir, clean_proxy)
run_download_upload(upload_enabled_override=None)

# 上传（异步，需 asyncio.run 包装）
async upload_via_telegram_manager(file_path, caption, target, thumb_path) -> bool
```

**下载要点**：
- N_m3u8DL-RE 必须放在 `xiazai/` 目录或 PATH 中
- 下载时清空代理环境变量（避免走代理影响速度）
- 支持多线程、断点续传、ffmpeg 合并

### 2.5 Telegram 管理器 (`telegram_manager.py`)

```python
class TelegramManager:
    async login(force_relogin=False)       # session 优先 → API 接码 → 交互式
    async upload_video(file_path, caption) # 直接 Telethon send_file
    async update_profile(name, bio, ...)   # 修改账号资料
    async join_groups(groups)              # 加群/频道
    async disconnect()

# CLI 用法
python telegram_manager.py login           # 首次登录生成 session
python telegram_manager.py profile --name "xxx" --bio "xxx"
python telegram_manager.py join --groups "@ch1,@ch2"
```

**Session 管理**：
- session 文件路径在 config.yaml `telegram.session_path`
- 首次运行 `python telegram_manager.py login` 生成
- session 有效期内无需重复登录

### 2.6 SQLite 数据模型 (`app/store.py`)

```sql
-- 收藏历史（监控用）
fav_items (item_key TEXT PK, total_episodes INT, title TEXT, last_total INT)
fav_display_names (item_key TEXT PK, display_name TEXT)

-- 番剧配置（下载上传用）
show_profiles (show_id TEXT PK, moon_item_key TEXT UNIQUE, topic_name TEXT,
               anime_prefix TEXT, caption_file TEXT, download_dir TEXT)

-- 分集任务（下载上传状态追踪）
episode_jobs (show_id TEXT, episode INT, url TEXT,
              download_status TEXT, upload_status TEXT, updated_at TEXT,
              PRIMARY KEY (show_id, episode))
```

**状态机**：
```
download_status: '' → downloading → downloaded / download_failed
upload_status:   '' → uploading → uploaded / upload_failed
```

## 三、配置清单

### config.yaml 必要字段
```yaml
database:
  path: data/app_state.sqlite

proxy:
  upload:
    enabled: true
    socks5: { host, port, username, password }

telegram:
  api_id: ''
  api_hash: ''
  target: '-100xxx'       # 上传目标频道
  session_path: 'session.session'
  phone: '+1xxx'

m3u8dl_re:
  executable: N_m3u8DL-RE
  auto_select: true
  ffmpeg_binary_path: ''

shows:
  - id: panlong              # 与 pipeline_config 的 ITEM_KEY_TO_SHOW_ID 一致
    moon_item_key: '23+81489'
    topic_name: 盘龙
    anime_prefix: 盘龙第
    caption_file: 'caption.txt'
    download_dir: xiazai/downloads/panlong
    sort_order: 1
```

### pipeline_config.py 必要配置
```python
ITEM_KEY_TO_SHOW_ID = {
    "23+81489": "panlong",     # MoonTV item_key → config.yaml shows[].id
    "37+97662": "zetianji",
    ...
}
```

### 环境变量（favorites_notify.py CONFIG）
```python
CONFIG = {
    "BASE_URL": "https://xxx",
    "TELEGRAM_BOT_TOKEN": "xxx:xxx",
    "TELEGRAM_CHAT_ID": "xxx",
    "FAVORITES_SCRIPT_TOKEN": "",
    "FAVORITES_SCRIPT_USERNAME": "",
    "PIPELINE_ENABLED": "1",
}
```

## 四、服务端部署

### systemd 示例
```ini
[Unit]
Description=Anime Monitor Auto Pipeline
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/anime-monitor
ExecStart=/usr/bin/python3 auto_run.py --interval 1800
Restart=always
RestartSec=30
User=anime
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Docker 部署要点
```dockerfile
FROM python:3.11-slim
RUN apt update && apt install -y ffmpeg wget
COPY xiazai/N_m3u8DL-RE /app/xiazai/N_m3u8DL-RE
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
CMD ["python", "auto_run.py", "--interval", "1800"]
```

### 服务端关键注意事项
1. **N_m3u8DL-RE**: 需下载对应平台二进制，放在 `xiazai/` 目录
2. **ffmpeg**: 服务端需安装 ffmpeg（m3u8 合并用）
3. **Session 文件**: 先在本地运行 `python telegram_manager.py login`，将 session 文件上传到服务器
4. **config.yaml**: 不提交到 git（在 .gitignore），手动 scp 到服务器
5. **SOCKS5 代理**: 服务器如在国内，需配置代理上传 Telegram

## 五、复用到新项目的步骤

1. **复制核心模块**：
   ```
   jiankong/favorites_notify.py   → 监控入口（修改 BASE_URL + API 格式）
   jiankong/provider_compare.py   → 多供应商比较（适配新站点 API）
   jiankong/pipeline.py           → 流水线编排（改 m3u8 解析逻辑）
   jiankong/m3u8_resolve.py       → m3u8 解析（适配新站点搜索 API）
   app/download_worker.py         → 下载（保持不变，复用 N_m3u8DL-RE）
   app/store.py                   → SQLite 数据层（保持不变）
   telegram_manager.py            → Telegram 管理（保持不变）
   ```

2. **适配站点 API**：
   - `favorites_notify.py`: 修改 `http_get_favorites()` 适配新站点收藏 API
   - `moon_tv_m3u8.py`: 修改搜索和 m3u8 解析逻辑

3. **配置映射**：
   - `pipeline_config.py`: 更新 ITEM_KEY_TO_SHOW_ID
   - `config.yaml`: 更新 shows 列表
   - `favorites_notify.py` CONFIG: 更新 BASE_URL、Telegram 凭据

4. **生成 session 文件**：
   ```bash
   python telegram_manager.py login
   ```

5. **测试流程**：
   ```bash
   python auto_run.py --once    # 单次测试
   python auto_run.py           # 开启循环
   ```

## 六、调试与日志

```bash
# 启用 DEBUG 日志
LOG_LEVEL=DEBUG python auto_run.py --once

# 跳过下载（仅解析 m3u8）
PIPELINE_SKIP_XIAZAI=1 python auto_run.py --once

# 使用占位符 m3u8（调试流水线不下载）
M3U8_RESOLVER_MODE=placeholder python auto_run.py --once

# 指定收藏 item_key 白名单（仅处理特定番剧）
PIPELINE_ITEM_KEYS="23+81489,maotaizy+91" python auto_run.py --once
```
