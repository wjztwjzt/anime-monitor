"""
根据收藏变更拉取搜索/详情 JSON。

收藏键形如「source+vod_id」（如 37+97662）：vod_id 与搜索 JSON 里 "id"（如 "97662"）对齐匹配，最稳定。
新增集的 m3u8 来自 episodes 数组：第 N 集对应下标 N-1；本次取 old_total … new_total-1 下标（新增集）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from jiankong.moontv_http import moon_tv_get, search_referer_query


def _parse_item_key(item_key: str) -> tuple[str, str]:
    if "+" in item_key:
        a, b = item_key.split("+", 1)
        return a.strip(), b.strip()
    return "", ""


def _flatten_search_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("list", "results", "data", "items", "records"):
        v = data.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _episode_cells_from_obj(obj: Any) -> list[Any]:
    """从详情或搜索卡片中提取剧集列表（多种 MoonTV JSON 形态）。"""
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        return []
    for key in ("episodes", "episode_list", "urls"):
        v = obj.get(key)
        if isinstance(v, list):
            return v
    pb = obj.get("playbacks")
    if isinstance(pb, list) and pb:
        p0 = pb[0]
        if isinstance(p0, dict):
            eps = p0.get("episodes")
            if isinstance(eps, list):
                return eps
    return []


def _norm_id(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _cell_to_m3u8_url(cell: Any) -> str | None:
    if isinstance(cell, str):
        s = cell.strip()
        return s if s.startswith("http") else None
    if isinstance(cell, dict):
        for k in ("url", "playUrl", "m3u8", "link", "src"):
            v = cell.get(k)
            if isinstance(v, str) and v.strip().startswith("http"):
                return v.strip()
    return None


def _pick_card(
    rows: list[dict[str, Any]],
    *,
    vod_id: str,
    source_id: str,
    item_key: str,
    source_name: str,
    title: str,
) -> dict[str, Any] | None:
    """
    优先：搜索结果里的 id 与收藏的 vod_id（或 item_key 的 + 右侧）一致。
    其次：source + id 同时匹配；再 source_name；再 title。
    若已能解析出 vod_id 却仍无匹配，不使用「默认第一条」，避免串剧。
    """
    src_k, id_k = _parse_item_key(item_key)
    vid = _norm_id(vod_id) or id_k
    sid = _norm_id(source_id) or src_k
    sn = (source_name or "").strip()

    # 1) 仅按 id（与 /api/search 里 "id": "97662" 对齐）
    if vid:
        for row in rows:
            rid = _norm_id(row.get("id") or row.get("vod_id") or row.get("vodId"))
            if rid and rid == vid:
                logging.info("搜索条目按 id=%s 命中（与收藏 vod_id 一致）", vid)
                return row

    # 2) source + id（防止不同源下同 id，少见）
    if vid and sid:
        for row in rows:
            rid = _norm_id(row.get("id") or row.get("vod_id") or row.get("vodId"))
            rsrc = _norm_id(
                row.get("source")
                or row.get("source_id")
                or row.get("sourceId")
                or row.get("script_key")
            )
            if rid == vid and rsrc == sid:
                logging.info("搜索条目按 source=%s + id=%s 命中", sid, vid)
                return row

    # 3) source_name
    if sn:
        for row in rows:
            r_sn = str(row.get("source_name") or row.get("sourceName") or "").strip()
            if r_sn == sn:
                logging.info("搜索条目按 source_name=%s 命中", sn)
                return row

    # 4) 标题完全一致
    if title:
        tt = title.strip()
        for row in rows:
            if str(row.get("title") or "").strip() == tt:
                logging.info("搜索条目按 title 命中")
                return row

    if vid:
        logging.error(
            "搜索到 %s 条结果，但无 id=%s 的条目；请换关键词或检查收藏键与站点 id",
            len(rows),
            vid,
        )
        return None

    if rows:
        logging.warning("无 vod_id，暂用搜索结果第一条（建议在收藏接口中带上 id）")
        return rows[0]
    return None


def _detail_try_fetch(base_url: str, source: str, vod_id: str) -> dict[str, Any] | None:
    """尝试多种详情接口路径（站点实现略有差异）。"""
    if not vod_id:
        return None
    candidates: list[tuple[str, dict[str, Any]]] = [
        ("/api/detail", {"id": vod_id, "source": source}),
        ("/api/detail", {"vod_id": vod_id, "sourceId": source}),
        ("/api/detail", {"id": vod_id, "sourceId": source}),
        ("/api/video/detail", {"id": vod_id, "source": source}),
    ]
    for path, params in candidates:
        try:
            r = moon_tv_get(
                base_url,
                path,
                params,
                referer_path=f"/detail/{vod_id}",
                timeout=30,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            cells = _episode_cells_from_obj(data)
            if cells:
                logging.debug("详情接口命中 %s params=%s episodes=%s", path, params, len(cells))
                return data if isinstance(data, dict) else None
        except Exception:
            logging.debug("详情请求失败 %s", path, exc_info=True)
            continue
    return None


def resolve_new_episodes_m3u8(
    *,
    base_url: str,
    item_key: str,
    title: str,
    display_name: str,
    source_name: str,
    source_id: str,
    vod_id: str,
    old_total: int,
    new_total: int,
) -> dict[int, str]:
    """
    多提供商搜索 → 取集数最高的条目 → 取 episodes 列表 →
    新增集对应下标 [old_total .. new_total-1] 的 m3u8。

    因为同一动漫可能有多个提供商（更新速度不同），本函数会搜索所有提供商，
    自动选择集数最高的那个来解析 m3u8。
    """
    if old_total >= new_total:
        return {}

    src_from_key, id_from_key = _parse_item_key(item_key)
    if not source_id:
        source_id = src_from_key
    if not vod_id:
        vod_id = id_from_key

    q = (display_name or source_name or title or "").strip()
    if not q:
        q = vod_id.strip()
    if not q:
        logging.error("无搜索关键词")
        return {}

    # === 多提供商搜索：使用 provider_compare 找到最佳提供商 ===
    from jiankong.provider_compare import _extract_source_name, search_all_providers

    providers = search_all_providers(q, base_url)
    if not providers:
        # 回退到原有的精确搜索
        logging.info("多提供商搜索无结果，回退到精确搜索")
        try:
            r = moon_tv_get(
                base_url,
                "/api/search",
                {"q": q},
                referer_path=search_referer_query(q),
            )
            if r.status_code != 200:
                logging.error("搜索 HTTP %s", r.status_code)
                return {}
            data = r.json()
        except Exception:
            logging.exception("搜索请求失败 q=%s", q)
            return {}
        rows = _flatten_search_items(data)
        if not rows:
            logging.error("搜索无结果: %s", q)
            return {}
        providers = rows

    # 取集数最高的提供商
    best_card = providers[0]
    best_count = len(_episode_cells_from_obj(best_card))
    logging.info(
        "「%s」多提供商搜索: 共 %s 个结果，选最高集数 %s (source=%s)",
        q,
        len(providers),
        best_count,
        _extract_source_name(best_card) or _norm_id(best_card.get("id") or ""),
    )

    cells = _episode_cells_from_obj(best_card)
    detail_obj: dict[str, Any] | None = best_card if isinstance(best_card, dict) else None

    if len(cells) < new_total:
        vs = str(best_card.get("id") or best_card.get("vod_id") or vod_id or "").strip()
        ss = str(best_card.get("source") or best_card.get("source_id") or source_id or "").strip()
        merged = _detail_try_fetch(base_url, ss, vs)
        if merged:
            detail_obj = merged
            cells = _episode_cells_from_obj(merged)

    n_eps = len(cells)
    if n_eps < new_total:
        logging.warning(
            "最佳提供商 episodes 长度 %s < 收藏 new_total=%s（尝试拉取详情后仍不足）",
            n_eps,
            new_total,
        )
    elif n_eps != new_total:
        logging.info(
            "episodes 条数=%s，收藏 total=%s（允许略有延迟；只要下标覆盖新集即可）",
            n_eps,
            new_total,
        )

    out: dict[int, str] = {}
    for ep in range(old_total + 1, new_total + 1):
        idx = ep - 1
        if idx >= len(cells):
            logging.error("第 %s 集无 episodes[%s]（共 %s 条）", ep, idx, len(cells))
            continue
        u = _cell_to_m3u8_url(cells[idx])
        if u:
            out[ep] = u
        else:
            logging.error("第 %s 集无法解析 m3u8，原始=%s", ep, repr(cells[idx])[:200])

    return out


def resolve_from_change(change: dict[str, Any], base_url: str | None = None) -> dict[int, str]:
    base = (base_url or os.environ.get("BASE_URL") or "").strip().rstrip("/")
    if not base:
        logging.error("未设置 BASE_URL")
        return {}
    return resolve_new_episodes_m3u8(
        base_url=base,
        item_key=str(change.get("key") or ""),
        title=str(change.get("title") or ""),
        display_name=str(change.get("display_name") or ""),
        source_name=str(change.get("source_name") or ""),
        source_id=str(change.get("source_id") or ""),
        vod_id=str(change.get("vod_id") or ""),
        old_total=int(change.get("oldTotal") or 0),
        new_total=int(change.get("newTotal") or 0),
    )
