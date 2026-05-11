"""向 xiazai/dongman.yaml 追加新集 m3u8，并写入 state.episodes。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml


def _normalize_state_episode_keys(cfg: dict[str, Any]) -> None:
    state = cfg.get("state")
    if not isinstance(state, dict):
        return
    ep = state.get("episodes")
    if not isinstance(ep, dict) or not ep:
        return
    state["episodes"] = {str(k): v for k, v in ep.items()}


def load_dongman(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = path.read_text(encoding="utf-8-sig")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} 根节点必须是 mapping")
    _normalize_state_episode_keys(data)
    return data


def save_dongman(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def append_episode_urls(
    yaml_path: Path,
    episode_to_url: dict[int, str],
    *,
    old_total_hint: int,
    new_total_hint: int,
) -> list[int]:
    """
    合并新集链接：urls 列表第 i 项对应第 i+1 集。
    episode_to_url 的键应为 old_total_hint+1 … new_total_hint。
    """
    if not episode_to_url:
        return []

    cfg = load_dongman(yaml_path)
    urls_obj = cfg.get("urls")
    if not isinstance(urls_obj, list):
        urls_obj = []
    urls: list[str] = []
    for u in urls_obj:
        if isinstance(u, str) and u.strip():
            urls.append(u.strip())

    n_before = len(urls)
    if n_before != old_total_hint:
        logging.warning(
            "dongman.yaml 当前 urls 条数=%s，与收藏旧集数 old_total=%s 不一致，仍尝试写入新集",
            n_before,
            old_total_hint,
        )

    written: list[int] = []
    for ep in sorted(episode_to_url.keys()):
        if ep <= old_total_hint or ep > new_total_hint:
            logging.warning("跳过集数 %s（不在 (%s,%s]）", ep, old_total_hint, new_total_hint)
            continue
        u = episode_to_url[ep].strip()
        if not u.startswith("http://") and not u.startswith("https://"):
            logging.error("第 %s 集 URL 无效，跳过: %s", ep, u[:80])
            continue
        idx = ep - 1
        if idx == len(urls):
            urls.append(u)
        elif idx < len(urls):
            urls[idx] = u
        else:
            while len(urls) < idx:
                logging.warning("urls 在集 %s 前出现空洞，已用占位填满（请检查）", len(urls) + 1)
                urls.append("")
            urls.append(u)
        written.append(ep)

    cfg["urls"] = urls

    episodes = cfg.setdefault("state", {}).setdefault("episodes", {})
    if not isinstance(episodes, dict):
        cfg.setdefault("state", {})["episodes"] = {}
        episodes = cfg["state"]["episodes"]

    for ep in written:
        episodes[str(ep)] = {"url": episode_to_url[ep].strip(), "status": "pending"}

    save_dongman(yaml_path, cfg)

    logging.info(
        "已更新 %s：新集 %s，urls %s→%s",
        yaml_path.name,
        written,
        n_before,
        len(urls),
    )
    return written
