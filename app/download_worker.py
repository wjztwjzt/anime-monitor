"""
多番剧下载 + Telegram 上传（Telethon + 可选 FastTelethon 分片）；分集状态读写 SQLite（app/store.py）。
下载不走代理；上传走 config.yaml proxy.upload（socks5）。
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
    upsert_show_profile,
    utc_now_iso,
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


def _count_planned_uploads(
    rows: list[Any],
    download_dir: Path,
    anime_prefix: str,
    upload_enabled: bool,
) -> int:
    """本剧本轮可能上传的集数（用于总进度分母；含尚未下载的集）。"""
    if not upload_enabled:
        return 0
    n = 0
    for row in rows:
        url = (row["url"] or "").strip()
        if not url:
            continue
        if str(row["upload_status"] or "") == "uploaded":
            continue
        ds = str(row["download_status"] or "")
        ep = int(row["episode"])
        op = download_dir / episode_filename(anime_prefix, ep)
        if ds == "downloaded" and not op.is_file():
            continue
        n += 1
    return n


def _make_show_upload_progress_cb(
    *,
    show_id: str,
    topic_name: str,
    ep: int,
    slot: int,
    n_planned: int,
    file_label: str,
) -> Any:
    """单文件 + 本剧多集上传总进度（按集加权），输出到 stderr 并 flush。"""
    if n_planned <= 0:
        return None

    last_t = [0.0]

    def cb(current: int, total: int) -> None:
        now = time.monotonic()
        total = total or 1
        if now - last_t[0] < 0.15 and current < total:
            return
        last_t[0] = now
        file_pct = 100.0 * current / total
        overall = 100.0 * (slot + current / total) / n_planned
        mb = 1024 * 1024
        sys.stderr.write(
            f"\r  [{show_id}] {topic_name} 第{ep}集  文件{file_pct:5.1f}%  "
            f"本剧总进度{overall:5.1f}% ({slot + 1}/{n_planned})  "
            f"{current / mb:.1f}/{total / mb:.1f} MB  {file_label[:30]}"
        )
        sys.stderr.flush()
        if current >= total:
            sys.stderr.write("\n")
            sys.stderr.flush()

    return cb


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


def resolve_cover(path: str | None, cfg: dict[str, Any], root: Path) -> Path:
    """按指定路径或全局 paths.cover 解析封面。"""
    if path and str(path).strip():
        p = resolve_rel(root, str(path).strip())
        if p.is_file():
            return p
    paths = cfg.get("paths") or {}
    rel = str(paths.get("cover") or "Telethon-FastUpload/cover.jpeg").strip()
    p = resolve_rel(root, rel)
    if not p.is_file():
        sys.exit(f"封面不存在: {p}")
    return p


def _load_channel_target_map(cfg: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """v2.0: 构建 show_id → telegram_chat_id 和 show_id → cover 映射（来自 monitor.channels）。"""
    target_map: dict[str, str] = {}
    cover_map: dict[str, str] = {}
    default_target = str((cfg.get("telegram") or {}).get("target") or "").strip()

    channels = (cfg.get("monitor") or {}).get("channels") or []
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        ch_target = str(ch.get("telegram_chat_id") or "").strip()
        ch_cover = str(ch.get("cover") or "").strip()
        ch_id = str(ch.get("id") or "").strip()
        shows = ch.get("shows") or []
        for s in shows:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or "").strip()
            if sid:
                target_map[sid] = ch_target or default_target
                if ch_cover:
                    cover_map[sid] = ch_cover
    return target_map, cover_map


def _load_urls_from_txt(txt_path: Path) -> dict[int, str]:
    """从 txt 文件读取 URL，每行一集（行号=集数）。跳过空行和注释行。"""
    urls: dict[int, str] = {}
    if not txt_path.is_file():
        return urls
    for i, line in enumerate(txt_path.read_text(encoding="utf-8").splitlines(), start=1):
        u = line.strip()
        if u and not u.startswith("#") and u.startswith("http"):
            urls[i] = u
    return urls


def seed_shows(conn: sqlite3.Connection, cfg: dict[str, Any], root: Path) -> None:
    """v2.0: 从 monitor.channels 写入 show_profiles，从 txt 文件加载 URL 写入 episode_jobs。"""
    channels = (cfg.get("monitor") or {}).get("channels") or []
    i = 0
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        ch_id = str(ch.get("id") or "").strip()
        for s in (ch.get("shows") or []):
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or "").strip()
            if not sid:
                continue
            urls_file = str(s.get("urls_file") or "").strip()
            upsert_show_profile(
                conn,
                show_id=sid,
                topic_name=str(s.get("topic_name") or sid),
                anime_prefix=str(s.get("anime_prefix") or ""),
                caption_file=str(s.get("caption_file") or ""),
                download_dir=str(s.get("download_dir") or f"xiazai/downloads/{sid}"),
                urls_file=urls_file,
                sort_order=int(s.get("sort_order") or i),
                channel_id=ch_id,
            )
            i += 1

            # 从 txt 文件加载 URL
            if urls_file:
                txt_path = resolve_rel(root, urls_file)
                episode_urls = _load_urls_from_txt(txt_path)
                if episode_urls:
                    for ep, url in episode_urls.items():
                        conn.execute(
                            """
                            INSERT INTO episode_jobs (show_id, episode, url, download_status, upload_status, updated_at)
                            VALUES (?,?,?,?,?,?)
                            ON CONFLICT(show_id, episode) DO UPDATE SET
                              url=excluded.url,
                              updated_at=excluded.updated_at
                            """,
                            (sid, ep, url, "", "", utc_now_iso()),
                        )
    conn.commit()


def run_download_upload(upload_enabled_override: bool | None = None) -> None:
    root = project_root()
    cfg = load_config()
    db_path = database_path(cfg)
    conn = connect(db_path)
    cur = conn.cursor()
    ensure_schema(cur)
    conn.commit()

    seed_shows(conn, cfg, root)

    # v2.0: 构建 show_id → telegram_chat_id 映射（多频道上传）
    show_target_map, show_cover_map = _load_channel_target_map(cfg)
    default_target = str((cfg.get("telegram") or {}).get("target") or "").strip()

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

    profiles = list_show_profiles(conn)
    if not profiles:
        print("show_profiles 为空，请在 config.yaml monitor 段配置频道和剧集。", file=sys.stderr)
        return

    print(
        f"数据库: {db_path}\n"
        f"模式: {'下载+上传' if upload_enabled else '仅下载'}"
    )

    for prof in profiles:
        show_id = str(prof["show_id"])
        topic_name = str(prof["topic_name"])
        # v2.0: 解析该剧上传目标（优先频道 target，回退全局 target）
        show_target = show_target_map.get(show_id) or default_target or None
        # 按频道封面（优先频道 cover，回退全局 paths.cover）
        show_cover = resolve_cover(show_cover_map.get(show_id), cfg, root)
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

        n_upload_planned = _count_planned_uploads(rows, download_dir, anime_prefix, upload_enabled)
        upload_slot = 0

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
            prog_cb = _make_show_upload_progress_cb(
                show_id=show_id,
                topic_name=topic_name,
                ep=ep,
                slot=upload_slot,
                n_planned=n_upload_planned,
                file_label=out_path.name,
            )
            upload_slot += 1
            for attempt in range(1, upload_retries + 1):
                print(f"  upload ep{ep} (attempt {attempt})", flush=True)
                try:
                    import asyncio
                    ok = asyncio.run(
                        upload_via_telegram_manager(
                            file_path=out_path,
                            caption=caption,
                            target=show_target,
                            thumb_path=show_cover,
                            progress_callback=prog_cb,
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
    progress_callback: Any = None,
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
            progress_callback=progress_callback,
        )
        return True
    except Exception as e:
        print(f"上传失败: {e}", file=sys.stderr)
        return False
    finally:
        await mgr.disconnect()
