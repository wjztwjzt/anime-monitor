import asyncio
import logging
import sys

from dotenv import load_dotenv
from tg_chat_sim.app import main


def _configure_tg_chat_sim_logging() -> None:
    """为本包提供 INFO 级别输出（不依赖 root 是否已 basicConfig）。"""
    pkg = logging.getLogger("tg_chat_sim")
    if pkg.handlers:
        return
    pkg.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    pkg.addHandler(h)


if __name__ == "__main__":
    load_dotenv()
    _configure_tg_chat_sim_logging()
    try:
        asyncio.run(main())
    except RuntimeError as exc:
        if str(exc).startswith("无法打开 SQLite"):
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from None
        raise
