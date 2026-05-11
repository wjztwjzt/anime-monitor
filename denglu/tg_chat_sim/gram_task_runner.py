import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tg_chat_sim.telethon_manager import TelethonManager


@dataclass
class GramTaskRecord:
    session_name: str
    changes: int
    join_groups: list[str | int]
    message_text: str
    image_path: str
    profile_bio: str


def _parse_join_groups(raw_value: Any) -> list[str | int]:
    if isinstance(raw_value, list):
        raw_items = raw_value
    else:
        raw_text = str(raw_value or "").strip()
        if not raw_text:
            return []
        if raw_text.startswith("[") and raw_text.endswith("]"):
            raw_text = raw_text[1:-1].strip()
        raw_items = re.split(r"[，,]", raw_text)

    groups: list[str | int] = []
    for item in raw_items:
        text = str(item).strip().strip("\"'").strip()
        if not text:
            continue
        if re.fullmatch(r"-?\d+", text):
            try:
                groups.append(int(text))
                continue
            except ValueError:
                pass
        groups.append(text)
    return groups


def load_gram_tasks(gram_path: str | None = None) -> dict[str, GramTaskRecord]:
    path = Path(gram_path) if gram_path else Path(__file__).with_name("gram.json")
    if not path.exists():
        return {}

    raw_text = path.read_text(encoding="utf-8")
    sanitized = re.sub(r",\s*([}\]])", r"\1", raw_text)
    payload = json.loads(sanitized) if sanitized.strip() else {}
    if not isinstance(payload, dict):
        return {}

    result: dict[str, GramTaskRecord] = {}
    for session_name, info in payload.items():
        if not isinstance(info, dict):
            continue
        result[str(session_name)] = GramTaskRecord(
            session_name=str(session_name),
            changes=int(info.get("changes", 0) or 0),
            join_groups=_parse_join_groups(info.get("join_groups")),
            message_text=str(info.get("message_text") or "").strip(),
            image_path=str(info.get("image_path") or "").strip(),
            profile_bio=str(info.get("profile_bio") or "").strip(),
        )
    return result


async def run_gram_tasks_once(
    *,
    client_by_session: dict[str, object],
    tg_manager: TelethonManager,
    gram_path: str | None = None,
    per_group_delay_seconds: float = 1.0,
) -> None:
    tasks = load_gram_tasks(gram_path)
    if not tasks:
        print("GRAM_TASK: gram.json 为空或不存在，跳过。")
        return

    for session_name, task in tasks.items():
        if task.changes != 1:
            continue

        client = client_by_session.get(session_name)
        if client is None:
            print(f"GRAM_TASK: 找不到账号客户端，跳过 {session_name}")
            continue

        if task.profile_bio:
            try:
                await tg_manager.apply_bio_for_account(client, task.profile_bio)
            except Exception as exc:
                print(f"GRAM_TASK: 修改简介失败，账号 {session_name}，error={exc}")

        if not task.join_groups or not task.message_text:
            print(
                f"GRAM_TASK: 账号 {session_name} 缺少必填字段 join_groups/message_text，跳过发言。"
            )
            continue

        for group in task.join_groups:
            try:
                if task.image_path:
                    await client.send_file(
                        entity=group,
                        file=task.image_path,
                        caption=task.message_text,
                    )
                else:
                    await client.send_message(entity=group, message=task.message_text)
            except Exception as exc:
                print(f"GRAM_TASK: 发言失败，账号 {session_name}，群 {group}，error={exc}")
            finally:
                if per_group_delay_seconds > 0:
                    await asyncio.sleep(per_group_delay_seconds)
