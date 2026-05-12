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
import ssl
import sys
import threading
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
    update_show_monitor_telegram_cache,
    upsert_channel,
    upsert_show_monitor_state,
    utc_now_iso,
)
from jiankong.provider_compare import search_all_providers, search_all_providers_v2


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
    tv = m.get("telegram_channel_verify")
    if not isinstance(tv, dict):
        tv = {}
    return {
        "notify_bot_token": str(m.get("notify_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(),
        "notify_chat_id": str(m.get("notify_chat_id") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip(),
        "base_url": str(m.get("base_url") or os.environ.get("BASE_URL") or "").strip().rstrip("/"),
        "interval": int(m.get("interval") or 1800),
        "channels": m.get("channels") or [],
        "telegram_channel_verify": {
            "enabled": bool(tv.get("enabled")),
            "scan_message_limit": int(tv.get("scan_message_limit") or 400),
        },
    }


def search_show_episode_count(show: dict, base_url: str, *, expected_episode_count: int = 0) -> dict | None:
    """
    搜索单剧：用 search_keyword 调用搜索，取最佳匹配结果。

    show.filters 存在时走 V2 路径（moon.658877.xyz 精确元数据匹配），
    否则走 V1 路径（tv.658877.xyz 多供应商比较）。

    返回:
      {title, source_name, source_id, vod_id, total_episodes}
      V2 路径额外包含 _moon_result (MoonShowResult) 供后续提取 m3u8 URL
      或 None（搜索无结果）。
    """
    kw = str(show.get("search_keyword") or "").strip()
    if not kw:
        logging.warning("剧集 %s 未配置 search_keyword，跳过", show.get("id", "?"))
        return None

    filters = show.get("filters")
    if filters and isinstance(filters, dict):
        # ---- V2: moon.658877.xyz 精确匹配 ----
        best = search_all_providers_v2(kw, base_url, filters=filters)
        if best is None:
            return None
        logging.info(
            "搜索(V2) %s → %s (year=%s douban=%s eps=%s source=%s)",
            kw, best.title, best.year, best.douban_id, best.episode_count, best.source_name,
        )
        return {
            "title": best.title,
            "source_name": best.source_name,
            "source_id": best.source,
            "vod_id": best.id,
            "total_episodes": best.episode_count,
            "_moon_result": best,
        }

    # ---- V1: tv.658877.xyz 多供应商比较 ----
    results = search_all_providers(kw, base_url, expected_episode_count=expected_episode_count)
    if not results:
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
    *,
    mc: dict | None = None,
) -> dict | None:
    """
    搜索剧集，与 SQLite show_monitor_state 比对，返回变更字典或 None。

    自动更新 show_monitor_state（新剧基线入库，有变更则更新 last_episode_count）。

    若 monitor.telegram_channel_verify.enabled 为 true（且配置了 telegram_chat_id）：
    1）库中无频道扫描记录 → Telethon 拉一次写入 channel_latest_ep / channel_ep_checked_at；
    2）用站点集数 new_total 与库中频道最新集比较：若 new_total ≤ 频道值 → 不更新；
    3）若 new_total > 频道值 → 再 Telethon 拉一次确认并写库；若仍 new_total ≤ 确认值 → 不更新；
    4）仅当确认后仍 new_total > 频道最新集时，走原有 upsert + 通知 + 流水线。
    """
    cur = conn.cursor()
    show_id = str(show.get("id") or "").strip()
    channel_id = str(channel.get("id") or "").strip()
    telegram_chat_id = str(channel.get("telegram_chat_id") or "").strip()
    topic_name = str(show.get("topic_name") or show_id)
    search_kw = str(show.get("search_keyword") or "").strip()

    state = get_show_monitor_state(conn, show_id)
    old_total = int(state["last_episode_count"]) if state else 0

    result = search_show_episode_count(show, base_url, expected_episode_count=old_total)
    if result is None:
        return None

    new_total = result["total_episodes"]

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

    mc = mc or {}
    tgv = mc.get("telegram_channel_verify")
    if not isinstance(tgv, dict):
        tgv = {}
    verify_on = bool(tgv.get("enabled")) and not bool(show.get("telegram_verify_disabled"))
    scan_limit = max(20, int(tgv.get("scan_message_limit") or 400))

    checked_at = (
        str(state["channel_ep_checked_at"] or "")
        if "channel_ep_checked_at" in state.keys()
        else ""
    )
    has_channel_record = bool(checked_at.strip())

    ch_db = 0
    if "channel_latest_ep" in state.keys():
        try:
            ch_db = int(state["channel_latest_ep"] or 0)
        except (TypeError, ValueError):
            ch_db = 0

    if verify_on:
        if not telegram_chat_id:
            logging.warning(
                "已开启 telegram_channel_verify 但频道未配置 telegram_chat_id，跳过核验: %s",
                channel.get("name", channel_id),
            )
        else:
            hashtag = str(show.get("telegram_verify_hashtag") or "").strip()
            if not hashtag:
                hashtag = f"#{topic_name.replace(' ', '').strip()}"

            from jiankong.channel_episode_telethon import scan_channel_max_episode_blocking

            def _scrape_channel() -> int:
                return scan_channel_max_episode_blocking(
                    telegram_chat_id=telegram_chat_id,
                    hashtag=hashtag,
                    message_limit=scan_limit,
                )

            ch_work = ch_db
            if not has_channel_record:
                try:
                    ch_work = _scrape_channel()
                    update_show_monitor_telegram_cache(
                        conn,
                        show_id,
                        channel_latest_ep=ch_work,
                        channel_ep_checked_at=utc_now_iso(),
                    )
                    conn.commit()
                    logging.info(
                        "频道最新集数（库中无记录，首次 Telethon）: %s → %s",
                        topic_name,
                        ch_work,
                    )
                except Exception:
                    logging.exception(
                        "Telethon 首次拉取频道集数失败，跳过本轮（未更新 last_episode_count）: %s",
                        topic_name,
                    )
                    return None

            if new_total <= ch_work:
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
                    "站点集数 %s 未高于频道记录 %s，已同步基线跳过: %s",
                    new_total,
                    ch_work,
                    topic_name,
                )
                return None

            try:
                ch2 = _scrape_channel()
                update_show_monitor_telegram_cache(
                    conn,
                    show_id,
                    channel_latest_ep=ch2,
                    channel_ep_checked_at=utc_now_iso(),
                )
                conn.commit()
                logging.info(
                    "站点 %s > 频道记录 %s，二次 Telethon 确认频道最新集=%s: %s",
                    new_total,
                    ch_work,
                    ch2,
                    topic_name,
                )
            except Exception:
                logging.exception(
                    "Telethon 二次确认失败，跳过本轮（未更新 last_episode_count）: %s",
                    topic_name,
                )
                return None

            if new_total <= ch2:
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
                    "二次确认后站点 %s ≤ 频道 %s，已同步基线跳过流水线: %s",
                    new_total,
                    ch2,
                    topic_name,
                )
                return None

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

    change: dict = {
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

    moon_result = result.get("_moon_result")
    if moon_result is not None:
        from jiankong.moon_api import extract_new_episode_m3u8s
        urls = extract_new_episode_m3u8s(moon_result, old_total, new_total)
        if urls:
            change["_episode_urls_direct"] = urls
            logging.info(
                "V2 快通道: %s 直接从搜索获取 %s 集 m3u8",
                topic_name, len(urls),
            )

    return change


def _telegram_api_post(bot_token: str, method: str, payload: dict, *, timeout: float = 45) -> bool:
    """通过 SOCKS5 代理（若有配置）调用 Telegram Bot API，直连失败时回退代理。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path = f"/bot{bot_token}/{method}"
    headers = {
        "Content-Type": "application/json",
        "Host": "api.telegram.org",
        "Connection": "close",
    }

    def _direct() -> bool:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.telegram.org{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=timeout).read()
            return True
        except Exception:
            return False

    # 先尝试直连
    if _direct():
        return True

    # 直连失败，尝试 SOCKS5 代理
    socks_url = _load_notify_proxy_url()
    if not socks_url:
        return False

    try:
        from python_socks.sync import Proxy
        proxy = Proxy.from_url(socks_url)
        sock = proxy.connect("api.telegram.org", 443)
        ctx = ssl.create_default_context()
        ssock = ctx.wrap_socket(sock, server_hostname="api.telegram.org")

        req_line = f"POST {path} HTTP/1.1\r\n"
        hdr_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        hdr_lines += f"Content-Length: {len(body)}\r\n\r\n"
        ssock.sendall(req_line.encode() + hdr_lines.encode() + body)

        # 读取响应
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += ssock.recv(4096)
        ssock.close()
        status_line = resp.split(b"\r\n")[0].decode()
        return status_line.startswith("HTTP/1.1 200")
    except Exception:
        return False


def _load_notify_proxy_url() -> str | None:
    """从 config.yaml proxy.upload.socks5 构建 SOCKS5 URL。"""
    try:
        from app.config_loader import load_config
        from app.proxy_util import build_socks5_url
        cfg = load_config()
        upload_cfg = (cfg.get("proxy") or {}).get("upload")
        if not isinstance(upload_cfg, dict):
            # 也尝试 CLI 代理子进程一样的环境变量
            host = os.environ.get("TELEGRAM_PROXY_HOST") or os.environ.get("SOCKS5_HOST") or ""
            port = os.environ.get("TELEGRAM_PROXY_PORT") or os.environ.get("SOCKS5_PORT") or ""
            if host and port:
                return f"socks5://{host}:{port}"
            return None
        return build_socks5_url(upload_cfg)
    except Exception:
        return None


def send_telegram_notification(bot_token: str, chat_id: str, changes: list[dict], channel_map: dict[str, str]) -> None:
    """发送 Telegram Bot 通知，按频道分组显示变更。直连失败自动走 SOCKS5 代理。"""
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
        blocks.append(f"  {ch_name}")
        for c in items:
            label = c.get("display_name") or c["title"] or c["show_id"]
            blocks.append(f"  - {label} {c['old_total']}→{c['new_total']}")

    msg = "\n".join(blocks)[:3900]
    success = _telegram_api_post(bot_token, "sendMessage", {"chat_id": chat_id, "text": msg})

    if success:
        logging.info("已发送 Telegram 通知至 chat_id=%s", chat_id)
    else:
        logging.error("Telegram 通知发送失败（直连+代理均不可用）")


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
    pipeline_threads: list[threading.Thread] = []
    pe = (os.environ.get("PIPELINE_ENABLED") or "").strip().lower()
    pipeline_on = pe in ("1", "true", "yes", "on")

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
                change = detect_show_changes(show, ch, base_url, conn, mc=mc)
                if change:
                    changes.append(change)
                    # 扫到变更立即启动下载（后台线程）
                    if pipeline_on:
                        t = threading.Thread(
                            target=_run_single_change_pipeline,
                            args=(change,),
                            daemon=True,
                        )
                        t.start()
                        pipeline_threads.append(t)
            except Exception:
                logging.exception("检查剧集异常: %s", show.get("topic_name", sid))

    conn.close()

    if not changes:
        logging.info("所有频道无变更")
        return 0

    logging.info("共 %s 条变更，发送通知", len(changes))
    send_telegram_notification(mc["notify_bot_token"], mc["notify_chat_id"], changes, channel_map)

    # 等待异步流水线完成
    for t in pipeline_threads:
        t.join(timeout=3600)

    return 0


def _run_single_change_pipeline(change: dict) -> None:
    """在后台线程中执行单个变更的流水线。"""
    try:
        from jiankong.pipeline import run_pipeline_for_change
        run_pipeline_for_change(change)
    except Exception:
        logging.exception("流水线执行异常: %s", change.get("display_name", "?"))


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
