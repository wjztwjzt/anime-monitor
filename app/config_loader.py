from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.paths import config_yaml_path, project_root


def load_config(path: Path | None = None) -> dict[str, Any]:
    p = path or config_yaml_path()
    if not p.is_file():
        raise FileNotFoundError(f"缺少配置文件: {p}")
    raw = p.read_text(encoding="utf-8-sig")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml 根节点必须是 mapping")
    return data


def resolve_rel(root: Path, s: str) -> Path:
    p = Path(s.strip())
    return (root / p).resolve() if not p.is_absolute() else p.resolve()


def database_path(cfg: dict[str, Any]) -> Path:
    root = project_root()
    db = cfg.get("database") or {}
    if not isinstance(db, dict):
        db = {}
    rel = str(db.get("path") or "data/app_state.sqlite").strip()
    return resolve_rel(root, rel)
