"""
manage_channels.py — 频道 & 剧集配置管理 CLI (v3.0)

直接读写 config.yaml 的 monitor 段，支持频道/剧集/手动 URL 的增删改查，
以及 SQLite 监控状态的查看与重置。

URL 以 txt 文件存储（一行一个），配置文件只引用 urls_file 路径。

用法:
  python manage_channels.py list-channels
  python manage_channels.py add-channel --id anime --name "动漫频道" --chat-id "-100xxx"
  python manage_channels.py delete-channel --id anime

  python manage_channels.py list-shows --channel anime
  python manage_channels.py add-show --channel anime --id test --search "关键词" --topic "名称"
  python manage_channels.py edit-show --id test --search "新关键词"
  python manage_channels.py delete-show --id test

  python manage_channels.py add-url --show-id mushenji --ep 69 --url "https://..."
  python manage_channels.py remove-url --show-id mushenji --ep 69
  python manage_channels.py list-urls --show-id mushenji

  python manage_channels.py show-state --show-id mushenji
  python manage_channels.py reset-state --show-id mushenji --episode-count 0
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config_loader import database_path, load_config, resolve_rel
from app.store import connect, ensure_schema, ensure_schema_v2, get_show_monitor_state, upsert_show_monitor_state

CONFIG_PATH = REPO_ROOT / "config.yaml"


def _read_cfg() -> dict:
    if CONFIG_PATH.is_file():
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return {}


def _write_cfg(cfg: dict) -> None:
    CONFIG_PATH.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"已写入 {CONFIG_PATH}")


def _resolve_urls_txt(show: dict) -> Path | None:
    """获取剧集 urls_file 的绝对路径。"""
    rel = str(show.get("urls_file") or "").strip()
    if not rel:
        return None
    return resolve_rel(REPO_ROOT, rel)


def _read_urls_txt(txt_path: Path) -> dict[int, str]:
    """读取 txt 文件，返回 {episode: url}。跳过空行和注释。"""
    urls: dict[int, str] = {}
    if not txt_path.is_file():
        return urls
    for i, line in enumerate(txt_path.read_text(encoding="utf-8").splitlines(), start=1):
        u = line.strip()
        if u and not u.startswith("#") and u.startswith("http"):
            urls[i] = u
    return urls


def _write_urls_txt(txt_path: Path, urls: dict[int, str]) -> None:
    """将 {episode: url} 写入 txt 文件（按集数排序，一行一个）。"""
    if not urls:
        txt_path.write_text("", encoding="utf-8")
        return
    max_ep = max(urls.keys())
    lines: list[str] = []
    for ep in range(1, max_ep + 1):
        lines.append(urls.get(ep, ""))
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ensure_monitor(cfg: dict) -> dict:
    if "monitor" not in cfg or not isinstance(cfg.get("monitor"), dict):
        cfg["monitor"] = {"channels": []}
    m = cfg["monitor"]
    if "channels" not in m or not isinstance(m.get("channels"), list):
        m["channels"] = []
    return cfg


# ---- 频道管理 ----

def cmd_list_channels(_args) -> int:
    cfg = _read_cfg()
    channels = (cfg.get("monitor") or {}).get("channels") or []
    if not channels:
        print("(无频道)")
        return 0
    for ch in channels:
        cid = ch.get("id", "?")
        cname = ch.get("name", cid)
        chat_id = ch.get("telegram_chat_id", "?")
        cover = ch.get("cover", "")
        shows = len(ch.get("shows") or [])
        cinfo = f" 封面={cover}" if cover else ""
        print(f"  [{cid}] {cname}  → chat_id={chat_id}  ({shows} 部剧){cinfo}")
    return 0


def cmd_add_channel(args) -> int:
    cfg = _read_cfg()
    cfg = _ensure_monitor(cfg)
    channels: list = cfg["monitor"]["channels"]
    cid = args.id.strip()
    if any(ch.get("id") == cid for ch in channels):
        print(f"频道 {cid} 已存在，请先 delete-channel")
        return 1
    entry = {
        "id": cid,
        "name": args.name.strip() or cid,
        "telegram_chat_id": args.chat_id.strip(),
        "sort_order": len(channels) + 1,
        "shows": [],
    }
    cover = args.cover.strip() if getattr(args, "cover", None) else ""
    if cover:
        entry["cover"] = cover
    channels.append(entry)
    _write_cfg(cfg)
    print(f"已添加频道: [{cid}] {args.name}")
    return 0


def cmd_delete_channel(args) -> int:
    cfg = _read_cfg()
    channels = (cfg.get("monitor") or {}).get("channels") or []
    new_list = [ch for ch in channels if ch.get("id") != args.id.strip()]
    if len(new_list) == len(channels):
        print(f"未找到频道: {args.id}")
        return 1
    cfg.setdefault("monitor", {})["channels"] = new_list
    _write_cfg(cfg)
    print(f"已删除频道: {args.id}")
    return 0


# ---- 剧集管理 ----

def _find_show(cfg: dict, show_id: str) -> tuple:
    """返回 (channel_dict, show_dict, show_index) 或 (None, None, -1)。"""
    channels = (cfg.get("monitor") or {}).get("channels") or []
    for ch in channels:
        shows = ch.get("shows") or []
        for i, s in enumerate(shows):
            if s.get("id") == show_id:
                return ch, s, i
    return None, None, -1


def cmd_list_shows(args) -> int:
    cfg = _read_cfg()
    channels = (cfg.get("monitor") or {}).get("channels") or []
    for ch in channels:
        if args.channel and ch.get("id") != args.channel.strip():
            continue
        cname = ch.get("name", ch.get("id", "?"))
        print(f"\n[{cname}]")
        shows = ch.get("shows") or []
        if not shows:
            print("  (无剧集)")
            continue
        for s in shows:
            sid = s.get("id", "?")
            kw = s.get("search_keyword", "?")
            topic = s.get("topic_name", sid)
            uf = s.get("urls_file", "")
            print(f"  {sid}  「{topic}」  搜索: {kw}  URL文件: {uf}")
    return 0


def cmd_add_show(args) -> int:
    cfg = _read_cfg()
    cfg = _ensure_monitor(cfg)
    channels = cfg["monitor"]["channels"]
    ch_id = args.channel.strip()

    target_ch = None
    for ch in channels:
        if ch.get("id") == ch_id:
            target_ch = ch
            break
    if target_ch is None:
        print(f"频道 {ch_id} 不存在，请先 add-channel")
        return 1

    shows: list = target_ch.setdefault("shows", [])
    sid = args.id.strip()
    if any(s.get("id") == sid for s in shows):
        print(f"剧集 {sid} 已存在于频道 {ch_id}")
        return 1

    urls_file = args.urls_file.strip() if getattr(args, "urls_file", None) else ""
    show = {
        "id": sid,
        "search_keyword": args.search.strip() or sid,
        "topic_name": args.topic.strip() or sid,
        "anime_prefix": args.prefix.strip() or f"{sid}第",
        "caption_file": args.caption.strip() or f"Telethon-FastUpload/{sid}.txt",
        "download_dir": args.download_dir.strip() or f"xiazai/downloads/{sid}",
        "sort_order": len(shows) + 1,
    }
    if urls_file:
        show["urls_file"] = urls_file
    shows.append(show)
    _write_cfg(cfg)
    print(f"已添加剧集 [{sid}] → 频道 [{ch_id}]  搜索关键词: {show['search_keyword']}")
    return 0


def cmd_edit_show(args) -> int:
    cfg = _read_cfg()
    ch, s, idx = _find_show(cfg, args.id.strip())
    if s is None:
        print(f"未找到剧集: {args.id}")
        return 1

    for field in ("search", "topic", "prefix", "caption", "download_dir", "urls_file"):
        val = getattr(args, field, None)
        if val is not None and str(val).strip():
            key_map = {
                "search": "search_keyword",
                "topic": "topic_name",
                "prefix": "anime_prefix",
                "caption": "caption_file",
            }
            k = key_map.get(field, field)
            s[k] = str(val).strip()
    _write_cfg(cfg)
    print(f"已更新剧集: {args.id}")
    return 0


def cmd_delete_show(args) -> int:
    cfg = _read_cfg()
    ch, s, idx = _find_show(cfg, args.id.strip())
    if s is None:
        print(f"未找到剧集: {args.id}")
        return 1
    ch["shows"].pop(idx)
    _write_cfg(cfg)
    print(f"已删除剧集: {args.id}")
    return 0


# ---- 手动 URL 管理（基于 txt 文件）----

def cmd_add_url(args) -> int:
    cfg = _read_cfg()
    _, s, _ = _find_show(cfg, args.show_id.strip())
    if s is None:
        print(f"未找到剧集: {args.show_id}")
        return 1

    txt_path = _resolve_urls_txt(s)
    if txt_path is None:
        print(f"剧集 {args.show_id} 未配置 urls_file，请先 edit-show --id {args.show_id} --urls-file <path>")
        return 1

    urls = _read_urls_txt(txt_path)
    urls[args.ep] = args.url.strip()
    _write_urls_txt(txt_path, urls)
    print(f"已添加手动 URL: [{args.show_id}] ep{args.ep} → {txt_path}")
    return 0


def cmd_remove_url(args) -> int:
    cfg = _read_cfg()
    _, s, _ = _find_show(cfg, args.show_id.strip())
    if s is None:
        print(f"未找到剧集: {args.show_id}")
        return 1

    txt_path = _resolve_urls_txt(s)
    if txt_path is None or not txt_path.is_file():
        print(f"URL 文件不存在: {txt_path}")
        return 1

    urls = _read_urls_txt(txt_path)
    if args.ep not in urls:
        print(f"未找到 ep{args.ep} 的 URL")
        return 1
    del urls[args.ep]
    _write_urls_txt(txt_path, urls)
    print(f"已删除手动 URL: [{args.show_id}] ep{args.ep}")
    return 0


def cmd_list_urls(args) -> int:
    cfg = _read_cfg()
    _, s, _ = _find_show(cfg, args.show_id.strip())
    if s is None:
        print(f"未找到剧集: {args.show_id}")
        return 1

    txt_path = _resolve_urls_txt(s)
    if txt_path is None:
        print("(未配置 urls_file)")
        return 0

    urls = _read_urls_txt(txt_path)
    if not urls:
        print("(无手动 URL)")
        return 0
    for ep in sorted(urls):
        print(f"  ep{ep}: {urls[ep]}")
    return 0


# ---- 监控状态 ----

def cmd_show_state(args) -> int:
    cfg = load_config()
    db_path = database_path(cfg)
    conn = connect(db_path)
    cur = conn.cursor()
    ensure_schema(cur)
    ensure_schema_v2(cur)
    conn.commit()

    sid = args.show_id.strip()
    state = get_show_monitor_state(conn, sid)
    conn.close()

    if state is None:
        print(f"剧集 {sid} 尚无监控状态记录")
        return 0
    print(f"  show_id:        {state['show_id']}")
    print(f"  channel_id:     {state['channel_id']}")
    print(f"  search_keyword: {state['search_keyword']}")
    print(f"  last_episode_count: {state['last_episode_count']}")
    print(f"  source_name:    {state['source_name']}")
    print(f"  title:          {state['title']}")
    print(f"  updated_at:     {state['updated_at']}")
    return 0


def cmd_reset_state(args) -> int:
    cfg = load_config()
    db_path = database_path(cfg)
    conn = connect(db_path)
    cur = conn.cursor()
    ensure_schema(cur)
    ensure_schema_v2(cur)
    conn.commit()

    sid = args.show_id.strip()
    upsert_show_monitor_state(
        conn,
        show_id=sid,
        channel_id="",
        search_keyword="",
        last_episode_count=args.episode_count,
    )
    conn.commit()
    conn.close()
    print(f"已重置监控状态: {sid} → episode_count={args.episode_count}")
    return 0


# ---- CLI ----

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="manage_channels v2.0 — 频道 & 剧集配置管理")
    sub = p.add_subparsers(dest="command", help="子命令")

    # 频道
    p_lc = sub.add_parser("list-channels", help="列出所有频道")

    p_ac = sub.add_parser("add-channel", help="添加频道")
    p_ac.add_argument("--id", required=True, help="频道 ID（如 anime）")
    p_ac.add_argument("--name", required=True, help="频道名称")
    p_ac.add_argument("--chat-id", required=True, help="Telegram chat_id")
    p_ac.add_argument("--cover", default="", help="频道封面图路径（可选）")

    p_dc = sub.add_parser("delete-channel", help="删除频道")
    p_dc.add_argument("--id", required=True, help="频道 ID")

    # 剧集
    p_ls = sub.add_parser("list-shows", help="列出剧集")
    p_ls.add_argument("--channel", default=None, help="按频道过滤")

    p_as = sub.add_parser("add-show", help="添加剧集")
    p_as.add_argument("--channel", required=True, help="所属频道 ID")
    p_as.add_argument("--id", required=True, help="剧集 ID")
    p_as.add_argument("--search", required=True, help="搜索关键词")
    p_as.add_argument("--topic", default="", help="显示名称（默认同 id）")
    p_as.add_argument("--prefix", default="", help="文件前缀")
    p_as.add_argument("--caption", default="", help="文案文件路径")
    p_as.add_argument("--download-dir", default="", help="下载目录")
    p_as.add_argument("--urls-file", default="", help="URL txt 文件路径（如 data/urls/anime/牧神记.txt）")

    p_es = sub.add_parser("edit-show", help="编辑剧集")
    p_es.add_argument("--id", required=True, help="剧集 ID")
    p_es.add_argument("--search", default=None, help="新搜索关键词")
    p_es.add_argument("--topic", default=None, help="新显示名称")
    p_es.add_argument("--prefix", default=None, help="新文件前缀")
    p_es.add_argument("--caption", default=None, help="新文案文件路径")
    p_es.add_argument("--urls-file", default=None, help="新 URL txt 文件路径")

    p_ds = sub.add_parser("delete-show", help="删除剧集")
    p_ds.add_argument("--id", required=True, help="剧集 ID")

    # 手动 URL
    p_au = sub.add_parser("add-url", help="添加手动 m3u8 URL")
    p_au.add_argument("--show-id", required=True, help="剧集 ID")
    p_au.add_argument("--ep", type=int, required=True, help="集数")
    p_au.add_argument("--url", required=True, help="m3u8 URL")

    p_ru = sub.add_parser("remove-url", help="删除手动 m3u8 URL")
    p_ru.add_argument("--show-id", required=True, help="剧集 ID")
    p_ru.add_argument("--ep", type=int, required=True, help="集数")

    p_lu = sub.add_parser("list-urls", help="列出手动 URL")
    p_lu.add_argument("--show-id", required=True, help="剧集 ID")

    # 状态
    p_ss = sub.add_parser("show-state", help="查看监控状态")
    p_ss.add_argument("--show-id", required=True, help="剧集 ID")

    p_rs = sub.add_parser("reset-state", help="重置监控状态")
    p_rs.add_argument("--show-id", required=True, help="剧集 ID")
    p_rs.add_argument("--episode-count", type=int, default=0, help="重置集数（默认 0）")

    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    dispatch = {
        "list-channels": cmd_list_channels,
        "add-channel": cmd_add_channel,
        "delete-channel": cmd_delete_channel,
        "list-shows": cmd_list_shows,
        "add-show": cmd_add_show,
        "edit-show": cmd_edit_show,
        "delete-show": cmd_delete_show,
        "add-url": cmd_add_url,
        "remove-url": cmd_remove_url,
        "list-urls": cmd_list_urls,
        "show-state": cmd_show_state,
        "reset-state": cmd_reset_state,
    }
    fn = dispatch.get(args.command)
    if fn:
        sys.exit(fn(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
