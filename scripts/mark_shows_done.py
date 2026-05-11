"""
批量将 episode_jobs 标为「已下载 + 已上传」。
无需安装 sqlite3 命令行，用项目自带 Python + SQLite。

用法（在项目根目录）:
  python scripts/mark_shows_done.py --show-id zetianji --show-id zhetian
  python scripts/mark_shows_done.py --show-id zetianji,zhetian,mushenji
  python scripts/mark_shows_done.py --all-shows --skip panlong
  python scripts/mark_shows_done.py --all-shows --dry-run

与 config.yaml -> database.path 使用同一库（默认 data/app_state.sqlite）。

默认会先把 config 里该剧的 urls 写入 episode_jobs（与 run.py 的同步逻辑一致），再标成已下载+已上传；
若只想改已有行、不要插入：加 --no-sync。

PowerShell：请勿写 --show-id mushenji,（末尾逗号会引发语法错误或未传入参数），请用：
  --show-id mushenji
或：
  --show-id "mushenji,zetianji"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import unicodedata
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config_loader import database_path, load_config
from app.store import (
    connect,
    ensure_schema,
    sync_episode_urls_from_config,
    upsert_show_profile,
    utc_now_iso,
)


def _norm_show_id(s: str) -> str:
    """去掉首尾空白、中英文逗号；统一 Unicode（避免命令行里看起来像 mushenji 的全角字符）。"""
    t = unicodedata.normalize("NFKC", (s or "").strip())
    return t.strip(",").strip("，").strip()


def _parse_csv(s: str) -> list[str]:
    out: list[str] = []
    for x in s.replace("，", ",").split(","):
        t = _norm_show_id(x)
        if t:
            out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="批量 update episode_jobs：download_status=downloaded, upload_status=uploaded"
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config.yaml 路径（默认项目根目录 config.yaml）",
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--show-id",
        action="append",
        dest="show_ids",
        metavar="ID",
        help="番剧 id，可写多次，或用逗号分隔多个",
    )
    g.add_argument(
        "--all-shows",
        action="store_true",
        help="config.yaml 里 shows 下出现的全部 id",
    )
    ap.add_argument(
        "--skip",
        default="",
        help="在 --all-shows 时跳过的 id，逗号分隔，例如: panlong",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将更新哪些 id，不写库",
    )
    ap.add_argument(
        "--no-sync",
        action="store_true",
        help="不要从 config 同步 urls 到 episode_jobs（仅改已有行；无行则仍为 0）",
    )
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except yaml.parser.ParserError as e:
        print(
            "config.yaml 解析失败：多为 YAML 缩进错误（例如某部证的 `- id:` 写进了上一部的 `urls:` 列表里）。",
            file=sys.stderr,
        )
        print(e, file=sys.stderr)
        return 1
    except yaml.YAMLError as e:
        print("config.yaml YAML 格式错误:", e, file=sys.stderr)
        return 1
    db_path = database_path(cfg)
    if not db_path.is_file():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 1

    targets: list[str] = []
    if args.all_shows:
        shows = cfg.get("shows") or []
        if not isinstance(shows, list):
            print("config shows 不是列表", file=sys.stderr)
            return 1
        for s in shows:
            if isinstance(s, dict) and str(s.get("id") or "").strip():
                targets.append(_norm_show_id(str(s["id"]).strip()))
        skip = set(_parse_csv(args.skip))
        targets = [t for t in targets if t not in skip]
    else:
        for x in args.show_ids or []:
            targets.extend(_parse_csv(x))
        # 去重保序
        seen: set[str] = set()
        targets = [t for t in targets if not (t in seen or seen.add(t))]

    targets = [_norm_show_id(t) for t in targets]
    targets = [t for t in targets if t]

    if not targets:
        print("没有要更新的 show_id", file=sys.stderr)
        return 1

    if args.dry_run:
        print("dry-run，将更新:", ", ".join(targets))
        print("数据库:", db_path)
        return 0

    shows_list = cfg.get("shows") or []
    if not isinstance(shows_list, list):
        shows_list = []

    def _config_show_ids() -> list[str]:
        ids: list[str] = []
        for s in shows_list:
            if isinstance(s, dict):
                raw = str(s.get("id") or "").strip()
                if raw:
                    ids.append(_norm_show_id(raw))
        return ids

    def _find_show(sid: str) -> dict | None:
        want = _norm_show_id(sid)
        for s in shows_list:
            if isinstance(s, dict):
                raw = str(s.get("id") or "").strip()
                if _norm_show_id(raw) == want:
                    return s
        return None

    now = utc_now_iso()
    conn = connect(db_path)
    try:
        cur = conn.cursor()
        ensure_schema(cur)
        conn.commit()

        skipped_no_sync: set[str] = set()

        if not args.no_sync:
            for sid in targets:
                row = _find_show(sid)
                if row is None:
                    avail = _config_show_ids()
                    hint = (
                        f"config 里已有 id（节选）: {', '.join(avail[:25])}"
                        + (" …" if len(avail) > 25 else "")
                    )
                    print(
                        f"{sid}: 跳过同步 — config.yaml 的 shows 里找不到该 id（请核对拼写，勿在末尾加多余逗号）。{hint}",
                        file=sys.stderr,
                    )
                    skipped_no_sync.add(sid)
                    continue
                urls = row.get("urls")
                if not isinstance(urls, list) or not any(
                    isinstance(u, str) and u.strip() for u in urls
                ):
                    print(
                        f"{sid}: 跳过同步（config 里 urls 为空，无法写入 episode_jobs）",
                        file=sys.stderr,
                    )
                    skipped_no_sync.add(sid)
                    continue
                url_list = [str(u).strip() for u in urls if isinstance(u, str) and u.strip()]
                try:
                    upsert_show_profile(
                        conn,
                        show_id=sid,
                        topic_name=str(row.get("topic_name") or sid),
                        anime_prefix=str(row.get("anime_prefix") or ""),
                        caption_file=str(row.get("caption_file") or ""),
                        download_dir=str(row.get("download_dir") or f"xiazai/downloads/{sid}"),
                        urls_file=str(row.get("urls_file") or "").strip(),
                        sort_order=int(row.get("sort_order") or 0),
                    )
                    sync_episode_urls_from_config(conn, sid, url_list)
                except sqlite3.IntegrityError as e:
                    print(
                        f"{sid}: 写入 show_profiles / episode_jobs 失败。{e}",
                        file=sys.stderr,
                    )
                    skipped_no_sync.add(sid)
                    continue
                print(f"{sid}: 已从 config 同步 {len(url_list)} 条分集 URL → episode_jobs")
            conn.commit()

        for sid in targets:
            cur = conn.execute(
                """
                UPDATE episode_jobs
                SET download_status = ?, upload_status = ?, updated_at = ?
                WHERE show_id = ?
                """,
                ("downloaded", "uploaded", now, sid),
            )
            n = cur.rowcount
            extra = ""
            if n == 0 and sid in skipped_no_sync:
                extra = "（未写入分集行：上面同步已跳过，episode_jobs 里可能还没有该剧记录）"
            elif n == 0:
                extra = "（库中无该 show_id 的分集行；可加 --no-sync 排查是否从未同步过 urls）"
            print(f"{sid}: 已标记已下载+已上传 {n} 条分集{extra}")
        conn.commit()

        # 二次校验（避免 GUI 工具路径不一致；请看 episode_jobs 表，不是 show_profiles）
        print("\n--- 写入后校验（episode_jobs）---")
        abs_db = db_path.resolve()
        for sid in targets:
            total = conn.execute(
                "SELECT COUNT(*) FROM episode_jobs WHERE show_id = ?",
                (sid,),
            ).fetchone()[0]
            ok = conn.execute(
                """
                SELECT COUNT(*) FROM episode_jobs
                WHERE show_id = ?
                  AND IFNULL(download_status, '') = 'downloaded'
                  AND IFNULL(upload_status, '') = 'uploaded'
                """,
                (sid,),
            ).fetchone()[0]
            print(
                f"  {sid}: 共 {total} 条；其中 download_status=downloaded 且 upload_status=uploaded 的有 {ok} 条"
            )
        print(f"\n若图形工具里看不到更新：请确认打开的是同一文件（复制路径）：\n  {abs_db}\n并在工具里刷新/关闭重开；表名 episode_jobs。")
    finally:
        conn.close()

    print("\n完成。数据库:", db_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
