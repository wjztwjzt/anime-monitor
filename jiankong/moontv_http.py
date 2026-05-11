"""
MoonTV 站点 HTTP：与 huoqu.py 一致的 Cookie、Accept-Encoding、代理策略。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
COOKIE_FILE = SCRIPT_DIR / "cookies.txt"

DISABLE_PROXY = os.environ.get("DISABLE_PROXY", "").strip().lower() in ("1", "true", "yes", "on")
PROXY_URL = (os.environ.get("HTTP_PROXY") or "http://127.0.0.1:10809").strip()


def load_cookie_header() -> str:
    if not COOKIE_FILE.is_file():
        return ""
    raw = COOKIE_FILE.read_text(encoding="utf-8")
    lines = [
        ln.strip()
        for ln in raw.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not lines:
        return ""
    first = lines[0]
    if first.lower().startswith("cookie:"):
        first = first.split(":", 1)[1].strip()
    if "=" in first.split(";", 1)[0]:
        return first.replace(" ", "")
    return f"auth={first.strip()}"


def _browser_headers(*, cookie_header: str, referer_path: str, base_url: str) -> dict[str, str]:
    referer = f"{base_url.rstrip('/')}{referer_path}"
    h: dict[str, str] = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": referer,
        "Origin": base_url.rstrip("/"),
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if cookie_header:
        h["Cookie"] = cookie_header
    return h


def moon_tv_get(
    base_url: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    referer_path: str | None = None,
    timeout: float = 30,
) -> requests.Response:
    base = base_url.rstrip("/")
    url = f"{base}{path if path.startswith('/') else '/' + path}"
    ref = referer_path if referer_path is not None else "/"
    headers = _browser_headers(
        cookie_header=load_cookie_header(),
        referer_path=ref,
        base_url=base,
    )
    proxies = None
    if not DISABLE_PROXY and PROXY_URL:
        proxies = {"http": PROXY_URL, "https": PROXY_URL}
    return requests.get(url, params=params or {}, headers=headers, proxies=proxies, timeout=timeout)


def search_referer_query(q: str) -> str:
    return f"/search?q={quote(q)}"
