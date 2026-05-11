"""
多番剧下载 + FastUpload 上传；分集状态读写 SQLite（app/store.py）。
下载不走代理；上传通过 TELEGRAM_PROXY=socks5（见 config.yaml proxy.upload）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import sqlite3

_pkg_root = Path(__file__).resolve().parent.parent
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from app.config_loader import database_path, load_config, resolve_rel
from app.paths import project_root

from app.store import (
    connect,
    ensure_schema,
    list_episodes_for_show,
    list_show_profiles,
    set_episode_status,
    sync_episode_urls_from_config,
    upsert_show_profile,
)


def _download_subprocess_env() -> dict[str, str]:
    e = os.environ.copy()
    for k in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        e.pop(k, None)
    return e


def _strip_dl_proxy(dl_cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(dl_cfg)
    out["custom_proxy"] = ""
    return out


def load_channel_message_template(path: Path) -> str:
    if not path.is_file():
        sys.exit(f"频道文案不存在: {path}")
    return path.read_text(encoding="utf-8").rstrip()


def build_upload_caption(template: str, episode: int, topic_name: str) -> str:
    line = f"🎬 {topic_name}第{episode}集"
    lines = template.splitlines()
    if not lines or not lines[0].strip().startswith("🎬"):
        return f"{line}\n{template}" if template.strip() else line
    lines[0] = line
    return "\n".join(lines)


def episode_filename(anime_prefix: str, episode: int) -> str:
    return f"{anime_prefix}{episode}集.mp4"


def _resolve_m3u8dl_executable(configured: str, xiazai_dir: Path) -> str:
    name = (configured or "N_m3u8DL-RE").strip() or "N_m3u8DL-RE"
    p = Path(name)
    if p.is_file():
        return str(p.resolve())
    if not p.is_absolute() and (xiazai_dir / p.name).is_file():
        return str((xiazai_dir / p.name).resolve())
    for fname in (name, f"{name}.exe") if sys.platform == "win32" and not name.lower().endswith(".exe") else (name,):
        lp = xiazai_dir / fname
        if lp.is_file():
            return str(lp.resolve())
    w = shutil.which(name)
    if w:
        return w
    sys.exit(
        f"未找到 N_m3u8DL-RE，请将可执行文件放到 {xiazai_dir} 或 PATH。"
    )


def _m3u8_header_args(dl_cfg: dict[str, Any]) -> list[str]:
    args: list[str] = []
    ua = str(dl_cfg.get("user_agent") or "").strip()
    if ua:
        args += ["--header", f"User-Agent: {ua}"]
    referer = str(dl_cfg.get("referer") or "").strip()
    if referer:
        args += ["--header", f"Referer: {referer}"]
    cookie = str(dl_cfg.get("cookie") or "").strip()
    if cookie:
        args += ["--header", f"Cookie: {cookie}"]
    extra = str(dl_cfg.get("headers") or "").strip()
    if extra:
        for ln in extra.replace("\\n", "\n").splitlines():
            if ln.strip():
                args += ["--header", ln.strip()]
    return args


def _normalize_m3u8_url(url: str) -> str:
    s = (url or "").strip()
    if not s:
        return s
    try:
        p = urlsplit(s)
        if p.scheme not in ("http", "https") or not p.netloc:
            return s
        new_path = quote(unquote(p.path), safe="/")
        return urlunsplit((p.scheme, p.netloc, new_path, p.query, p.fragment))
    except Exception:
        return s


def download_m3u8_re(
    url: str,
    out_path: Path,
    dl_cfg: dict[str, Any],
    *,
    working_dir: Path,
    clean_proxy: bool,
) -> None:
    dl_cfg = _strip_dl_proxy(dl_cfg)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    exe = _resolve_m3u8dl_executable(str(dl_cfg.get("executable") or "N_m3u8DL-RE"), working_dir)
    url = _normalize_m3u8_url(url)
    cmd: list[str] = [
        exe,
        url,
        "--save-dir",
        str(out_path.parent.resolve()),
        "--save-name",
        out_path.stem,
    ]
    tmp_dir = str(dl_cfg.get("tmp_dir") or "").strip()
    if tmp_dir:
        p = Path(tmp_dir)
        if not p.is_absolute():
            p = working_dir / p
        p.mkdir(parents=True, exist_ok=True)
        cmd += ["--tmp-dir", str(p.resolve())]
    t = str(dl_cfg.get("http_request_timeout") or "").strip()
    if t:
        cmd += ["--http-request-timeout", t]
    rc = str(dl_cfg.get("download_retry_count") or "").strip()
    if rc:
        cmd += ["--download-retry-count", rc]
    tc = str(dl_cfg.get("thread_count") or "").strip()
    if tc:
        cmd += ["--thread-count", tc]
    if bool(dl_cfg.get("disable_update_check", True)):
        cmd.append("--disable-update-check")
    log_level = str(dl_cfg.get("log_level") or "ERROR").strip()
    if log_level:
        cmd += ["--log-level", log_level]
    if bool(dl_cfg.get("auto_select", True)):
        cmd.append("--auto-select")
    ff = str(dl_cfg.get("ffmpeg_binary_path") or "").strip()
    if ff:
        cmd += ["--ffmpeg-binary-path", ff]
    if bool(dl_cfg.get("binary_merge", False)):
        cmd.append("--binary-merge")
    if bool(dl_cfg.get("use_ffmpeg_concat_demuxer", False)):
        cmd.append("--use-ffmpeg-concat-demuxer")
    cmd += _m3u8_header_args(dl_cfg)
    extra_args = dl_cfg.get("extra_args")
    if isinstance(extra_args, list):
        for a in extra_args:
            if isinstance(a, str) and a.strip():
                cmd.append(a.strip())
    elif isinstance(extra_args, str) and extra_args.strip():
        cmd.append(extra_args.strip())

    env = _download_subprocess_env() if clean_proxy else os.environ.copy()
    subprocess.run(cmd, check=True, cwd=str(working_dir), stdin=subprocess.DEVNULL, env=env)
    if not out_path.is_file():
        raise RuntimeError(f"下载完成但未找到输出: {out_path}")


def _delete_local_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        print(f"delete failed {path}: {e}", file=sys.stderr)


def resolve_cover(cfg: dict[str, Any], root: Path) -> Path:
    paths = cfg.get("paths") or {}
    rel = str(paths.get("cover") or "Telethon-FastUpload/cover.jpeg").strip()
    p = resolve_rel(root, rel)
    if not p.is_file():
        sys.exit(f"封面不存在: {p}")
    return p


def seed_shows(conn: sqlite3.Connection, cfg: dict[str, Any]) -> None:
    shows = cfg.get("shows") or []
    if not isinstance(shows, list):
        return
    for i, s in enumerate(shows):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip()
        if not sid:
            continue
        upsert_show_profile(
            conn,
            show_id=sid,
            moon_item_key=str(s.get("moon_item_key") or "").strip() or None,
            topic_name=str(s.get("topic_name") or sid),
            anime_prefix=str(s.get("anime_prefix") or ""),
            caption_file=str(s.get("caption_file") or ""),
            download_dir=str(s.get("download_dir") or f"xiazai/downloads/{sid}"),
            sort_order=int(s.get("sort_order") or i),
        )
        urls = s.get("urls")
        if isinstance(urls, list) and urls:
            sync_episode_urls_from_config(conn, sid, [str(u) for u in urls if str(u).strip()])
    conn.commit()


def run_download_upload(upload_enabled_override: bool | None = None) -> None:
    root = project_root()
    cfg = load_config()
    db_path = database_path(cfg)
    conn = connect(db_path)
    cur = conn.cursor()
    ensure_schema(cur)
    conn.commit()

    seed_shows(conn, cfg)

    runtime = cfg.get("runtime") or {}
    upload_enabled = bool(runtime.get("upload_enabled", True))
    if upload_enabled_override is not None:
        upload_enabled = upload_enabled_override
    upload_retries = int(runtime.get("upload_retries", 5))

    m3u8dl_re_cfg = cfg.get("m3u8dl_re") or {}
    if not isinstance(m3u8dl_re_cfg, dict):
        m3u8dl_re_cfg = {}

    download_use_clean_proxy = not bool((cfg.get("proxy") or {}).get("download", {}).get("enabled", False))

    xiazai_dir = root / "xiazai"
    cover_path = resolve_cover(cfg, root)

    profiles = list_show_profiles(conn)
    if not profiles:
        print("show_profiles 为空，请在 config.yaml 的 shows 下配置番剧。", file=sys.stderr)
        return

    print(
        f"数据库: {db_path}\n"
        f"封面: {cover_path}\n"
        f"模式: {'下载+上传' if upload_enabled else '仅下载'}"
    )

    for prof in profiles:
        show_id = str(prof["show_id"])
        topic_name = str(prof["topic_name"])
        anime_prefix = str(prof["anime_prefix"])
        cap_rel = str(prof["caption_file"])
        dl_rel = str(prof["download_dir"])

        caption_path = resolve_rel(root, cap_rel)
        download_dir = resolve_rel(root, dl_rel)
        download_dir.mkdir(parents=True, exist_ok=True)

        if caption_path.is_file():
            cap_template = load_channel_message_template(caption_path)
        else:
            print(f"  警告: 文案文件不存在 {caption_path}，上传仅用自动生成标题行")
            cap_template = ""

        rows = list_episodes_for_show(conn, show_id)
        if not rows:
            print(f"[{show_id}] 无分集记录，跳过")
            continue

        print(f"\n=== {show_id} ({topic_name}) 共 {len(rows)} 条分集记录 ===")

        for row in rows:
            ep = int(row["episode"])
            url = (row["url"] or "").strip()
            if not url:
                continue
            ds = str(row["download_status"] or "")
            us = str(row["upload_status"] or "")

            if us == "uploaded":
                print(f"  skip 已上传 {show_id} ep{ep}")
                continue

            out_path = download_dir / episode_filename(anime_prefix, ep)

            if ds == "downloaded" and not out_path.is_file():
                print(f"  skip ep{ep}: 标记已下载但文件缺失")
                continue

            if not out_path.is_file():
                set_episode_status(conn, show_id, ep, download_status="downloading")
                conn.commit()
                print(f"  download {show_id} ep{ep}/{len(rows)}")
                try:
                    download_m3u8_re(
                        url,
                        out_path,
                        m3u8dl_re_cfg,
                        working_dir=xiazai_dir,
                        clean_proxy=download_use_clean_proxy,
                    )
                    set_episode_status(conn, show_id, ep, download_status="downloaded")
                    conn.commit()
                except (subprocess.CalledProcessError, RuntimeError) as e:
                    set_episode_status(conn, show_id, ep, download_status="download_failed")
                    conn.commit()
                    _delete_local_file(out_path)
                    print(f"  ep{ep} 下载失败: {e}", file=sys.stderr)
                    continue
            else:
                if ds != "downloaded":
                    set_episode_status(conn, show_id, ep, download_status="downloaded")
                    conn.commit()
                print(f"  已有文件 ep{ep} -> {out_path.name}")

            if not upload_enabled:
                continue

            if not out_path.is_file():
                continue

            if cap_template:
                caption = build_upload_caption(cap_template, ep, topic_name)
            else:
                caption = f"🎬 {topic_name}第{ep}集"

            ok = False
            for attempt in range(1, upload_retries + 1):
                print(f"  upload ep{ep} (attempt {attempt})")
                try:
                    import asyncio
                    ok = asyncio.run(
                        upload_via_telegram_manager(
                            file_path=out_path,
                            caption=caption,
                            thumb_path=cover_path,
                        )
                    )
                    if ok:
                        break
                except Exception as e:
                    print(f"  upload ep{ep} error: {e}", file=sys.stderr)
                set_episode_status(conn, show_id, ep, upload_status="upload_failed")
                conn.commit()
                if attempt < upload_retries:
                    time.sleep(5)

            if ok:
                set_episode_status(conn, show_id, ep, upload_status="uploaded")
                conn.commit()
                _delete_local_file(out_path)
                print(f"  done ep{ep} uploaded")
            else:
                print(f"  ep{ep} upload 失败", file=sys.stderr)

    conn.close()



async def upload_via_telegram_manager(
    file_path: Path,
    caption: str,
    *,
    target: str | None = None,
    thumb_path: Path | None = None,
) -> bool:
    """
    使用统一 TelegramManager 直接上传（不走子进程）。
    比子进程方式更快，且支持更好的错误处理。
    """
    from telegram_manager import TelegramManager

    mgr = TelegramManager()
    try:
        await mgr.login()
        await mgr.upload_video(
            file_path=file_path,
            caption=caption,
            target=target,
            thumb_path=str(thumb_path) if thumb_path else None,
        )
        return True
    except Exception as e:
        print(f"上传失败: {e}", file=sys.stderr)
        return False
    finally:
        await mgr.disconnect()
