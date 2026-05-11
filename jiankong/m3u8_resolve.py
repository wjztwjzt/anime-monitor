"""
新集 m3u8：默认 moon_tv（搜索 + episodes）；可选 stub / placeholder / import。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

ResolverFn = Callable[..., dict[int, str]]


def resolve_new_episode_m3u8_urls(
    *,
    item_key: str,
    title: str,
    display_name: str,
    old_total: int,
    new_total: int,
    source_name: str = "",
    source_id: str = "",
    vod_id: str = "",
) -> dict[int, str]:
    """
    返回 {集数: m3u8_url}，仅包含新增区间 (old_total+1)…new_total。
    moon_tv：按 BASE_URL 调 /api/search，匹配 source_name，读 episodes。
    """
    mode = (os.environ.get("M3U8_RESOLVER_MODE") or "moon_tv").strip().lower()

    if mode == "placeholder":
        out: dict[int, str] = {}
        for ep in range(old_total + 1, new_total + 1):
            out[ep] = (
                f"https://example.invalid/placeholder/{item_key.replace('+', '_')}/ep{ep}.m3u8"
            )
        logging.warning("M3U8_RESOLVER_MODE=placeholder：虚构链接，仅调试流水线")
        return out

    if mode == "stub":
        logging.info("M3U8_RESOLVER_MODE=stub：不解析 m3u8")
        return {}

    if mode == "moon_tv":
        from jiankong.moon_tv_m3u8 import resolve_from_change

        return resolve_from_change(
            {
                "key": item_key,
                "title": title,
                "display_name": display_name,
                "oldTotal": old_total,
                "newTotal": new_total,
                "source_name": source_name,
                "source_id": source_id,
                "vod_id": vod_id,
            },
            base_url=(os.environ.get("BASE_URL") or "").strip().rstrip("/"),
        )

    if mode == "import":
        spec = (os.environ.get("M3U8_RESOLVER_IMPORT") or "").strip()
        if not spec or ":" not in spec:
            logging.error("M3U8_RESOLVER_MODE=import 需要 M3U8_RESOLVER_IMPORT=模块路径:函数名")
            return {}
        mod_name, _, func_name = spec.partition(":")
        mod = __import__(mod_name, fromlist=[func_name])
        fn = getattr(mod, func_name, None)
        if not callable(fn):
            logging.error("无法导入解析函数: %s", spec)
            return {}
        raw = fn(
            item_key=item_key,
            title=title,
            display_name=display_name,
            old_total=old_total,
            new_total=new_total,
            source_name=source_name,
            source_id=source_id,
            vod_id=vod_id,
        )
        return _normalize_episode_dict(raw)

    logging.error("未知 M3U8_RESOLVER_MODE=%s", mode)
    return {}


def _normalize_episode_dict(raw: Any) -> dict[int, str]:
    if not isinstance(raw, dict):
        logging.error("解析函数必须返回 dict，实际 %s", type(raw).__name__)
        return {}
    out: dict[int, str] = {}
    for k, v in raw.items():
        try:
            ep = int(k)
        except (TypeError, ValueError):
            continue
        u = str(v).strip()
        if u.startswith("http://") or u.startswith("https://"):
            out[ep] = u
    return dict(sorted(out.items()))
