"""
config_monitor.py — 配置驱动多频道监控 (v2.0)

替代 favorites_notify.py：不再依赖 /api/favorites 收藏接口。
通过 config.yaml monitor.channels 定义频道和剧集，用搜索 API 检测更新。

用法:
  python jiankong/config_monitor.py                      # 单次检查所有频道
  python jiankong/config_monitor.py --channel anime       # 仅检查指定频道
  python jiankong/config_monitor.py --loop                # 循环监控（默认 30 分钟）
  python jiankong/config_monitor.py --loop --interval 600
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config_loader import database_path, load_config
from app.store import (
    connect,
    ensure_schema,
    ensure_schema_v2,
    get_show_monitor_state,
    list_channels,
    upsert_channel,
    upsert_show_monitor_state,
)
from jiankong.provider_compare import search_all_providers


def setup_logging() -> None:
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    cfg: dict = dict(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    if sys.version_info >= (3, 8):
        cfg["force"] = True
    logging.basicConfig(**cfg)


def load_monitor_config(cfg: dict) -> dict:
    """提取 monitor 配置段，兼容环境变量回退。"""
    m = cfg.get("monitor") or {}
    if not isinstance(m, dict):
        m = {}
    return {
        "notify_bot_token": str(m.get("notify_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(),
        "notify_chat_id": str(m.get("notify_chat_id") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip(),
        "base_url": str(m.get("base_url") or os.environ.get("BASE_URL") or "").strip().rstrip("/"),
        "interval": int(m.get("interval") or 1800),
        "channels": m.get("channels") or [],
    }


def search_show_episode_count(show: dict, base_url: str) -> dict | None:
    """
    搜索单剧：用 search_keyword 调用多供应商搜索，取最高集数结果。

    返回:
      {title, source_name, source_id, vod_id, total_episodes, episodes_list}
      或 None（搜索无结果）。
    """
    kw = str(show.get("search_keyword") or "").strip()
    if not kw:
        logging.warning("剧集 %s 未配置 search_keyword，跳过", show.get("id", "?"))
        return None

    results = search_all_providers(kw, base_url)
    if not results:
        logging.info("搜索无结果: %s", kw)
        return None

    best = results[0]
    episode_count = 0
    for key in ("total_episodes", "totalEpisodes", "episode_count", "totalEpisodesCount"):
        v = best.get(key)
        if v is not None:
            try:
                episode_count = int(v)
                break
            except (TypeError, ValueError):
                pass
    if episode_count == 0:
        episodes = best.get("episodes") or best.get("episode_list") or []
        if isinstance(episodes, list):
            episode_count = len(episodes)

    if episode_count == 0:
        logging.info("搜索结果无法确定集数: %s", kw)
        return None

    source_name = str(best.get("source_name") or best.get("sourceName") or "")
    source_id = str(best.get("source_id") or best.get("sourceId") or best.get("source") or "")
    if isinstance(source_id, dict):
        source_id = str(source_id.get("id") or source_id.get("name") or "")
    vod_id = str(best.get("id") or best.get("vod_id") or best.get("vodId") or "")
    title = str(best.get("title") or kw)

    logging.info(
        "搜索 %s → %s (provider=%s, episodes=%s)",
        kw, title, source_name or source_id, episode_count,
    )
    return {
        "title": title,
        "source_name": source_name,
        "source_id": source_id,
        "vod_id": vod_id,
        "total_episodes": episode_count,
    }


def detect_show_changes(
    show: dict,
    channel: dict,
    base_url: str,
    conn,
) -> dict | None:
    """
    搜索剧集，与 SQLite show_monitor_state 比对，返回变更字典或 None。

    自动更新 show_monitor_state（新剧基线入库，有变更则更新 last_episode_count）。
    """
    cur = conn.cursor()
    show_id = str(show.get("id") or "").strip()
    channel_id = str(channel.get("id") or "").strip()
    telegram_chat_id = str(channel.get("telegram_chat_id") or "").strip()
    topic_name = str(show.get("topic_name") or show_id)
    search_kw = str(show.get("search_keyword") or "").strip()

    result = search_show_episode_count(show, base_url)
    if result is None:
        return None

    new_total = result["total_episodes"]
    state = get_show_monitor_state(conn, show_id)

    if state is None:
        # 新剧：基线入库，不发通知
        upsert_show_monitor_state(
            conn,
            show_id=show_id,
            channel_id=channel_id,
            search_keyword=search_kw,
            last_episode_count=new_total,
            source_name=result["source_name"],
            source_id=result["source_id"],
            vod_id=result["vod_id"],
            title=result["title"],
        )
        conn.commit()
        logging.info("新剧基线入库（不发通知）: %s total=%s", topic_name, new_total)
        return None

    old_total = int(state["last_episode_count"])
    if new_total <= old_total:
        logging.info("无变更: %s (%s→%s)", topic_name, old_total, new_total)
        return None

    # 有变更：更新状态
    upsert_show_monitor_state(
        conn,
        show_id=show_id,
        channel_id=channel_id,
        search_keyword=search_kw,
        last_episode_count=new_total,
        source_name=result["source_name"],
        source_id=result["source_id"],
        vod_id=result["vod_id"],
        title=result["title"],
    )
    conn.commit()
    logging.info(
        "检测到变更: %s [%s] %s→%s (provider=%s)",
        topic_name, channel.get("name", channel_id),
        old_total, new_total, result["source_name"] or result["source_id"],
    )

    return {
        "show_id": show_id,
        "channel_id": channel_id,
        "telegram_chat_id": telegram_chat_id,
        "search_keyword": search_kw,
        "title": result["title"],
        "display_name": topic_name,
        "old_total": old_total,
        "new_total": new_total,
        "source_name": result["source_name"],
        "source_id": result["source_id"],
        "vod_id": result["vod_id"],
    }


def send_telegram_notification(bot_token: str, chat_id: str, changes: list[dict], channel_map: dict[str, str]) -> None:
    """发送 Telegram Bot 通知，按频道分组显示变更。"""
    if not bot_token or not chat_id:
        logging.warning("未配置 notify_bot_token / notify_chat_id，跳过通知")
        return

    # 按频道分组
    by_channel: dict[str, list[dict]] = {}
    for c in changes:
        ch_id = c.get("channel_id", "")
        by_channel.setdefault(ch_id, []).append(c)

    blocks: list[str] = []
    for ch_id, items in by_channel.items():
        ch_name = channel_map.get(ch_id, ch_id)
        blocks.append(f"📺 {ch_name}")
        for c in items:
            label = c.get("display_name") or c["title"] or c["show_id"]
            blocks.append(f"  - {label} {c['old_total']}→{c['new_total']}")

    msg = "\n".join(blocks)[:3900]
    payload = json.dumps(
        {"chat_id": chat_id, "text": msg}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=45).read()
        logging.info("已发送 Telegram 通知至 chat_id=%s", chat_id)
    except Exception:
        logging.exception("Telegram 通知发送失败")


def sync_channels_to_db(conn, channels: list[dict]) -> dict[str, str]:
    """将 config 中的频道同步到 SQLite，返回 {channel_id: channel_name} 映射。"""
    channel_map: dict[str, str] = {}
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if not cid:
            continue
        cname = str(ch.get("name") or cid)
        chat_id = str(ch.get("telegram_chat_id") or "").strip()
        upsert_channel(
            conn,
            channel_id=cid,
            channel_name=cname,
            telegram_chat_id=chat_id,
            channel_type=str(ch.get("type") or "").strip(),
            cover=str(ch.get("cover") or "").strip(),
            sort_order=int(ch.get("sort_order") or 0),
        )
        channel_map[cid] = cname
    conn.commit()
    return channel_map


def run_monitor(config_path: Path | None = None, *, channel_filter: str | None = None) -> int:
    """单次监控：遍历频道→剧集，搜索比对，触发流水线。"""
    cfg = load_config(config_path)
    mc = load_monitor_config(cfg)

    base_url = mc["base_url"]
    if not base_url:
        logging.error("未配置 monitor.base_url")
        return 1

    channels = mc["channels"]
    if not channels:
        logging.error("未配置 monitor.channels，请在 config.yaml 中定义频道和剧集")
        return 1

    if channel_filter:
        channels = [ch for ch in channels if ch.get("id") == channel_filter]
        if not channels:
            logging.error("未找到频道: %s", channel_filter)
            return 1

    db_path = database_path(cfg)
    conn = connect(db_path)
    cur = conn.cursor()
    ensure_schema(cur)
    ensure_schema_v2(cur)
    conn.commit()

    channel_map = sync_channels_to_db(conn, channels)

    changes: list[dict] = []
    for ch in channels:
        ch_id = str(ch.get("id") or "").strip()
        ch_name = str(ch.get("name") or ch_id)
        shows = ch.get("shows") or []
        if not shows:
            logging.info("频道 %s 无剧集配置，跳过", ch_name)
            continue

        logging.info("=== 频道: %s (%s) 共 %s 部剧 ===", ch_name, ch_id, len(shows))
        for show in shows:
            if not isinstance(show, dict):
                continue
            sid = str(show.get("id") or "").strip()
            if not sid:
                continue
            try:
                change = detect_show_changes(show, ch, base_url, conn)
                if change:
                    changes.append(change)
            except Exception:
                logging.exception("检查剧集异常: %s", show.get("topic_name", sid))

    conn.close()

    if not changes:
        logging.info("所有频道无变更")
        return 0

    logging.info("共 %s 条变更，发送通知", len(changes))
    send_telegram_notification(mc["notify_bot_token"], mc["notify_chat_id"], changes, channel_map)

    # 触发流水线
    pe = (os.environ.get("PIPELINE_ENABLED") or "").strip().lower()
    if pe in ("1", "true", "yes", "on"):
        try:
            from jiankong.pipeline import run_pipeline_for_changes
            run_pipeline_for_changes(changes)
        except Exception:
            logging.exception("流水线执行异常（通知已发出）")

    return 0


def main() -> int:
    """CLI 入口：支持单次/循环/频道过滤。"""
    import argparse
    import time

    parser = argparse.ArgumentParser(description="config_monitor v2.0 — 配置驱动多频道监控")
    parser.add_argument("--loop", action="store_true", help="循环监控模式")
    parser.add_argument("--interval", type=int, default=1800, help="循环间隔秒数（默认 1800 = 30 分钟）")
    parser.add_argument("--channel", type=str, default=None, help="仅检查指定频道 ID")
    parser.add_argument("--once", action="store_true", help="单次检查（默认行为）")
    args = parser.parse_args()

    setup_logging()

    # 从 config 读取间隔（命令行可覆盖）
    try:
        cfg = load_config()
        mc = load_monitor_config(cfg)
        if mc.get("interval") and not parser.parse_known_args()[0].interval != 1800:
            pass  # 使用命令行默认或覆盖
    except Exception:
        pass

    if not args.loop:
        logging.info("启动 config_monitor v2.0（单次）")
        try:
            return run_monitor(channel_filter=args.channel)
        except KeyboardInterrupt:
            logging.info("中断")
            return 130

    logging.info(
        "启动 config_monitor v2.0 循环监控（间隔 %s 秒 = %s 分钟）",
        args.interval, round(args.interval / 60, 1),
    )
    while True:
        try:
            ret = run_monitor(channel_filter=args.channel)
            if ret != 0:
                logging.warning("本轮检查返回码=%s，继续下一轮", ret)
        except KeyboardInterrupt:
            logging.info("循环监控已停止")
            return 0
        except Exception:
            logging.exception("本轮检查异常，%s 秒后重试", args.interval)

        logging.info("等待 %s 秒后下一轮检查...", args.interval)
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            logging.info("循环监控已停止")
            return 0


if __name__ == "__main__":
    sys.exit(main())
