import asyncio
import random
from typing import Dict, List

from telethon import TelegramClient

from tg_chat_sim.config import Settings
from tg_chat_sim.storage import ChatScriptMessageRecord, StorageManager


def _looks_like_cannot_write(exc: Exception) -> bool:
    s = str(exc).lower()
    return (
        "can't write" in s
        or "you can't write" in s
        or "chat_write_forbidden" in s
        or "user_banned_in_channel" in s
        or "write forbidden" in s
    )


def _looks_like_flood_wait(exc: Exception) -> bool:
    s = str(exc).lower()
    return "floodwait" in s or "flood wait" in s


def _looks_like_connection_reset(exc: Exception) -> bool:
    s = str(exc).lower()
    return (
        "connection reset by peer" in s
        or "server closed the connection" in s
        or isinstance(exc, ConnectionResetError)
    )


async def send_chat_script_once(
    *,
    clients: List[TelegramClient],
    speaker_index_to_client: Dict[int, TelegramClient],
    settings: Settings,
    storage: StorageManager,
    stop_event: asyncio.Event,
) -> None:
    send_cap = int(settings.chat_collect_send_reserve_limit)
    reserve_limit: int | None = send_cap if send_cap > 0 else None
    reserved = await storage.reserve_unsent_collected_messages(reserve_limit)
    messages: list[ChatScriptMessageRecord] = []
    if reserved:
        step = max(1, int(getattr(settings, "chat_script_row_interval_seconds", 15)))
        speaker_count = max(1, len(speaker_index_to_client))
        from datetime import datetime, timedelta

        now = datetime.utcnow()
        for idx, row in enumerate(reserved):
            # If reserve_limit=1, using idx would always assign speaker 1.
            # Use a stable per-row rotation so even single-message batches spread across accounts.
            basis = int(getattr(row, "id", 0) or 0) or (idx + 1)
            messages.append(
                ChatScriptMessageRecord(
                    script_id=1,
                    speaker_index=(basis % speaker_count) + 1,
                    message_time=now + timedelta(seconds=idx * step),
                    text_content=row.text_content,
                    collected_message_id=row.id,
                )
            )
    else:
        messages = await storage.load_active_chat_script_messages(str(settings.target_group))
    if not messages:
        path = settings.chat_records_path
        print(
            "未加载到可发送的聊天内容："
            f"storage_backend={settings.storage_backend}，"
            f"xlsx={path}（存在={path.exists()}）。"
            "请把 chat_records.xlsx 放到该路径并保证有数据行；"
            "若用 SQLite 脚本，请在库中配置 active 的 chat_script；"
            "也可设置 CHAT_RECORDS_XLSX 指向实际文件。"
        )
        return

    target_entities: Dict[int, object] = {}

    first_time = messages[0].message_time
    loop = asyncio.get_running_loop()
    start_monotonic = loop.time()

    async def _ensure_join_target_group(client: TelegramClient) -> None:
        try:
            entity = await client.get_entity(settings.target_group)
            from telethon.tl.functions.channels import JoinChannelRequest

            await client(JoinChannelRequest(entity))
        except Exception:
            # If already joined / no permission / not a channel, ignore.
            pass

    # Resolve per-client entity (Telethon entities are client-scoped).
    async def get_target_entity(client: TelegramClient) -> object:
        key = id(client)
        if key not in target_entities:
            target_entities[key] = await client.get_entity(settings.target_group)
        return target_entities[key]

    # Ensure each logged-in account has joined the target group/channel.
    # Otherwise Telethon may allow resolving entity but cannot write, causing all messages
    # to be sent by a single account with permission.
    for cli in clients:
        await _ensure_join_target_group(cli)

    # Some accounts may not have permission to write (not joined, muted, banned, etc).
    # We keep them in the pool but skip them after the first "cannot write" failure.
    disabled_client_ids: set[int] = set()

    for msg_idx, msg in enumerate(messages):
        if stop_event.is_set():
            rest_ids = [
                m.collected_message_id
                for m in messages[msg_idx:]
                if m.collected_message_id is not None
            ]
            if rest_ids:
                await storage.mark_collected_messages_unused(rest_ids)
            print("收到停止信号，终止聊天脚本发送。")
            return

        desired_elapsed = (msg.message_time - first_time).total_seconds()
        elapsed = loop.time() - start_monotonic
        wait_seconds = desired_elapsed - elapsed

        while wait_seconds > 0 and not stop_event.is_set():
            step = min(wait_seconds, 1.0)
            await asyncio.sleep(step)
            elapsed = loop.time() - start_monotonic
            wait_seconds = desired_elapsed - elapsed

        if stop_event.is_set():
            rest_ids = [
                m.collected_message_id
                for m in messages[msg_idx:]
                if m.collected_message_id is not None
            ]
            if rest_ids:
                await storage.mark_collected_messages_unused(rest_ids)
            return

        primary = speaker_index_to_client.get(msg.speaker_index)
        if primary is None:
            raise RuntimeError(f"缺少 speaker_index={msg.speaker_index} 对应的已登录账号")
        text = msg.text_content

        sent_ok = False
        used_client: TelegramClient | None = None
        try:
            # Try primary first, then other accounts in a stable order.
            pool: list[TelegramClient] = []
            if id(primary) not in disabled_client_ids:
                pool.append(primary)
            for alt in clients:
                if alt is primary:
                    continue
                if id(alt) in disabled_client_ids:
                    continue
                pool.append(alt)

            for attempt_idx, cli in enumerate(pool):
                try:
                    entity = await get_target_entity(cli)
                    try:
                        await cli.send_message(entity, text)
                    except Exception as exc:
                        if _looks_like_connection_reset(exc):
                            # Best-effort reconnect once.
                            try:
                                await cli.connect()
                            except Exception:
                                pass
                            await cli.send_message(entity, text)
                        else:
                            raise
                    sent_ok = True
                    used_client = cli
                    break
                except Exception as exc:
                    if _looks_like_cannot_write(exc):
                        disabled_client_ids.add(id(cli))
                        # Non-retryable for this account; try next.
                        continue
                    if _looks_like_flood_wait(exc) and attempt_idx == 0:
                        # If primary hits FloodWait, do not immediately spam others.
                        # Let the caller loop handle pacing; return message back to pool.
                        raise
                    # Other errors: try next account, but keep this account in pool.
                    continue
        except Exception as exc:
            if not sent_ok:
                print(f"发送失败: speaker={msg.speaker_index}, idx={msg_idx}, err={exc}")

        if msg.collected_message_id is not None:
            if sent_ok:
                await storage.delete_collected_messages_by_ids([msg.collected_message_id])
            else:
                await storage.mark_collected_messages_unused([msg.collected_message_id])

        lo = int(getattr(settings, "chat_after_send_min_seconds", 0) or 0)
        hi = int(getattr(settings, "chat_after_send_max_seconds", 0) or 0)
        if hi > 0 and not stop_event.is_set():
            if lo > hi:
                lo, hi = hi, lo
            extra = random.uniform(float(lo), float(hi))
            acc = 0.0
            while acc < extra and not stop_event.is_set():
                step = min(1.0, extra - acc)
                await asyncio.sleep(step)
                acc += step

