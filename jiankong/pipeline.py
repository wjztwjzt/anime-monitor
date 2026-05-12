"""收藏更新 → m3u8 → 写入统一 SQLite →（可选）项目根 run.py --upload。"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config_loader import database_path, load_config
from app.store import append_episode_urls, connect, ensure_schema
from jiankong.m3u8_resolve import resolve_new_episode_m3u8_urls


def pipeline_enabled() -> bool:
    v = (os.environ.get("PIPELINE_ENABLED") or "").strip().lower()
    return v in ("1", "true", "yes", "on")



def _resolve_episode_urls(c: dict) -> dict[int, str] | None:
    """从变更字典解析 m3u8 URL（V2 快通道优先，回退 V1 m3u8_resolve）。"""
    show_id = str(c.get("show_id") or "")
    display_name = str(c.get("display_name") or "")
    title = str(c.get("title") or "")
    search_kw = str(c.get("search_keyword") or "")
    source_name = str(c.get("source_name") or "")
    source_id = str(c.get("source_id") or "")
    vod_id = str(c.get("vod_id") or "")

    # V2 快通道
    episode_urls_direct = c.get("_episode_urls_direct")
    if episode_urls_direct:
        urls = {int(k): str(v) for k, v in episode_urls_direct.items()}
        logging.info(
            "V2 快通道: %s 跳过 m3u8 解析，直接使用 %s 集 URL",
            display_name or show_id, len(urls),
        )
        return urls

    # V1 回退
    item_key = str(c.get("key") or f"{source_id}+{vod_id}")
    try:
        old_total = int(c.get("old_total") or c.get("oldTotal", 0))
        new_total = int(c.get("new_total") or c.get("newTotal", 0))
    except (TypeError, ValueError):
        return None
    return resolve_new_episode_m3u8_urls(
        item_key=item_key,
        title=title,
        display_name=search_kw or display_name,
        old_total=old_total,
        new_total=new_total,
        source_name=source_name,
        source_id=source_id,
        vod_id=vod_id,
    )


def run_pipeline_for_change(c: dict) -> bool:
    """处理单个变更：写入 episode_jobs + 触发下载上传。

    返回 True 表示有新任务写入并已触发下载。
    """
    if not pipeline_enabled():
        return False

    show_id = str(c.get("show_id") or "")
    channel_id = str(c.get("channel_id") or "")
    display_name = str(c.get("display_name") or "") or c.get("title", "")
    if not show_id:
        return False

    try:
        old_total = int(c.get("old_total") or c.get("oldTotal", 0))
        new_total = int(c.get("new_total") or c.get("newTotal", 0))
    except (TypeError, ValueError):
        logging.error("变更条目集数无效: %s", c)
        return False

    episode_urls = _resolve_episode_urls(c)
    if not episode_urls:
        logging.info("未得到 m3u8，跳过: %s", display_name or show_id)
        return False

    cfg = load_config()
    db_path = database_path(cfg)
    conn = connect(db_path)
    cur = conn.cursor()
    ensure_schema(cur)
    try:
        from app.store import ensure_schema_v2
        ensure_schema_v2(cur)
    except ImportError:
        pass
    conn.commit()

    try:
        written = append_episode_urls(
            conn,
            show_id,
            episode_urls,
            old_total_hint=old_total,
            new_total_hint=new_total,
        )
    except Exception:
        logging.exception("写入 episode_jobs 失败 show_id=%s", show_id)
        conn.close()
        return False

    if written and channel_id:
        for ep in written:
            conn.execute(
                "UPDATE episode_jobs SET channel_id=? WHERE show_id=? AND episode=?",
                (channel_id, show_id, ep),
            )
        conn.commit()

    conn.close()

    skip_run = (os.environ.get("PIPELINE_SKIP_XIAZAI") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if skip_run:
        logging.info("PIPELINE_SKIP_XIAZAI 已设置，不执行下载上传")
        return bool(written)

    if not written:
        return False

    logging.info("启动下载/上传: %s", display_name or show_id)
    try:
        from app.download_worker import run_download_upload
        run_download_upload(upload_enabled_override=True)
    except Exception:
        logging.exception("下载/上传流程异常: %s", display_name or show_id)

    return True


def run_pipeline_for_changes(changes: list[dict]) -> None:
    """v2.0: 接受 config_monitor 的变更字典（批量模式，顺序处理）。

    单变更场景推荐用 run_pipeline_for_change() 支持并行。
    """
    for c in changes:
        run_pipeline_for_change(c)
