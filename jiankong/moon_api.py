"""
MoonTVPlus API 客户端 (moon.658877.xyz)。

搜索接口直接返回丰富元数据（year, douban_id, class, source_name, episodes 数组等），
一次调用即可完成搜索+精确匹配+获取 m3u8 URL，无需单独的 m3u8 解析步骤。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from jiankong.moontv_http import moon_tv_get, search_referer_query

logger = logging.getLogger(__name__)

# 短剧 / 剧场版 / OVA 等「不同剧」关键词
_DIFFERENT_SHOW_INDICATORS = [
    "短剧", "剧场版", "ova", "番外", "特别篇", "外传", "前传",
    "sp", "总集篇", "番外篇", "oad", "oad版",
]


@dataclass
class MoonShowResult:
    """moon.658877.xyz /api/search 单条搜索结果。"""

    id: str
    title: str
    poster: str = ""
    episodes: list[str] = field(default_factory=list)
    episodes_titles: list[str] = field(default_factory=list)
    source: str = ""
    source_name: str = ""
    class_tags: list[str] = field(default_factory=list)
    year: str = ""
    desc: str = ""
    type_name: str = ""
    douban_id: str = ""
    vod_remarks: str = ""
    vod_total: int = 0
    proxy_mode: bool = False
    weight: int = 0

    @property
    def episode_count(self) -> int:
        return len(self.episodes)

    @property
    def year_int(self) -> int:
        try:
            return int(self.year)
        except (TypeError, ValueError):
            return 0


# ---- 解析 ----

def _flatten_search_results(data: Any) -> list[dict[str, Any]]:
    """兼容多种 JSON 结构，提取搜索结果数组。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("results", "list", "data", "items", "records"):
        v = data.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _parse_class_tags(raw: str) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()]


