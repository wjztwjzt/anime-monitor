"""Load 配置.yaml from project root (parent of tg_chat_sim)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tg_chat_sim.config import Settings, TelegramAccountConfig


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_config_path() -> Path:
    return project_root() / "配置.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def _nama_session_order() -> list[str]:
    nama_path = Path(__file__).resolve().parent / "nama.json"
    if not nama_path.exists():
        return []
    raw_text = nama_path.read_text(encoding="utf-8")
    sanitized = re.sub(r",\s*([}\]])", r"\1", raw_text)
    try:
        payload = json.loads(sanitized) if sanitized.strip() else {}
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    return list(payload.keys())


def _parse_startup_profile(raw: Any) -> list[tuple[int, str]]:
    """List items like {1: '名字.txt'} -> [(1, '名字.txt'), ...]."""
    out: list[tuple[int, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            try:
                flag = int(k)
            except (TypeError, ValueError):
                flag = 0
            path = str(v).strip() if v is not None else ""
            if path:
                out.append((flag, path))
    return out


def _parse_startup_join(raw: Any) -> tuple[int, str]:
    """'1|加群列表.txt' -> (1, '加群列表.txt')."""
    if raw is None:
        return (0, "")
    s = str(raw).strip()
    if not s:
        return (0, "")
    if "|" in s:
        a, b = s.split("|", 1)
        try:
            return int(a.strip()), b.strip()
        except ValueError:
            return (0, b.strip())
    try:
        return int(s), ""
    except ValueError:
        return (0, s)


@dataclass
class ProjectYamlSettings:
    account_list_file: str = ""
    login_sessions_dir: str = "sessions"
    startup_profile: list[tuple[int, str]] = field(default_factory=list)
    startup_join_flag: int = 0
    startup_join_file: str = ""


def load_project_yaml(path: Path | None = None) -> ProjectYamlSettings:
    root = project_root()
    data = _read_yaml(path or default_config_path())
    account_list = str(data.get("账号列表文件") or "").strip()
    sessions_dir = str(data.get("登录状态目录") or "sessions").strip() or "sessions"
    startup_profile = _parse_startup_profile(data.get("启动时更新资料"))
    join_flag, join_file = _parse_startup_join(data.get("启动时加群"))
    return ProjectYamlSettings(
        account_list_file=account_list,
        login_sessions_dir=sessions_dir,
        startup_profile=startup_profile,
        startup_join_flag=join_flag,
        startup_join_file=join_file,
    )


def load_telegram_accounts_from_yaml(settings: Settings) -> list[TelegramAccountConfig]:
    """
    Build accounts from 配置.yaml + 账号列表.txt + nama.json key order.
    Lines: 手机号|验证码API[|二次密码可选]
    """
    root = project_root()
    py = load_project_yaml()
    if not py.account_list_file:
        return []

    list_path = (root / py.account_list_file).resolve()
    if not list_path.is_file():
        return []

    nama_keys = _nama_session_order()
    accounts: list[TelegramAccountConfig] = []
    entry_index = 0
    for line in list_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            continue
        phone, rest = line.split("|", 1)
        phone = phone.strip()
        rest_parts = rest.split("|", 1)
        code_url = rest_parts[0].strip()
        two_fa = rest_parts[1].strip() if len(rest_parts) > 1 else ""

        if not phone or not code_url:
            continue

        if entry_index < len(nama_keys):
            session_name = nama_keys[entry_index]
        else:
            session_name = f"tg_user_{entry_index + 1}"
        entry_index += 1

        accounts.append(
            TelegramAccountConfig(
                session_name=session_name,
                phone=phone,
                code_api_url=code_url,
                two_fa_password=two_fa,
            )
        )

    cap = max(1, int(settings.max_active_accounts))
    return accounts[:cap]


def read_text_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def parse_join_groups_one_line(line: str) -> list[str]:
    """
    一行内的多个群/频道：用英文逗号「,」或全角逗号（U+FF0C，中文输入法「，」）分隔。
    若整行没有任何逗号，则按空白分隔。
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return []
    normalized = s.replace("\uFF0C", ",")
    if "," in normalized:
        parts = normalized.split(",")
    else:
        parts = re.split(r"\s+", s)
    return [p.strip().strip("\"'") for p in parts if p.strip().strip("\"'")]


def read_join_group_lines(path: Path) -> list[str]:
    """将文件内所有非空行的群链接展开为一维列表（与 parse_join_groups_one_line 规则一致）。"""
    if not path.is_file():
        return []
    groups: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        groups.extend(parse_join_groups_one_line(line))
    return groups


def read_text_lines_by_account_index(path: Path) -> list[str]:
    """
    一行对应一个账号（含空行：该账号该项留空）。
    整行以 # 开头视为该账号空值。
    """
    if not path.is_file():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            out.append("")
        else:
            out.append(s)
    return out


def read_join_groups_per_account(path: Path) -> list[list[str]]:
    """
    加群列表：每行对应一个账号；一行内多个群或频道用英文逗号或全角逗号（U+FF0C）隔开。
    """
    if not path.is_file():
        return []
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            rows.append([])
            continue
        rows.append(parse_join_groups_one_line(s))
    return rows


_AVATAR_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def read_avatar_folder_path_from_txt(avatar_txt_path: Path) -> str:
    """头像.txt：内容为头像所在文件夹路径（取首个非空非注释行）。"""
    if not avatar_txt_path.is_file():
        return ""
    for line in avatar_txt_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    return ""


def list_avatar_files_in_folder(folder: Path, limit: int = 256) -> list[Path]:
    """文件夹内图片按文件名排序，依次对应第 1、2…个账号。"""
    if not folder.is_dir():
        return []
    files = [
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in _AVATAR_SUFFIXES
    ]
    files.sort(key=lambda p: p.name.lower())
    return files[:limit]
