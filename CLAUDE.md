# anime-monitor — 动漫监控全自动流水线

## 项目概述
监控 MoonTV 收藏 → 多供应商比较集数 → 解析 m3u8 → N_m3u8DL-RE 下载 → Telethon 上传 Telegram → 删除本地文件。完全自动化端到端。

## 关键入口

| 入口 | 用途 |
|------|------|
| `python auto_run.py` | 一键启动循环监控（默认30分钟） |
| `python auto_run.py --once` | 单次检查 |
| `python telegram_manager.py login` | 首次生成 Telegram session |
| `python telegram_manager.py profile --name "xxx"` | 修改账号资料 |
| `python telegram_manager.py join --groups "@ch"` | 加群/频道 |

## 核心模块

```
jiankong/
  favorites_notify.py    → 监控入口，定时拉取收藏，发 Telegram 通知
  provider_compare.py    → 多供应商同动漫比较，取最高集数
  pipeline.py            → 流水线编排：解析 m3u8 → 写 SQLite → 触发下载
  m3u8_resolve.py        → m3u8 解析器（moon_tv / stub / placeholder / import）
  moon_tv_m3u8.py        → MoonTV 站点 m3u8 搜索与解析
  pipeline_config.py     → item_key → show_id 映射

app/
  download_worker.py     → 下载+上传（N_m3u8DL-RE + telegram_manager）
  store.py               → SQLite 数据层（建表/读写状态）
  config_loader.py       → config.yaml 加载

telegram_manager.py      → 统一 Telegram 客户端（登录/上传/资料/加群）
```

## 数据流
```
favorites_notify (30min定时)
  → GET /api/favorites → SQLite 比对
  → provider_compare 多供应商取最高集数
  → 发现变更 → pipeline
    → m3u8_resolve 解析新增集 m3u8 URL
    → 写入 episode_jobs 表
    → download_worker 直接调用
      → N_m3u8DL-RE 下载 mp4
      → telegram_manager 上传 Telegram
      → 删除本地 mp4
```

## 可用 Skills
- **m3u8-automation**: 动漫监控自动化流水线的完整架构文档，可复用到其他类似项目
