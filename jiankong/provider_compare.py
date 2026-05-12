"""
多提供商比较：同一动漫可能有多个提供商（如 jisu、maotaizy、37 等），
更新速度不一。本模块通过搜索同名动漫、对比各提供商的集数，取最高集数
作为最新集数，并返回最佳提供商的 m3u8 信息。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from jiankong.moontv_http import moon_tv_get, search_referer_query


# ---- V2: moon.658877.xyz 精确匹配 ----

def search_all_providers_v2(
    keyword: str,
    base_url: str,
    *,
    filters: dict | None = None,
    max_retries: int = 3,
    retry_delay: float = 5.0,
):
    """V2 搜索：调用 moon.658877.xyz API，用元数据精确匹配，返回最佳 MoonShowResult 或 None。"""
    from jiankong.moon_api import match_best_show, search_moon_api

    results = search_moon_api(
        keyword, base_url,
        max_retries=max_retries,
        retry_delay=retry_delay,
    )
    if not results:
        return None
    return match_best_show(results, title=keyword, filters=filters)


# ---- V1: tv.658877.xyz ----

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


def _is_different_show_indicator(text: str) -> bool:
    """检查文本是否含「不同剧」标记（短剧、剧场版等），不应与正片匹配。"""
    _indicators = [
        "短剧", "剧场版", "ova", "番外", "特别篇", "外传", "前传",
        "sp", "总集篇", "番外篇", "oad", "oad版",
    ]
    t = text.lower()
    return any(ind in t for ind in _indicators)


def _title_match_quality(target: str, row_title: str) -> int:
    """
    返回匹配质量分数：
      2 = 精确匹配（完全相等）
      1 = 包含匹配（target 是 row_title 子串，且长度比 ≥ 0.5，且额外部分非「不同剧」标记）
      0 = 不匹配
    """
    t = target.lower().strip()
    r = row_title.lower().strip()
    if not t or not r:
        return 0
    if t == r:
        return 2
    if t in r:
        extra = r.replace(t, "", 1)
        if _is_different_show_indicator(extra):
            return 0
        # 子串匹配需长度比 ≥ 50%，防止「盘龙」匹配「盘龙卧虎高山顶」
        if len(t) < len(r) * 0.5:
            return 0
        return 1
    if r in t:
        extra = t.replace(r, "", 1)
        if _is_different_show_indicator(extra):
            return 0
        if len(r) < len(t) * 0.5:
            return 0
        return 1
    return 0


def search_all_providers(
    title: str,
    base_url: str,
    *,
    min_title_similarity: float = 0.8,
    max_retries: int = 3,
    retry_delay: float = 5.0,
    expected_episode_count: int = 0,
) -> list[dict[str, Any]]:
    """搜索动漫标题，返回所有匹配的提供商条目（按匹配置信度+集数排序）。
    带重试机制应对 API 返回空。expected_episode_count 用于过滤离群结果。
    """
    q = title.strip()
    if not q:
        return []

    data = None
    _auth_expired_logged = False
    for attempt in range(max_retries):
        if attempt > 0:
            logging.info("搜索重试 %s/%s: %s", attempt + 1, max_retries, q)
            time.sleep(retry_delay)

        try:
            r = moon_tv_get(
                base_url,
                "/api/search",
                {"q": q},
                referer_path=search_referer_query(q),
            )
            if r.status_code in (401, 403):
                if not _auth_expired_logged:
                    logging.error(
                        "MoonTV auth 过期 (HTTP %s)，请重新从浏览器获取 cookie 更新 jiankong/cookies.txt",
                        r.status_code,
                    )
                    _auth_expired_logged = True
                return []
            if r.status_code != 200:
                logging.error("搜索 HTTP %s: %s", r.status_code, r.text[:400])
                continue
            data = r.json()
        except Exception:
            logging.exception("搜索请求失败 q=%s (attempt %s)", q, attempt + 1)
            continue

        rows = _flatten_search(data)
        if not rows:
            if attempt < max_retries - 1:
                logging.info("搜索返回空，将重试: %s", q)
                continue
            logging.warning("搜索无结果（已重试%s次）: %s", max_retries, q)
            return []

        target_lower = title.lower().strip()
        scored: list[tuple[int, dict[str, Any]]] = []

        for row in rows:
            row_title = _extract_title(row).lower()
            if not row_title:
                continue
            quality = _title_match_quality(target_lower, row_title)
            if quality > 0:
                scored.append((quality, row))
                continue
            # 模糊匹配：字符重叠度
            overlap = len(set(target_lower) & set(row_title))
            if overlap >= max(len(target_lower), len(row_title)) * min_title_similarity:
                scored.append((0, row))

        if scored:
            # 按 匹配质量 ↓ → 集数合理度 → 集数 ↓ 排序
            expected = expected_episode_count if expected_episode_count > 0 else 0

            def _sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
                quality, row = item
                ep = _extract_episode_count(row)
                # 集数合理度：越接近 expected 越优先（expected=0 时不干预）
                # 对集数远超 expected 的结果施加惩罚
                penalty = 0
                if expected > 0 and ep > expected:
                    ratio = ep / expected
                    if ratio > 1.5:
                        penalty = int(ratio * 100)  # 越大惩罚越重
                # 返回排序元组：质量(高优先) → 集数合理度(低惩罚优先) → 集数(低优先)
                # quality 取负使高质排前，penalty 正数使惩罚大的排后，ep 取负使低集数排前
                return (-quality, penalty, -ep)

            scored.sort(key=_sort_key)

            # 离群检测：同标题(质量≥1)结果中，剔除集数为中位数 2x 以上的极端离群
            quality_results = [r for q, r in scored if q >= 1]
            if len(quality_results) >= 3:
                counts = sorted([_extract_episode_count(r) for r in quality_results])
                median = counts[len(counts) // 2]
                if median > 0 and counts[-1] > median * 2:
                    outlier_threshold = median * 2
                    logging.info(
                        "搜索 %s 检测到集数离群值 (%s > %s×2)，已过滤",
                        q, counts[-1], median,
                    )
                    scored = [
                        (q, r) for q, r in scored
                        if not (q >= 1 and _extract_episode_count(r) > outlier_threshold)
                    ]
                    if not scored:
                        scored = [(q, r) for q, r in [(0, row) for row in quality_results]]

            # 2个同名结果集数差异大 → 保守取较低集数，并告警
            if len(quality_results) == 2:
                c0 = _extract_episode_count(quality_results[0])
                c1 = _extract_episode_count(quality_results[1])
                if min(c0, c1) > 0 and max(c0, c1) / min(c0, c1) > 1.5:
                    hi_count = max(c0, c1)
                    logging.warning(
                        "搜索 %s 同名结果集数差异大: %s vs %s，取较低值 %s",
                        q, max(c0, c1), min(c0, c1), min(c0, c1),
                    )
                    # 把高集数条目降到模糊匹配层(quality=0)，确保低集数优先
                    new_scored: list[tuple[int, dict[str, Any]]] = []
                    for q, r in scored:
                        if q >= 1 and _extract_episode_count(r) == hi_count:
                            new_scored.append((0, r))
                        else:
                            new_scored.append((q, r))
                    scored = new_scored
                    scored.sort(key=_sort_key)
                elif min(c0, c1) > 0 and max(c0, c1) / min(c0, c1) > 1.3:
                    logging.info(
                        "搜索 %s 同名结果集数有差异: %s vs %s",
                        q, max(c0, c1), min(c0, c1),
                    )

            # 多结果时打印完整列表（辅助排查）
            if len(scored) > 1:
                lines = []
                for quality, row in scored[:10]:
                    ep = _extract_episode_count(row)
                    t = _extract_title(row)
                    src = _extract_source_name(row) or _extract_source_id(row)
                    lines.append(f"  q={quality} ep={ep} [{src}] {t}")
                logging.info("搜索 %s 共%s条候选:\n%s", q, len(scored), "\n".join(lines))

            results = [r for _, r in scored]
            return results

        if attempt < max_retries - 1:
            logging.info("搜索匹配结果为空，将重试: %s", q)

    logging.warning("搜索无结果（已重试%s次）: %s", max_retries, q)
    return []


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

    # 第二步：每组取最高集数（附带整组条目，供 fav_items 按多 key 对齐）
    result: dict[str, dict[str, Any]] = {}
    for display, entries in groups.items():
        best = max(entries, key=lambda e: e["total"])
        merged = dict(best)
        merged["_group_entries"] = list(entries)
        result[display] = merged

        if len(entries) > 1:
            logging.info(
                "「%s」共 %s 个提供商，取最高集数 %s (来自 %s)",
                display,
                len(entries),
                best["total"],
                best["source_name"] or best["source_id"] or best["key"],
            )

    return result
