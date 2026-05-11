"""收藏 item_key（MoonTV source+id）→ config.yaml 里 shows[].id（show_id）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# 与 config.yaml 中 shows[].id 一致，例如「37+97662」→「zhetian」
ITEM_KEY_TO_SHOW_ID: dict[str, str] = {
    # 须与 config.yaml -> shows[].id 一致，且 shows[].moon_item_key 对应收藏键
    "23+81489": "panlong",
    "37+97662": "zetianji",
    "maotaizy+91": "mushenji",  
    "maotaizy+70658": "zhetian",
    "16+121988": "wanmeishijie",
    "29+72574": "cangyuantu",
    "jisu+106821": "xingchenbian",
    "jisu+105664": "jianye",
}


def load_item_key_to_show_id() -> dict[str, str]:
    raw = dict(ITEM_KEY_TO_SHOW_ID)
    env_json = (os.environ.get("PIPELINE_SHOW_MAP_JSON") or "").strip()
    if env_json:
        try:
            extra: Any = json.loads(env_json)
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        raw[str(k)] = v.strip()
        except json.JSONDecodeError:
            pass
    return {k.strip(): v.strip() for k, v in raw.items()}
