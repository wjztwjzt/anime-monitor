"""
MoonTV 搜索 API：与浏览器一致的头；正文避免 zstd（见 moontv_http）。
"""

from __future__ import annotations

import json
import os
import sys

import requests

from moontv_http import moon_tv_get, search_referer_query


def default_base_url() -> str:
    return (os.environ.get("BASE_URL") or "https://tv.658877.xyz").strip().rstrip("/")


def fetch_search(q: str, base_url: str | None = None):
    base = (base_url or default_base_url()).strip().rstrip("/")
    return moon_tv_get(
        base,
        "/api/search",
        {"q": q},
        referer_path=search_referer_query(q),
    )


def main() -> None:
    q = (os.environ.get("SEARCH_Q") or "择天记").strip()
    try:
        r = fetch_search(q)
    except requests.exceptions.ProxyError:
        print(
            "代理连接失败。请启动本地代理或设置 DISABLE_PROXY=1。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    except requests.RequestException as e:
        print(f"请求失败: {e}", file=sys.stderr)
        raise SystemExit(1)

    print(f"HTTP {r.status_code}")
    print(f"Content-Type: {r.headers.get('Content-Type', '')}")
    enc = r.headers.get("Content-Encoding") or ""
    if enc:
        print(f"Content-Encoding: {enc}")

    if r.status_code != 200:
        print(r.text[:2000])
        raise SystemExit(1)

    ct = (r.headers.get("Content-Type") or "").lower()
    if "application/json" in ct or r.text.lstrip().startswith(("{", "[")):
        try:
            data = r.json()
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except ValueError:
            print("正文不是合法 JSON，前 500 字节（调试）：")
            print(repr(r.content[:500]))
            raise SystemExit(1)
    else:
        print(r.text[:4000])


if __name__ == "__main__":
    main()
