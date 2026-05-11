"""收藏更新 → m3u8 → 写入统一 SQLite →（可选）项目根 run.py --upload。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config_loader import database_path, load_config
from app.store import append_episode_urls, connect, ensure_schema
from jiankong.m3u8_resolve import resolve_new_episode_m3u8_urls
from jiankong.pipeline_config import load_item_key_to_show_id


def pipeline_enabled() -> bool:
    v = (os.environ.get("PIPELINE_ENABLED") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _item_key_allowed(item_key: str) -> bool:
    raw = (os.environ.get("PIPELINE_ITEM_KEYS") or "").strip()
    if not raw:
        return True
    allowed = {x.strip() for x in raw.split(",") if x.strip()}
    return item_key in allowed


def run_pipeline_for_changes(changes: list[dict]) -> None:
    if not pipeline_enabled() or not changes:
        return

    mapping = load_item_key_to_show_id()
    if not mapping:
        logging.warning(
            "PIPELINE_ENABLED 已开但未配置 ITEM_KEY_TO_SHOW_ID（jiankong/pipeline_config.py）"
        )
        return

    cfg = load_config()
    db_path = database_path(cfg)
    conn = connect(db_path)
    cur = conn.cursor()
    ensure_schema(cur)
    conn.commit()

    run_main = ROOT / "run.py"
    if not run_main.is_file():
        logging.error("找不到 %s", run_main)
        conn.close()
        return

    modified = False

    for c in changes:
        item_key = str(c.get("key") or "")
        if not item_key or not _item_key_allowed(item_key):
            continue
        show_id = mapping.get(item_key)
        if not show_id:
            logging.info("流水线跳过（未绑定 show_id）: %s", item_key)
            continue

        title = str(c.get("title") or "")
        display_name = str(c.get("display_name") or "") or title
        source_name = str(c.get("source_name") or "")
        source_id = str(c.get("source_id") or "")
        vod_id = str(c.get("vod_id") or "")
        try:
            old_total = int(c.get("oldTotal", 0))
            new_total = int(c.get("newTotal", 0))
        except (TypeError, ValueError):
            logging.error("变更条目集数无效: %s", c)
            continue

        episode_urls = resolve_new_episode_m3u8_urls(
            item_key=item_key,
            title=title,
            display_name=display_name,
            old_total=old_total,
            new_total=new_total,
            source_name=source_name,
            source_id=source_id,
            vod_id=vod_id,
        )
        if not episode_urls:
            logging.info("未得到 m3u8，跳过写入库: %s", display_name or item_key)
            continue

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
            continue

        if written:
            modified = True

    conn.close()

    skip_run = (os.environ.get("PIPELINE_SKIP_XIAZAI") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if skip_run:
        logging.info("PIPELINE_SKIP_XIAZAI 已设置，不调用 run.py")
        return

    if not modified:
        return

    logging.info("启动下载/上传: python run.py --upload")
    rc = subprocess.run(
        [sys.executable, str(run_main), "--upload"],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
    ).returncode
    if rc != 0:
        logging.error("run.py 退出码 %s", rc)
