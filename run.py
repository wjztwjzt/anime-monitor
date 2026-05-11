"""项目入口：在项目根目录执行 python run.py [--upload|--download-only]

Windows 请勿双击依赖 shebang；请用终端: py run.py 或 python run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 必须从任意工作目录运行都能找到 app 包（勿依赖「当前目录」）
_ROOT = Path(__file__).resolve().parent
_rs = str(_ROOT)
if _rs not in sys.path:
    sys.path.insert(0, _rs)

from app.download_cli import main

if __name__ == "__main__":
    main()
