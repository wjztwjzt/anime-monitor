"""CLI：下载/上传（读取项目根 config.yaml）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 被直接 python -m app.download_cli 或其它方式加载时也能解析 app.*
_pkg_root = Path(__file__).resolve().parent.parent
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from app.download_worker import run_download_upload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="多番剧 m3u8 下载 + FastUpload 上传（统一 config.yaml + SQLite）"
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--upload", action="store_true", help="强制开启上传")
    g.add_argument("--download-only", action="store_true", help="仅下载")
    args = parser.parse_args()

    upload_override: bool | None = None
    if args.upload:
        upload_override = True
    elif args.download_only:
        upload_override = False

    run_download_upload(upload_enabled_override=upload_override)


if __name__ == "__main__":
    main()
