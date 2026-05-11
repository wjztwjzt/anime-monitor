import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, utils
from telethon.tl import types

MB = 1024 * 1024


def _env_proxy_url() -> Optional[str]:
    enabled = os.getenv("PROXY_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    host = os.getenv("PROXY_HOST")
    port = os.getenv("PROXY_PORT")
    if not host or not port:
        return None
    user = os.getenv("PROXY_USER")
    pwd = os.getenv("PROXY_PASS")
    auth = f"{user}:{pwd}@" if user else ("{}@".format(user) if pwd else "")
    return f"socks5://{auth}{host}:{port}"


def _parse_proxy(proxy_str: Optional[str]):
    if not proxy_str:
        return None
    parsed = urlparse(proxy_str)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        raise ValueError("代理 URL 缺少 scheme/host/port")
    scheme = parsed.scheme.lower()
    if scheme not in {"socks5", "socks5h", "socks4", "http", "https"}:
        raise ValueError(f"不支持的代理类型: {scheme}")
    username = parsed.username
    password = parsed.password
    return (scheme, parsed.hostname, parsed.port, True, username, password)


def _normalize_peer(value: str):
    s = value.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return value


def _iter_video_files(download_dir: Path, recursive: bool) -> list[Path]:
    exts = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4v", ".ts"}
    pattern = "**/*" if recursive else "*"
    files: list[Path] = []
    for p in download_dir.glob(pattern):
        if p.is_file() and p.suffix.lower() in exts:
            files.append(p)
    files.sort(key=lambda x: str(x).lower())
    return files


def _make_progress_printer(label: str, min_interval_sec: float = 0.5):
    start = time.monotonic()
    last = start
    last_bytes = 0

    def cb(current: int, total: int):
        nonlocal last, last_bytes
        now = time.monotonic()
        if now - last < min_interval_sec and current != total:
            return
        dt = now - last
        db = current - last_bytes
        inst = (db / MB) / dt if dt > 0 else 0.0
        avg = (current / MB) / (now - start) if now > start else 0.0
        percent = (current / total * 100) if total else 0.0
        sys.stderr.write(
            f"\r{label} {current / MB:8.2f}/{total / MB:8.2f} MB {percent:6.2f}% inst {inst:6.2f} MB/s avg {avg:6.2f} MB/s"
        )
        sys.stderr.flush()
        last = now
        last_bytes = current
        if current == total:
            sys.stderr.write("\n")
            sys.stderr.flush()

    return cb


def _prompt_yes_no(prompt: str, default_yes: bool = True) -> bool:
    suffix = " (Y/n): " if default_yes else " (y/N): "
    while True:
        s = input(prompt + suffix).strip().lower()
        if not s:
            return default_yes
        if s in {"y", "yes", "1", "true", "on"}:
            return True
        if s in {"n", "no", "0", "false", "off"}:
            return False
        print("请输入 y 或 n")


def _prompt_int(prompt: str, default: Optional[int] = None, min_value: Optional[int] = None) -> Optional[int]:
    hint = f"（默认 {default}）" if default is not None else "（留空表示默认）"
    while True:
        s = input(f"{prompt}{hint}: ").strip()
        if not s:
            return default
        if not s.lstrip("-").isdigit():
            print("请输入整数")
            continue
        v = int(s)
        if min_value is not None and v < min_value:
            print(f"请输入 >= {min_value} 的整数")
            continue
        return v


def _fasttelethon_available() -> bool:
    try:
        import FastTelethonhelper  # noqa: F401
        import FastTelethon  # noqa: F401

        return True
    except ImportError:
        return False


async def fasttelethon_upload_file_tuned(
    *,
    client: TelegramClient,
    file_path: Path,
    progress_callback,
    connections: Optional[int],
    part_size: int = 512 * 1024,
):
    """
    使用 FastTelethon 的多连接并行上传，但本地读取用 512KB 分片，避免 1KB 读取导致 Python 端变慢。
    """
    import FastTelethonhelper  # noqa: F401  # 确保其把 FastTelethon.py 加入 sys.path
    import FastTelethon  # type: ignore

    from telethon import helpers
    from telethon.tl.types import InputFile, InputFileBig

    file_size = file_path.stat().st_size
    file_id = helpers.generate_random_long()
    part_count = (file_size + part_size - 1) // part_size
    is_large = file_size > 10 * 1024 * 1024

    uploader = FastTelethon.ParallelTransferrer(client)  # type: ignore[attr-defined]
    await uploader._init_upload(  # type: ignore[attr-defined]
        connections=connections or uploader._get_connection_count(file_size),  # type: ignore[attr-defined]
        file_id=file_id,
        part_count=part_count,
        big=is_large,
    )

    uploaded = 0
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(part_size)
            if not chunk:
                break
            await uploader.upload(chunk)
            uploaded += len(chunk)
            if not is_large:
                md5.update(chunk)
            if progress_callback:
                try:
                    progress_callback(uploaded, file_size)
                except Exception:
                    pass

    await uploader.finish_upload()
    if is_large:
        return InputFileBig(file_id, part_count, file_path.name)
    return InputFile(file_id, part_count, file_path.name, md5.hexdigest())


def _resolved_thumb_path() -> Optional[str]:
    raw = (os.getenv("TELEGRAM_THUMB") or "").strip()
    if not raw:
        return None
    tp = Path(raw).expanduser()
    return str(tp.resolve()) if tp.is_file() else None


def _video_attributes_for_path(video_path: Path, thumb_path: Optional[str]):
    """按本地视频路径生成文档属性，确保以「视频」而非普通文件发送；封面用于辅助尺寸。"""
    return utils.get_attributes(
        str(video_path.resolve()),
        supports_streaming=True,
        thumb=thumb_path,
    )


def _ffprobe_video_meta(video_path: Path) -> Optional[tuple[float, int, int]]:
    """读取真实时长(秒)与首路视频宽高。Telegram 需要非零 duration，否则会显示 00:00 并像「大文件」。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,duration",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(video_path.resolve()),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    w, h = 1280, 720
    stream_dur = 0.0
    if streams:
        s0 = streams[0]
        try:
            w = max(1, int(s0.get("width") or 1280))
            h = max(1, int(s0.get("height") or 720))
        except (TypeError, ValueError):
            pass
        try:
            sd = s0.get("duration")
            if sd is not None and str(sd).strip():
                stream_dur = float(sd)
        except (TypeError, ValueError):
            pass
    dur = 0.0
    try:
        fd = fmt.get("duration")
        if fd is not None and str(fd).strip():
            dur = float(fd)
    except (TypeError, ValueError):
        pass
    if dur <= 0 and stream_dur > 0:
        dur = stream_dur
    if dur <= 0:
        return None
    return (dur, w, h)


def _inject_ffprobe_document_attributes(
    attributes: list,
    video_path: Path,
    *,
    supports_streaming: bool,
    nosound_hint: Optional[bool],
) -> list:
    """用 ffprobe 覆盖 DocumentAttributeVideo，避免 duration=0。"""
    meta = _ffprobe_video_meta(video_path)
    base = [a for a in attributes if not isinstance(a, types.DocumentAttributeVideo)]
    if meta:
        dur, w, h = meta
        nv_attr: Optional[bool]
        if nosound_hint is True:
            nv_attr = True
        elif nosound_hint is False:
            nv_attr = False
        else:
            nv_attr = None
        vid = types.DocumentAttributeVideo(
            duration=float(dur),
            w=w,
            h=h,
            round_message=False,
            supports_streaming=supports_streaming,
            nosound=nv_attr,
        )
        return [vid] + base
    print(
        "警告: ffprobe 无法读取视频时长/分辨率，频道可能仍显示 00:00；请安装 FFmpeg 并保证 PATH 中有 ffprobe。",
        file=sys.stderr,
    )
    # 保留 Telethon 推断的属性（可能仍为 0）；尽量给一个正的占位时长减轻客户端异常展示
    fallback = types.DocumentAttributeVideo(
        duration=1.0,
        w=1280,
        h=720,
        round_message=False,
        supports_streaming=supports_streaming,
        nosound=True if nosound_hint is True else (False if nosound_hint is False else None),
    )
    return [fallback] + base


def _build_document_attributes(
    video_path: Path,
    thumb_path: Optional[str],
    *,
    nosound_hint: Optional[bool],
) -> tuple[list, Optional[str]]:
    attrs, mime = _video_attributes_for_path(video_path, thumb_path)
    attrs = _inject_ffprobe_document_attributes(
        attrs,
        video_path,
        supports_streaming=True,
        nosound_hint=nosound_hint,
    )
    return attrs, mime


def _normalize_video_mime(video_path: Path, mime_type: Optional[str]) -> str:
    """强制 video/*，避免被当成 application/octet-stream 从而在客户端里显示成「文件」。"""
    if mime_type and mime_type.startswith("video/"):
        return mime_type
    ext = video_path.suffix.lower()
    by_ext = {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".flv": "video/x-flv",
        ".ts": "video/mp2t",
    }
    if ext in by_ext:
        return by_ext[ext]
    g = mimetypes.guess_type(str(video_path))[0]
    if g and g.startswith("video/"):
        return g
    return "video/mp4"


def _infer_nosound_video(video_path: Path) -> Optional[bool]:
    """无音轨时 Telegram 需 nosound_video=True 才会按「视频」展示并可点播放；有音轨则显式 False。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(video_path.resolve()),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    has_audio = bool([ln for ln in (r.stdout or "").splitlines() if ln.strip()])
    if has_audio:
        return False
    return True


def _want_parallel_upload() -> bool:
    return (os.getenv("TELEGRAM_PARALLEL_UPLOAD") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _send_parallel_video_document(
    *,
    client: TelegramClient,
    target,
    path: Path,
    tgfile,
    thumb_path: Optional[str],
    caption: str,
    attributes,
    mime_guess: Optional[str],
    nosound: Optional[bool],
) -> Any:
    mime_type = _normalize_video_mime(path, mime_guess)
    thumb_handle = None
    if thumb_path:
        thumb_handle = await client.upload_file(thumb_path)
    nv: Optional[bool] = None
    if mime_type.startswith("video/"):
        nv = nosound
    media = types.InputMediaUploadedDocument(
        file=tgfile,
        mime_type=mime_type,
        attributes=attributes,
        thumb=thumb_handle,
        force_file=False,
        nosound_video=nv,
    )
    return await client.send_file(target, media, caption=caption)


async def upload_video_with_fallback(
    *,
    client: TelegramClient,
    target,
    path: Path,
    connections: Optional[int],
    progress_cb,
) -> None:
    """发送可 inline 播放的视频（非「文件」气泡）。

    默认走 Telethon 按路径上传（Telegram 侧识别最稳）。无音轨时需 nosound_video=True（ffprobe 检测）。
    并行分片上传仅当同时安装 FastTelethonhelper 且设置环境变量 TELEGRAM_PARALLEL_UPLOAD=1。
    """
    thumb_path = _resolved_thumb_path()
    nosound = _infer_nosound_video(path)
    attributes, mime_guess = _build_document_attributes(
        path,
        thumb_path,
        nosound_hint=nosound,
    )
    cap = (os.getenv("TELEGRAM_CAPTION") or "").strip()

    use_parallel = _want_parallel_upload() and _fasttelethon_available()
    if use_parallel:
        tgfile = await fasttelethon_upload_file_tuned(
            client=client,
            file_path=path,
            progress_callback=progress_cb,
            connections=connections,
        )
        await _send_parallel_video_document(
            client=client,
            target=target,
            path=path,
            tgfile=tgfile,
            thumb_path=thumb_path,
            caption=cap,
            attributes=attributes,
            mime_guess=mime_guess,
            nosound=nosound,
        )
        return

    if _want_parallel_upload() and not _fasttelethon_available():
        print(
            "提示: 已设置 TELEGRAM_PARALLEL_UPLOAD 但未安装 FastTelethonhelper，改用内置上传。",
            file=sys.stderr,
        )

    def _prog(current: int, total: int):
        if progress_cb:
            try:
                progress_cb(current, total)
            except Exception:
                pass

    await client.send_file(
        target,
        str(path.resolve()),
        caption=cap,
        progress_callback=_prog,
        supports_streaming=True,
        force_document=False,
        attributes=attributes,
        thumb=thumb_path,
        nosound_video=nosound,
    )


async def main_async(args: argparse.Namespace) -> int:
    base_dir = Path(__file__).resolve().parent
    load_dotenv(dotenv_path=base_dir / ".env")

    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    session = os.getenv("TELEGRAM_SESSION", str(base_dir / "session.session"))
    phone = os.getenv("TELEGRAM_PHONE")
    target_raw = os.getenv("TELEGRAM_TARGET")

    if not api_id or not api_id.strip().isdigit():
        raise SystemExit("缺少 TELEGRAM_API_ID（需要整数）")
    if not api_hash:
        raise SystemExit("缺少 TELEGRAM_API_HASH")
    if not target_raw:
        raise SystemExit("缺少 TELEGRAM_TARGET")

    single_file: Optional[Path] = getattr(args, "file", None)
    if single_file is not None:
        single_file = Path(single_file).expanduser().resolve()
        if not single_file.is_file():
            raise SystemExit(f"文件不存在: {single_file}")

    download_dir_raw = os.getenv("TELEGRAM_DOWNLOAD_DIR") or str(base_dir / "downloads")
    download_dir = Path(download_dir_raw).expanduser().resolve()
    if single_file is None and not download_dir.exists():
        raise SystemExit(f"downloads 目录不存在: {download_dir}")

    proxy = None
    if not args.no_proxy:
        proxy_str = os.getenv("TELEGRAM_PROXY") or _env_proxy_url()
        proxy = _parse_proxy(proxy_str)

    client = TelegramClient(
        session,
        int(api_id),
        api_hash,
        use_ipv6=False,
        proxy=proxy,
    )

    await client.start(phone=phone)
    try:
        target = await client.get_entity(_normalize_peer(target_raw))
        if single_file is not None:
            files = [single_file]
        else:
            files = _iter_video_files(download_dir, recursive=args.recursive)
        if args.limit:
            files = files[: args.limit]
        if not files:
            print(f"未在 {download_dir} 找到视频文件")
            return 0

        print(f"目标: {target_raw}")
        if single_file is not None:
            print(f"单文件: {single_file}")
        else:
            print(f"目录: {download_dir}")
        print(f"文件数: {len(files)}")

        total_bytes = 0
        total_sec = 0.0

        for idx, path in enumerate(files, start=1):
            size = path.stat().st_size
            label = path.name[-60:]
            progress_cb = _make_progress_printer(label)

            print(f"\n[{idx}/{len(files)}] {path.name} ({size / MB:.2f} MB)")
            t0 = time.monotonic()
            await upload_video_with_fallback(
                client=client,
                target=target,
                path=path,
                connections=args.connections,
                progress_cb=progress_cb,
            )
            t1 = time.monotonic()

            sec = t1 - t0
            avg = (size / MB) / sec if sec > 0 else 0.0
            print(f"完成: {path.name} 用时 {sec:.2f}s 平均 {avg:.2f} MB/s")

            total_bytes += size
            total_sec += sec

        total_avg = (total_bytes / MB) / total_sec if total_sec > 0 else 0.0
        print(f"\n总计: {total_bytes / MB:.2f} MB / {total_sec:.2f}s = {total_avg:.2f} MB/s")
        return 0
    finally:
        await client.disconnect()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="FastTelethon 多连接上传测速示例（读取 .env + 现有 session）")
    p.add_argument(
        "--file",
        type=Path,
        default=None,
        help="只上传该视频文件（单文件模式；不必依赖 TELEGRAM_DOWNLOAD_DIR 目录存在）",
    )
    p.add_argument("--limit", type=int, default=None, help="最多上传多少个文件（默认全部）")
    p.add_argument("--recursive", action="store_true", help="递归扫描 downloads 子目录")
    p.add_argument("--no-proxy", action="store_true", help="忽略 .env 的代理配置")
    p.add_argument("--connections", type=int, default=None, help="强制连接数（默认按文件大小自动）")
    return p.parse_args()


def main() -> None:
    if len(sys.argv) == 1:
        print("交互模式：无需参数，按提示输入即可。\n")
        args = argparse.Namespace()
        args.file = None
        args.limit = _prompt_int("最多上传多少个文件", default=None, min_value=1)
        args.recursive = _prompt_yes_no("递归扫描 downloads 子目录？", default_yes=False)
        args.no_proxy = _prompt_yes_no("忽略代理（--no-proxy）？", default_yes=True)
        args.connections = _prompt_int("连接数（建议 8~20）", default=16, min_value=1)
    else:
        args = parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
