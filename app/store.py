"""
统一 SQLite：收藏表（jiankong）+ 分番剧分集下载/上传状态。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Cursor) -> None:
    cur = conn
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fav_items (
          item_key TEXT PRIMARY KEY,
          total_episodes INTEGER NOT NULL,
          title TEXT,
          last_total INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fav_display_names (
          item_key TEXT PRIMARY KEY,
          display_name TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS show_profiles (
          show_id TEXT PRIMARY KEY,
          moon_item_key TEXT UNIQUE,
          topic_name TEXT NOT NULL,
          anime_prefix TEXT NOT NULL,
          caption_file TEXT NOT NULL,
          download_dir TEXT NOT NULL,
          sort_order INTEGER DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS episode_jobs (
          show_id TEXT NOT NULL,
          episode INTEGER NOT NULL,
          url TEXT,
          download_status TEXT DEFAULT '',
          upload_status TEXT DEFAULT '',
          updated_at TEXT,
          PRIMARY KEY (show_id, episode)
        )
        """
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_show_profile(
    conn: sqlite3.Connection,
    *,
    show_id: str,
    moon_item_key: str | None,
    topic_name: str,
    anime_prefix: str,
    caption_file: str,
    download_dir: str,
    sort_order: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO show_profiles (show_id, moon_item_key, topic_name, anime_prefix, caption_file, download_dir, sort_order)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(show_id) DO UPDATE SET
          moon_item_key=excluded.moon_item_key,
          topic_name=excluded.topic_name,
          anime_prefix=excluded.anime_prefix,
          caption_file=excluded.caption_file,
          download_dir=excluded.download_dir,
          sort_order=excluded.sort_order
        """,
        (
            show_id,
            moon_item_key or None,
            topic_name,
            anime_prefix,
            caption_file,
            download_dir,
            sort_order,
        ),
    )


def sync_episode_urls_from_config(
    conn: sqlite3.Connection, show_id: str, urls: list[str]
) -> None:
    """把 config 里的 urls 同步到 episode_jobs（覆盖该行 url）。"""
    for i, u in enumerate(urls, start=1):
        if not isinstance(u, str) or not u.strip():
            continue
        conn.execute(
            """
            INSERT INTO episode_jobs (show_id, episode, url, download_status, upload_status, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(show_id, episode) DO UPDATE SET
              url=excluded.url,
              updated_at=excluded.updated_at
            """,
            (show_id, i, u.strip(), "", "", utc_now_iso()),
        )


def append_episode_urls(
    conn: sqlite3.Connection,
    show_id: str,
    episode_to_url: dict[int, str],
    *,
    old_total_hint: int,
    new_total_hint: int,
) -> list[int]:
    written: list[int] = []
    for ep in sorted(episode_to_url.keys()):
        if ep <= old_total_hint or ep > new_total_hint:
            continue
        u = episode_to_url[ep].strip()
        if not u.startswith("http"):
            continue
        conn.execute(
            """
            INSERT INTO episode_jobs (show_id, episode, url, download_status, upload_status, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(show_id, episode) DO UPDATE SET
              url=excluded.url,
              download_status='pending',
              upload_status='',
              updated_at=excluded.updated_at
            """,
            (show_id, ep, u, "pending", "", utc_now_iso()),
        )
        written.append(ep)
    conn.commit()
    return written


def get_episode_row(
    conn: sqlite3.Connection, show_id: str, episode: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM episode_jobs WHERE show_id=? AND episode=?",
        (show_id, episode),
    ).fetchone()


def list_episodes_for_show(conn: sqlite3.Connection, show_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM episode_jobs WHERE show_id=? ORDER BY episode",
            (show_id,),
        ).fetchall()
    )


def set_episode_status(
    conn: sqlite3.Connection,
    show_id: str,
    episode: int,
    *,
    download_status: str | None = None,
    upload_status: str | None = None,
) -> None:
    sets: list[str] = []
    args: list[Any] = []
    if download_status is not None:
        sets.append("download_status=?")
        args.append(download_status)
    if upload_status is not None:
        sets.append("upload_status=?")
        args.append(upload_status)
    sets.append("updated_at=?")
    args.append(utc_now_iso())
    args.extend([show_id, episode])
    conn.execute(
        f"UPDATE episode_jobs SET {', '.join(sets)} WHERE show_id=? AND episode=?",
        args,
    )


def list_show_profiles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM show_profiles ORDER BY sort_order, show_id"
        ).fetchall()
    )
