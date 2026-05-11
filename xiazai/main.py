"""兼容入口：从仓库任意位置运行时请优先使用项目根目录 `python run.py`。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.download_cli import main

if __name__ == "__main__":
    main()
