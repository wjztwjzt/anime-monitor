"""
多提供商比较：同一动漫可能有多个提供商（如 jisu、maotaizy、37 等），
更新速度不一。本模块通过搜索同名动漫、对比各提供商的集数，取最高集数
作为最新集数，并返回最佳提供商的 m3u8 信息。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from jiankong.moontv_http import moon_tv_get, search_referer_query


def _flatten_search(data: Any) -> list[dict[str, Any]]:
    """从 MoonTV 搜索响应中提取条目列表（兼容多种 JSON 结构）。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("list", "results", "data", "items", "records"):
        v = data.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _norm_id(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _extract_episode_count(item: dict[str, Any]) -> int:
    """从搜索条目中提取集数。"""
    for key in ("total_episodes", "totalEpisodes", "episode_count", "episodeCount",
                "total", "count", "episodes_total"):
        val = item.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    episodes = item.get("episodes") or item.get("episode_list") or []
    if isinstance(episodes, list):
        return len(episodes)
    return 0


def _extract_title(item: dict[str, Any]) -> str:
    return str(item.get("title") or "").strip()


def _extract_source_name(item: dict[str, Any]) -> str:
    return str(item.get("source_name") or item.get("sourceName") or "").strip()


def _extract_source_id(item: dict[str, Any]) -> str:
    src = item.get("source") or item.get("source_id") or item.get("sourceId") or ""
    if isinstance(src, dict):
        return _norm_id(src.get("id") or src.get("key") or src.get("code"))
    return _norm_id(src)


def _extract_vod_id(item: dict[str, Any]) -> str:
    return _norm_id(item.get("id") or item.get("vod_id") or item.get("vodId"))


def search_all_providers(
    title: str,
    base_url: str,
    *,
    min_title_similarity: float = 0.8,
) -> list[dict[str, Any]]:
    """搜索动漫标题，返回所有匹配的提供商条目（按集数降序）。"""
    q = title.strip()
    if not q:
        return []

    try:
        r = moon_tv_get(
            base_url,
            "/api/search",
            {"q": q},
            referer_path=search_referer_query(q),
        )
        if r.status_code != 200:
            logging.error("搜索 HTTP %s: %s", r.status_code, r.text[:400])
            return []
        data = r.json()
    except Exception:
        logging.exception("搜索请求失败 q=%s", q)
        return []

    rows = _flatten_search(data)
    if not rows:
        logging.warning("搜索无结果: %s", q)
        return []

    results: list[dict[str, Any]] = []
    target_lower = title.lower().strip()

    for row in rows:
        row_title = _extract_title(row).lower()
        if not row_title:
            continue
        # 简单标题匹配：完全包含或高度相似
        if target_lower in row_title or row_title in target_lower:
            results.append(row)
            continue
        # 模糊匹配：字符重叠度
        overlap = len(set(target_lower) & set(row_title))
        if overlap >= max(len(target_lower), len(row_title)) * min_title_similarity:
            results.append(row)

    # 按集数降序排列
    results.sort(key=_extract_episode_count, reverse=True)
    return results


def get_best_provider(
    title: str,
    base_url: str,
) -> dict[str, Any] | None:
    """
    搜索动漫并返回集数最高的提供商条目。
    返回 dict 包含: title, source_name, source_id, vod_id, total_episodes, episodes 等。
    """
    providers = search_all_providers(title, base_url)
    if not providers:
        return None

    best = providers[0]
    best_count = _extract_episode_count(best)

    logging.info(
        "「%s」找到 %s 个提供商，最佳: %s (source=%s, 集数=%s)",
        title,
        len(providers),
        _extract_title(best),
        _extract_source_name(best) or _extract_source_id(best),
        best_count,
    )

    # 记录所有提供商信息（调试用）
    for i, p in enumerate(providers):
        logging.debug(
            "  [%s] %s | source=%s | episodes=%s",
            i + 1,
            _extract_title(p),
            _extract_source_name(p) or _extract_source_id(p),
            _extract_episode_count(p),
        )

    return best


def compare_and_get_max_episodes(
    favorites_data: dict[str, Any],
    alias_map: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """
    从收藏数据中按动漫名称分组，每组取最高集数的条目。

    返回: {display_name: {key, title, total, source_name, source_id, vod_id, ...}}
    """
    # 第一步：按备注名/标题分组
    groups: dict[str, list[dict[str, Any]]] = {}

    for item_key, fav in favorites_data.items():
        if not isinstance(fav, dict):
            continue
        title = str(fav.get("title") or "")
        display = alias_map.get(item_key) or title
        if not display:
            continue
        try:
            total = int(fav.get("total_episodes") or 0)
        except (TypeError, ValueError):
            total = 0

        source_name = str(fav.get("source_name") or fav.get("sourceName") or "")
        source_id = str(fav.get("source_id") or fav.get("sourceId") or "")
        vod_id = str(fav.get("id") or fav.get("vod_id") or fav.get("vodId") or "")

        src_raw = fav.get("source")
        if isinstance(src_raw, str) and src_raw.strip():
            source_id = source_id or src_raw.strip()
        elif isinstance(src_raw, dict):
            source_id = source_id or _norm_id(
                src_raw.get("id") or src_raw.get("key") or src_raw.get("code")
            )
            if not source_name:
                source_name = str(
                    src_raw.get("name") or src_raw.get("source_name") or ""
                )

        if "+" in item_key and (not source_id or not vod_id):
            a, b = item_key.split("+", 1)
            if not source_id:
                source_id = a.strip()
            if not vod_id:
                vod_id = b.strip()

        entry = {
            "key": item_key,
            "title": title,
            "display_name": display,
            "total": total,
            "source_name": source_name,
            "source_id": source_id,
            "vod_id": vod_id,
        }

        if display not in groups:
            groups[display] = []
        groups[display].append(entry)

    # 第二步：每组取最高集数
    result: dict[str, dict[str, Any]] = {}
    for display, entries in groups.items():
        best = max(entries, key=lambda e: e["total"])
        result[display] = best

        if len(entries) > 1:
            logging.info(
                "「%s」共 %s 个提供商，取最高集数 %s (来自 %s)",
                display,
                len(entries),
                best["total"],
                best["source_name"] or best["source_id"] or best["key"],
            )

    return result
