"""项目根目录（含 config.yaml）解析。"""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    env = (os.environ.get("FASTTELETHON_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    for p in [here.parent.parent, *here.parent.parents]:
        if (p / "config.yaml").is_file():
            return p
    return here.parent.parent


def config_yaml_path() -> Path:
    return project_root() / "config.yaml"
