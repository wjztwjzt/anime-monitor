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
        CREATE TABLE IF NOT EXISTS show_profiles (
          show_id TEXT PRIMARY KEY,
          topic_name TEXT NOT NULL,
          anime_prefix TEXT NOT NULL,
          caption_file TEXT NOT NULL,
          download_dir TEXT NOT NULL,
          urls_file TEXT DEFAULT '',
          sort_order INTEGER DEFAULT 0,
          channel_id TEXT DEFAULT ''
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
          channel_id TEXT DEFAULT '',
          PRIMARY KEY (show_id, episode)
        )
        """
    )


def ensure_schema_v2(cur: sqlite3.Cursor) -> None:
    """v2.0 新增表 + 旧表补列（幂等）。"""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
          channel_id TEXT PRIMARY KEY,
          channel_name TEXT NOT NULL,
          telegram_chat_id TEXT NOT NULL,
          channel_type TEXT DEFAULT '',
          cover TEXT DEFAULT '',
          sort_order INTEGER DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS show_monitor_state (
          show_id TEXT PRIMARY KEY,
          channel_id TEXT NOT NULL DEFAULT '',
          search_keyword TEXT NOT NULL,
          last_episode_count INTEGER NOT NULL DEFAULT 0,
          source_name TEXT DEFAULT '',
          source_id TEXT DEFAULT '',
          vod_id TEXT DEFAULT '',
          title TEXT DEFAULT '',
          updated_at TEXT
        )
        """
    )
    for table, col_def in [
        ("show_profiles", "channel_id TEXT DEFAULT ''"),
        ("episode_jobs", "channel_id TEXT DEFAULT ''"),
        ("channels", "cover TEXT DEFAULT ''"),
        ("show_monitor_state", "channel_latest_ep INTEGER NOT NULL DEFAULT 0"),
        ("show_monitor_state", "channel_ep_checked_at TEXT NOT NULL DEFAULT ''"),
    ]:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_show_profile(
    conn: sqlite3.Connection,
    *,
    show_id: str,
    topic_name: str,
    anime_prefix: str,
    caption_file: str,
    download_dir: str,
    urls_file: str = "",
    sort_order: int = 0,
    channel_id: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO show_profiles (show_id, topic_name, anime_prefix, caption_file, download_dir, urls_file, sort_order, channel_id)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(show_id) DO UPDATE SET
          topic_name=excluded.topic_name,
          anime_prefix=excluded.anime_prefix,
          caption_file=excluded.caption_file,
          download_dir=excluded.download_dir,
          urls_file=excluded.urls_file,
          sort_order=excluded.sort_order,
          channel_id=excluded.channel_id
        """,
        (
            show_id,
            topic_name,
            anime_prefix,
            caption_file,
            download_dir,
            urls_file,
            sort_order,
            channel_id,
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


# ---- v2.0: 频道 & 监控状态 CRUD ----


def upsert_channel(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    channel_name: str,
    telegram_chat_id: str,
    channel_type: str = "",
    cover: str = "",
    sort_order: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO channels (channel_id, channel_name, telegram_chat_id, channel_type, cover, sort_order)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(channel_id) DO UPDATE SET
          channel_name=excluded.channel_name,
          telegram_chat_id=excluded.telegram_chat_id,
          channel_type=excluded.channel_type,
          cover=excluded.cover,
          sort_order=excluded.sort_order
        """,
        (channel_id, channel_name, telegram_chat_id, channel_type, cover, sort_order),
    )


def delete_channel(conn: sqlite3.Connection, channel_id: str) -> None:
    conn.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))


def list_channels(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM channels ORDER BY sort_order, channel_id").fetchall()
    )


def get_channel_by_id(conn: sqlite3.Connection, channel_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM channels WHERE channel_id=?", (channel_id,)
    ).fetchone()


def upsert_show_monitor_state(
    conn: sqlite3.Connection,
    *,
    show_id: str,
    channel_id: str = "",
    search_keyword: str,
    last_episode_count: int = 0,
    source_name: str = "",
    source_id: str = "",
    vod_id: str = "",
    title: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO show_monitor_state (show_id, channel_id, search_keyword, last_episode_count, source_name, source_id, vod_id, title, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(show_id) DO UPDATE SET
          channel_id=excluded.channel_id,
          search_keyword=excluded.search_keyword,
          last_episode_count=excluded.last_episode_count,
          source_name=excluded.source_name,
          source_id=excluded.source_id,
          vod_id=excluded.vod_id,
          title=excluded.title,
          updated_at=excluded.updated_at
        """,
        (show_id, channel_id, search_keyword, last_episode_count, source_name, source_id, vod_id, title, utc_now_iso()),
    )


def get_show_monitor_state(conn: sqlite3.Connection, show_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM show_monitor_state WHERE show_id=?", (show_id,)
    ).fetchone()


def update_show_monitor_telegram_cache(
    conn: sqlite3.Connection,
    show_id: str,
    *,
    channel_latest_ep: int,
    channel_ep_checked_at: str,
) -> None:
    """写入 Telethon 扫频道得到的最新集数及扫描时间（不改动 last_episode_count）。"""
    conn.execute(
        """
        UPDATE show_monitor_state
        SET channel_latest_ep = ?, channel_ep_checked_at = ?
        WHERE show_id = ?
        """,
        (int(channel_latest_ep), channel_ep_checked_at, show_id),
    )


def bump_show_monitor_channel_latest_ep(
    conn: sqlite3.Connection,
    show_id: str,
    episode: int,
) -> None:
    """上传成功后，将频道侧已确认集数抬到不低于本集。"""
    conn.execute(
        """
        UPDATE show_monitor_state
        SET channel_latest_ep = MAX(COALESCE(channel_latest_ep, 0), ?)
        WHERE show_id = ?
        """,
        (int(episode), show_id),
    )


def list_show_monitor_states(
    conn: sqlite3.Connection, channel_id: str | None = None
) -> list[sqlite3.Row]:
    if channel_id:
        return list(
            conn.execute(
                "SELECT * FROM show_monitor_state WHERE channel_id=? ORDER BY show_id",
                (channel_id,),
            ).fetchall()
        )
    return list(
        conn.execute("SELECT * FROM show_monitor_state ORDER BY channel_id, show_id").fetchall()
    )