def _coerce_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _to_str(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def parse_moon_search_response(data: Any) -> list[MoonShowResult]:
    rows = _flatten_search_results(data)
    results: list[MoonShowResult] = []
    for row in rows:
        episodes_raw = row.get("episodes") or []
        if isinstance(episodes_raw, list):
            episodes = [_to_str(u) for u in episodes_raw if _to_str(u).startswith("http")]
        else:
            episodes = []

        titles_raw = row.get("episodes_titles") or []
        if isinstance(titles_raw, list):
            episodes_titles = [_to_str(t) for t in titles_raw]
        else:
            episodes_titles = []

        results.append(MoonShowResult(
            id=_to_str(row.get("id")),
            title=_to_str(row.get("title")),
            poster=_to_str(row.get("poster")),
            episodes=episodes,
            episodes_titles=episodes_titles,
            source=_to_str(row.get("source")),
            source_name=_to_str(row.get("source_name")),
            class_tags=_parse_class_tags(row.get("class") or ""),
            year=_to_str(row.get("year")),
            desc=_to_str(row.get("desc")),
            type_name=_to_str(row.get("type_name")),
            douban_id=_to_str(row.get("douban_id")),
            vod_remarks=_to_str(row.get("vod_remarks")),
            vod_total=_coerce_int(row.get("vod_total")),
            proxy_mode=bool(row.get("proxyMode")),
            weight=_coerce_int(row.get("weight")),
        ))
    return results


# ---- 搜索 ----

def search_moon_api(
    keyword: str,
    base_url: str,
    *,
    max_retries: int = 3,
    retry_delay: float = 5.0,
) -> list[MoonShowResult]:
    """搜索 moon.658877.xyz，返回解析后的结果列表。带重试机制。"""
    q = keyword.strip()
    if not q:
        return []

    for attempt in range(max_retries):
        if attempt > 0:
            logger.info("MoonTVPlus 搜索重试 %s/%s: %s", attempt + 1, max_retries, q)
            time.sleep(retry_delay)

        try:
            r = moon_tv_get(
                base_url,
                "/api/search",
                {"q": q},
                referer_path=search_referer_query(q),
            )
            if r.status_code != 200:
                logger.error("MoonTVPlus HTTP %s: %s", r.status_code, r.text[:400])
                continue
            data = r.json()
        except Exception:
            logger.exception("MoonTVPlus 请求失败 q=%s (attempt %s)", q, attempt + 1)
            continue

        results = parse_moon_search_response(data)
        if results:
            return results

        if attempt < max_retries - 1:
            logger.info("MoonTVPlus 搜索返回空，重试: %s", q)

    logger.warning("MoonTVPlus 搜索无结果（已重试 %s 次）: %s", max_retries, q)
    return []


# ---- 匹配评分 ----

def _has_different_show_indicator(title: str, keyword: str) -> bool:
    """标题含 keyword 但额外部分标记为不同剧（短剧/剧场版等）。"""
    extra = title.lower().replace(keyword.lower(), "", 1).strip()
    if not extra:
        return False
    return any(ind in extra for ind in _DIFFERENT_SHOW_INDICATORS)


def _title_length_ok(keyword: str, title: str) -> bool:
    """子串匹配时长度的合理性检查：防止「盘龙」匹配「盘龙卧虎高山顶」。"""
    k = keyword.lower().strip()
    t = title.lower().strip()
    shorter = min(len(k), len(t))
    longer = max(len(k), len(t))
    if longer == 0:
        return False
    return shorter >= longer * 0.5


def score_match(
    result: MoonShowResult,
    *,
    title: str = "",
    douban_id: str = "",
    year_min: int = 0,
    year_max: int = 0,
    class_keywords: list[str] | None = None,
    source_preference: str = "",
    type_name: str = "",
) -> tuple[int, str]:
    """对搜索结果打分，返回 (分数, 原因)。

    分数 > 0 表示匹配，越高越可靠。分数 = 0 表示直接排除。
    计分策略：
      - douban_id 完全匹配: +1000（唯一标识，最高优先）
      - title 完全匹配: +500
      - keyword 是 title 的子串且长度比合理: +200
      - year 在范围内: +300（范围内）/-50（范围外，不排除）
      - class_keywords 命中: +50/条
      - source_preference 匹配: +200
      - type_name 匹配: +100
    """
    kw = title.lower().strip()
    rt = result.title.lower().strip()

    # 排除规则
    if not rt:
        return (0, "空白标题")
    if _has_different_show_indicator(result.title, title):
        return (0, "标题含不同剧标记（短剧/剧场版等）")
    if kw in rt and not _title_length_ok(title, result.title):
        return (0, f"标题长度比异常: {title!r} vs {result.title!r}")

    score = 0
    reasons: list[str] = []

    # douban_id（最高权重）
    if douban_id and result.douban_id:
        if result.douban_id == douban_id:
            score += 1000
            reasons.append("豆瓣ID匹配")
        else:
            return (0, f"豆瓣ID不符: 期望{douban_id} 实际{result.douban_id}")

    # title 匹配
    if kw == rt:
        score += 500
        reasons.append("标题完全匹配")
    elif kw in rt or rt in kw:
        score += 200
        reasons.append("标题包含匹配")

    # year
    yi = result.year_int
    if yi > 0:
        if year_min > 0 and year_max > 0:
            if year_min <= yi <= year_max:
                score += 300
                reasons.append(f"年份在范围({year_min}-{year_max})")
            else:
                score -= 50
        elif year_min > 0:
            if yi >= year_min:
                score += 300
                reasons.append(f"年份>={year_min}")
            else:
                score -= 50
    else:
        # 年份缺失则不加不减（有些剧未填年份）
        pass

    # class_keywords
    if class_keywords:
        matched = [k for k in class_keywords if k in result.class_tags]
        if matched:
            score += 50 * len(matched)
            reasons.append(f"类型匹配: {matched}")

    # source_preference
    if source_preference:
        sp = source_preference.lower().strip()
        rs = result.source_name.lower()
        if sp == rs:
            score += 200
            reasons.append(f"源匹配: {result.source_name}")

    # type_name
    if type_name:
        tn = type_name.lower().strip()
        rn = result.type_name.lower()
        if tn == rn:
            score += 100
            reasons.append(f"类型名匹配: {result.type_name}")

    # 没有任何 title 匹配也照常（可能在 desc 里命中）
    if score == 0 and (kw in result.desc.lower()):
        score = 10
        reasons.append("简介匹配")

    reason = "; ".join(reasons) if reasons else "弱匹配"
    return (score, reason)


def match_best_show(
    results: list[MoonShowResult],
    *,
    title: str = "",
    filters: dict[str, Any] | None = None,
) -> MoonShowResult | None:
    """从搜索结果中选出最佳匹配，返回 None 表示无匹配。

    filters 可选字段:
      douban_id, year_min, year_max, class_keywords,
      source_preference, type_name
    """
    if not results:
        return None

    f = filters or {}
    scored: list[tuple[int, str, MoonShowResult]] = []

    for r in results:
        s, reason = score_match(
            r,
            title=title,
            douban_id=_to_str(f.get("douban_id") or ""),
            year_min=_coerce_int(f.get("year_min")),
            year_max=_coerce_int(f.get("year_max")),
            class_keywords=f.get("class_keywords") if isinstance(f.get("class_keywords"), list) else None,
            source_preference=_to_str(f.get("source_preference") or ""),
            type_name=_to_str(f.get("type_name") or ""),
        )
        if s > 0:
            scored.append((s, reason, r))
        else:
            logger.debug("排除: [%s] %s — %s", r.id, r.title, reason)

    if not scored:
        logger.warning("搜索 %s 所有结果均被过滤器排除", title)
        return None

    scored.sort(key=lambda x: x[0], reverse=True)

    # 多结果时打印排行
    if len(scored) > 1:
        lines = []
        for s, reason, r in scored[:5]:
            lines.append(
                f"  score={s} [{r.source_name}] {r.title} "
                f"(id={r.id} year={r.year} douban={r.douban_id} eps={r.episode_count})"
            )
        logger.info("搜索 %s 匹配排行:\n%s", title, "\n".join(lines))

    best_score, best_reason, best = scored[0]
    logger.info(
        "搜索 %s → %s (score=%s, id=%s, year=%s, douban=%s, eps=%s, %s)",
        title, best.title, best_score,
        best.id, best.year, best.douban_id, best.episode_count, best_reason,
    )
    return best


# ---- 提取新增集 m3u8 URL ----

def extract_new_episode_m3u8s(
    result: MoonShowResult,
    old_total: int,
    new_total: int,
) -> dict[int, str]:
    """从搜索结果 episodes 数组提取 (old_total, new_total] 集的 m3u8 URL。

    episodes 是 0-indexed 数组（第 1 集 = index 0）。
    """
    urls: dict[int, str] = {}
    episodes = result.episodes or []

    for ep_num in range(old_total + 1, new_total + 1):
        idx = ep_num - 1
        if idx < 0 or idx >= len(episodes):
            logger.warning(
                "集数越界: %s 第%s集 (索引=%s) 超出 episodes 长度 %s",
                result.title, ep_num, idx, len(episodes),
            )
            continue
        url = episodes[idx].strip()
        if url.startswith("http"):
            urls[ep_num] = url
        else:
            logger.warning("episodes[%s] 非 http 链接: %s", idx, url[:80])

    return urls
