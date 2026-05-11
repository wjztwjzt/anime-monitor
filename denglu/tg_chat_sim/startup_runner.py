"""On startup: optional profile + join from 配置.yaml (flags 0/1)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tg_chat_sim.project_yaml import (
    list_avatar_files_in_folder,
    load_project_yaml,
    project_root,
    read_avatar_folder_path_from_txt,
    read_join_groups_per_account,
    read_text_lines_by_account_index,
)

if TYPE_CHECKING:
    from tg_chat_sim.storage import RuntimeAccountRecord
    from tg_chat_sim.telethon_manager import TelethonManager


def _classify_profile_file(fname: str) -> str | None:
    """Return 'name' | 'bio' | 'avatar' | None."""
    stem = Path(fname).stem
    if "名字" in fname or stem == "名字":
        return "name"
    if "简介" in fname or stem == "简介":
        return "bio"
    if "头像" in fname or stem == "头像":
        return "avatar"
    return None


async def run_startup_from_yaml(
    tg_manager: TelethonManager,
    runtime_accounts: list[RuntimeAccountRecord],
    client_by_session: dict[str, Any],
) -> None:
    root = project_root()
    py = load_project_yaml()

    profile_specs: list[tuple[str, Path]] = []
    for flag, fname in py.startup_profile:
        if int(flag) != 1:
            continue
        kind = _classify_profile_file(fname)
        if not kind:
            print(f"STARTUP_YAML: 跳过未知资料项: {fname}")
            continue
        profile_specs.append((kind, (root / fname).resolve()))

    if profile_specs:
        name_lines: list[str] = []
        bio_lines: list[str] = []
        avatar_files: list[Path] = []

        for kind, path in profile_specs:
            if kind == "name":
                name_lines = read_text_lines_by_account_index(path)
            elif kind == "bio":
                bio_lines = read_text_lines_by_account_index(path)
            elif kind == "avatar":
                rel = read_avatar_folder_path_from_txt(path)
                if rel:
                    folder = (
                        Path(rel).resolve()
                        if Path(rel).is_absolute()
                        else (root / rel).resolve()
                    )
                    avatar_files = list_avatar_files_in_folder(folder)
                else:
                    avatar_files = []

        for idx, acc in enumerate(runtime_accounts):
            cli = client_by_session.get(acc.session_name)
            if cli is None:
                continue
            nickname = name_lines[idx] if idx < len(name_lines) else ""
            about = bio_lines[idx] if idx < len(bio_lines) else ""
            avatar_path_str = ""
            if idx < len(avatar_files):
                avatar_path_str = str(avatar_files[idx].resolve())

            try:
                if nickname or about:
                    await tg_manager.apply_bio_for_account(cli, nickname, about)
                if avatar_path_str:
                    await tg_manager.apply_avatar_for_account(cli, avatar_path_str)
                if nickname or about or avatar_path_str:
                    await asyncio.sleep(1.0)
            except Exception as exc:
                print(f"STARTUP_PROFILE: {acc.session_name} error={exc}")

        print("STARTUP_YAML: 启动时资料更新已按 配置.yaml 执行。")

    if int(py.startup_join_flag) == 1 and py.startup_join_file.strip():
        join_path = (root / py.startup_join_file.strip()).resolve()
        groups_by_account = read_join_groups_per_account(join_path)
        for idx, acc in enumerate(runtime_accounts):
            cli = client_by_session.get(acc.session_name)
            if cli is None:
                continue
            groups = groups_by_account[idx] if idx < len(groups_by_account) else []
            if not groups:
                continue
            try:
                await tg_manager.join_groups_for_client(cli, groups)
            except Exception as exc:
                print(f"STARTUP_JOIN: {acc.session_name} error={exc}")
            await asyncio.sleep(2.0)
        print("STARTUP_YAML: 启动时加群已按 配置.yaml 执行（每行对应一个账号）。")
