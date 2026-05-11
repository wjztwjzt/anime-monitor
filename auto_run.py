"""全自动监控→下载→上传→删除 一键启动。

用法:
  python auto_run.py                  # 每 30 分钟循环检查
  python auto_run.py --interval 600   # 每 10 分钟循环检查
  python auto_run.py --once           # 单次检查
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="全自动动漫监控→下载→上传→删除")
    parser.add_argument("--once", action="store_true", help="单次检查")
    parser.add_argument("--interval", type=int, default=1800, help="循环间隔秒数（默认 1800）")
    return parser.parse_known_args()[0]


if __name__ == "__main__":
    args = _parse_args()
    if args.once:
        sys.argv = [sys.argv[0]]
    else:
        sys.argv = [sys.argv[0], "--loop", "--interval", str(args.interval)]

    from jiankong.favorites_notify import main

    sys.exit(main())
