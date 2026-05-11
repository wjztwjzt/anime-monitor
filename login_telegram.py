"""
首次登录 Telegram：走 config.yaml 里的上传 SOCKS5 代理，交互输入验证码与二步验证密码，
会话写入 telegram.session_path（与上传脚本共用）。下次同一 session 文件可直接用，无需再输验证码。

用法（在项目根目录）:
  python login_telegram.py
  python login_telegram.py --no-proxy   # 直连（调试）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def _ensure_root_on_path() -> None:
    root = Path(__file__).resolve().parent
    rs = str(root)
    if rs not in sys.path:
        sys.path.insert(0, rs)


_ensure_root_on_path()


def _socks_proxy_from_config(cfg: dict) -> dict | None:
    px = cfg.get("proxy") or {}
    up = px.get("upload") if isinstance(px, dict) else {}
    if not isinstance(up, dict) or not bool(up.get("enabled", False)):
        return None
    s = up.get("socks5") or {}
    if not isinstance(s, dict):
        return None
    host = str(s.get("host") or "").strip()
    try:
        port = int(s.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not host or port <= 0:
        return None
    try:
        from python_socks import ProxyType
    except ImportError as e:
        raise SystemExit(
            "请安装依赖: pip install python-socks telethon\n" + str(e)
        ) from e

    user = str(s.get("username") or s.get("user") or "").strip() or None
    pwd = str(s.get("password") or "").strip() or None
    return {
        "proxy_type": ProxyType.SOCKS5,
        "addr": host,
        "port": port,
        "username": user,
        "password": pwd,
        "rdns": True,
    }


async def _run(*, use_proxy: bool) -> int:
    from app.config_loader import load_config, resolve_rel
    from app.paths import project_root

    cfg = load_config()
    tg = cfg.get("telegram") or {}
    if not isinstance(tg, dict):
        raise SystemExit("config.yaml 缺少 telegram 段")

    api_id_raw = str(tg.get("api_id") or "").strip()
    api_hash = str(tg.get("api_hash") or "").strip()
    if not api_id_raw.isdigit() or not api_hash:
        raise SystemExit("请在 config.yaml -> telegram 填写 api_id（数字）与 api_hash")

    api_id = int(api_id_raw)
    session_rel = str(tg.get("session_path") or "Telethon-FastUpload/session.session").strip()
    root = project_root()
    session_path = resolve_rel(root, session_rel)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    phone_cfg = str(tg.get("phone") or "").strip()

    proxy = _socks_proxy_from_config(cfg) if use_proxy else None
    if use_proxy and proxy is None:
        print(
            "警告: config.yaml 里 proxy.upload.enabled 未开启或 socks5 不完整，将改为直连。",
            file=sys.stderr,
        )

    try:
        from telethon import TelegramClient
    except ImportError as e:
        raise SystemExit("请安装: pip install telethon\n" + str(e)) from e

    if proxy:
        print(f"使用 SOCKS5 代理: {proxy['addr']}:{proxy['port']}")
    else:
        print("直连 Telegram（未使用代理）")

    client = TelegramClient(
        str(session_path),
        api_id,
        api_hash,
        connection_retries=5,
        retry_delay=3,
        proxy=proxy,
    )
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"会话已有效，无需重新登录: {session_path}")
        if me:
            print(f"当前账号: {getattr(me, 'first_name', '')} id={me.id}")
        await client.disconnect()
        return 0

    print("首次登录：按提示输入手机号（可含国际区号）、Telegram 发来的验证码；若开启二步验证会要求密码。")
    phone_arg = phone_cfg if phone_cfg else None
    await client.start(phone=phone_arg)

    me = await client.get_me()
    print(f"登录成功，会话已保存: {session_path}")
    if me:
        print(f"账号: {getattr(me, 'first_name', '')} id={me.id}")

    await client.disconnect()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Telegram 首次登录（SOCKS5 session）")
    ap.add_argument(
        "--no-proxy",
        action="store_true",
        help="不使用 config 中的 SOCKS5，直连",
    )
    args = ap.parse_args()
    try:
        raise SystemExit(asyncio.run(_run(use_proxy=not args.no_proxy)))
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
