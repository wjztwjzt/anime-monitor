from __future__ import annotations

from typing import Any


def build_socks5_url(upload_cfg: dict[str, Any]) -> str | None:
    """upload.proxy → socks5://[user:pass@]host:port"""
    if not isinstance(upload_cfg, dict):
        return None
    if not bool(upload_cfg.get("enabled", False)):
        return None
    s = upload_cfg.get("socks5") or upload_cfg.get("socks") or {}
    if not isinstance(s, dict):
        return None
    host = str(s.get("host") or "").strip()
    port = s.get("port")
    if not host or port is None:
        return None
    try:
        pi = int(port)
    except (TypeError, ValueError):
        return None
    user = str(s.get("username") or s.get("user") or "").strip()
    pwd = str(s.get("password") or s.get("pass") or "").strip()
    if user:
        from urllib.parse import quote

        uq = quote(user, safe="")
        pq = quote(pwd, safe="") if pwd else ""
        auth = f"{uq}:{pq}@" if pq else f"{user}@"
        return f"socks5://{auth}{host}:{pi}"
    return f"socks5://{host}:{pi}"
