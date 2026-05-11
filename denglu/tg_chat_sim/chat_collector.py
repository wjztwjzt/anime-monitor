import asyncio
import logging
import re
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.errors import UserAlreadyParticipantError, UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, Chat, Message, User

from tg_chat_sim.config import Settings
from tg_chat_sim.storage import StorageManager

logger = logging.getLogger(__name__)


class ProfileAdFilter:
    def __init__(self) -> None:
        self.url_pattern = re.compile(r"https?://\S+", re.IGNORECASE)
        self.tme_pattern = re.compile(r"(?:https?://)?t\.me/[A-Za-z0-9_]+", re.IGNORECASE)
        self.at_pattern = re.compile(r"@[A-Za-z0-9_]+")

    def has_profile_ad_pattern(self, name_text: str, bio_text: str) -> bool:
        if name_text and (
            self.url_pattern.search(name_text) or self.tme_pattern.search(name_text)
        ):
            return True
        if bio_text and (
            self.url_pattern.search(bio_text)
            or self.tme_pattern.search(bio_text)
            or self.at_pattern.search(bio_text)
        ):
            return True
        return False


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _strip_links_and_mentions(text: str) -> str:
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:https?://)?t\.me/\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"@[A-Za-z0-9_]+", " ", text)
    return _normalize_text(text)


def _display_name(u: User) -> str:
    first = (u.first_name or "").strip()
    last = (u.last_name or "").strip()
    name = f"{first} {last}".strip()
    return name or (u.username or "").strip() or str(u.id)


async def _get_profile_texts(client: TelegramClient, user_id: int) -> tuple[str, str]:
    name_text = ""
    bio_text = ""
    try:
        u = await client.get_entity(user_id)
        display_name = " ".join(
            p
            for p in [getattr(u, "first_name", "") or "", getattr(u, "last_name", "") or ""]
            if p
        ).strip()
        username = f"@{u.username}" if getattr(u, "username", None) else ""
        name_text = " ".join(p for p in [display_name, username] if p).strip()
    except Exception:
        pass
    try:
        full = await client(GetFullUserRequest(user_id))
        if full and full.full_user:
            bio_text = full.full_user.about or ""
    except Exception:
        pass
    return name_text, bio_text


async def _message_to_row(
    *,
    client: TelegramClient,
    msg: Message,
    profile_filter: ProfileAdFilter,
    sender_skip_cache: dict[int, bool],
    min_chars: int,
    max_chars: int,
    debug: bool = False,
) -> dict | None:
    raw_text = _normalize_text(getattr(msg, "message", "") or "")
    if not raw_text:
        if debug:
            print("CHAT_COLLECT_DEBUG: 过滤原因=empty_raw_text")
        return None
    # 按原有过滤逻辑：剥离链接/提及后，再做长度限制。
    text = _strip_links_and_mentions(raw_text)
    if not text or not (min_chars <= len(text) <= max_chars):
        if debug:
            print(
                f"CHAT_COLLECT_DEBUG: 过滤原因=text_empty_or_len_out len(text)={len(text)} min={min_chars} max={max_chars}"
            )
        return None

    sender = None
    try:
        sender = await msg.get_sender()
    except KeyError:
        sender = None
    except Exception:
        sender = None

    # Prefer resolved sender entity; otherwise fall back to sender_id.
    sid_raw = getattr(sender, "id", None) if sender is not None else None
    if sid_raw is None:
        sid_raw = getattr(msg, "sender_id", None)
    if sid_raw is None:
        if debug:
            print("CHAT_COLLECT_DEBUG: 过滤原因=missing_sender_id")
        return None

    sid = int(sid_raw)
    if sender is not None and bool(getattr(sender, "bot", False)):
        if debug:
            print("CHAT_COLLECT_DEBUG: 过滤原因=sender_is_bot")
        return None
    cached = sender_skip_cache.get(sid)
    if cached is None:
        name_text, bio_text = await _get_profile_texts(client, sid)
        cached = profile_filter.has_profile_ad_pattern(name_text, bio_text)
        sender_skip_cache[sid] = cached
    if cached:
        if debug:
            print("CHAT_COLLECT_DEBUG: 过滤原因=profile_ad_pattern")
        return None

    msg_date = getattr(msg, "date", None)
    if msg_date is None:
        if debug:
            print("CHAT_COLLECT_DEBUG: 过滤原因=missing_msg_date")
        return None
    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)
    mid = int(getattr(msg, "id", 0) or 0)
    return {
        "source_message_id": mid,
        "message_date": msg_date.astimezone(timezone.utc).replace(tzinfo=None),
        "sender_id": sid,
        "sender_username": (getattr(sender, "username", "") or "").strip()
        if sender is not None
        else "",
        "sender_display_name": _display_name(sender) if sender is not None else str(sid),
        "text_content": text,
    }


def _invite_hash_from_link(group_value: str) -> str | None:
    gv = str(group_value).strip()
    if not gv:
        return None
    if "joinchat/" in gv or "/+" in gv:
        h = gv.rstrip("/").split("/")[-1].replace("+", "")
        return h or None
    return None


async def ensure_join_collect_target(client: TelegramClient, group: str) -> None:
    """
    采集监听专用：解析 CHAT_COLLECT_TARGET_GROUP 中的每一项，若未加入则尝试加入群/频道。
    与 配置.yaml「启动时加群」无关；采集任务启动时总会执行。
    已加入则不再调用加入接口（先 GetParticipant，邀请链则依赖 UserAlreadyParticipantError）。
    """
    gv = (group or "").strip()
    if not gv:
        return

    invite_hash = _invite_hash_from_link(gv)
    if invite_hash is not None:
        try:
            await client(ImportChatInviteRequest(hash=invite_hash))
            logger.info("CHAT_COLLECT_JOIN: 已通过邀请加入监听目标: %s", gv[:72])
        except UserAlreadyParticipantError:
            logger.info("CHAT_COLLECT_JOIN: 已在群内（邀请），跳过: %s", gv[:72])
        except Exception as exc:
            logger.warning("CHAT_COLLECT_JOIN: 邀请加入失败 %s error=%s", gv[:72], exc)
        return

    try:
        entity = await client.get_entity(gv)
    except Exception as exc:
        logger.warning("CHAT_COLLECT_JOIN: 无法解析目标 %r error=%s", gv, exc)
        return

    if isinstance(entity, Channel):
        try:
            me = await client.get_input_entity("me")
            await client(GetParticipantRequest(channel=entity, participant=me))
            logger.info("CHAT_COLLECT_JOIN: 已在群内，跳过加入: %s", gv)
            return
        except UserNotParticipantError:
            pass
        except Exception as exc:
            # 非成员时部分环境可能不是 UserNotParticipantError，继续尝试 Join。
            logger.info(
                "CHAT_COLLECT_JOIN: 成员检查未确认已加入，将尝试 Join: %s (%s)",
                gv,
                exc,
            )
        try:
            await client(JoinChannelRequest(entity))
            logger.info("CHAT_COLLECT_JOIN: 已加入频道/超级群: %s", gv)
        except UserAlreadyParticipantError:
            logger.info("CHAT_COLLECT_JOIN: 已在群内: %s", gv)
        except Exception as exc:
            logger.warning("CHAT_COLLECT_JOIN: 加入失败: %s error=%s", gv, exc)
        return

    if isinstance(entity, Chat):
        logger.warning(
            "CHAT_COLLECT_JOIN: 目标 %r 为基础群（非频道），"
            "Telegram 无法仅靠用户名自动加入，请用邀请链接配置或手动加群后再采集。",
            gv,
        )
        return

    logger.warning(
        "CHAT_COLLECT_JOIN: 未知实体类型，跳过自动加入: %r type=%s",
        gv,
        type(entity).__name__,
    )


async def run_live_collect_new_messages(
    *,
    client: TelegramClient,
    account_session_name: str,
    settings: Settings,
    storage: StorageManager,
) -> None:
    """
    在目标群上长期监听「新发言」：过滤机器人、资料广告、过短/过长文本，写入 chat_collected_messages。
    启动监听前对每个 CHAT_COLLECT_TARGET_GROUP 项执行 ensure_join_collect_target（与 yaml 启动加群无关）。
    不设条数上限；去重依赖库表 (source_group, source_message_id) 唯一约束。
    任务被取消时在 finally 中刷写残余批次并移除事件处理器。
    """
    # 目标群支持“列表”：逗号分隔多个群名/ID/链接
    # 例如：CHAT_COLLECT_TARGET_GROUP=@g1,@g2,@g3
    targets_raw = (settings.chat_collect_target_group or "").strip()
    targets = [t.strip() for t in targets_raw.split(",") if t.strip()]
    if not targets:
        return

    # 同一个 client 同时监听多个群：每个群各自注册一个 handler。
    event_handlers: list[tuple[object, object]] = []
    debug_seen = 0

    profile_filter = ProfileAdFilter()
    sender_skip_cache: dict[int, bool] = {}
    min_chars = max(2, int(settings.chat_collect_min_chars))
    max_chars = max(min_chars, int(settings.chat_collect_max_chars))

    for group in targets:
        await ensure_join_collect_target(client, group)
        event_builder = events.NewMessage(chats=group)

        async def handler(event: events.NewMessage.Event, source_group: str = group) -> None:
            msg = event.message
            if getattr(msg, "out", False):
                return

            nonlocal debug_seen
            debug_seen += 1
            if debug_seen <= 20:
                raw = _normalize_text(getattr(msg, "message", "") or "")
                preview = raw[:40].replace("\n", " ")
                print(
                    f"CHAT_COLLECT_DEBUG: 新消息触发 group={source_group} id={getattr(msg, 'id', None)} "
                    f"len(raw)={len(raw)} preview='{preview}'"
                )

            row = await _message_to_row(
                client=client,
                msg=msg,
                profile_filter=profile_filter,
                sender_skip_cache=sender_skip_cache,
                min_chars=min_chars,
                max_chars=max_chars,
                debug=(debug_seen <= 20),
            )
            if row is None:
                if debug_seen <= 20:
                    print("CHAT_COLLECT_DEBUG: 消息被过滤（row=None）。")
                return

            if debug_seen <= 20:
                print(
                    "CHAT_COLLECT_DEBUG: 消息通过过滤，写库。source_message_id="
                    f"{row.get('source_message_id')} sender_id={row.get('sender_id')}"
                )

            # 每条立即入库；避免攒批导致消息少时长期不落库。
            await storage.upsert_collected_messages(
                account_session_name=account_session_name,
                source_group=source_group,
                rows=[row],
            )

        client.add_event_handler(handler, event_builder)
        event_handlers.append((handler, event_builder))

    logger.info(
        "CHAT_COLLECT: 已注册实时监听，目标群列表=%s，入库表=chat_collected_messages（SQLite）。",
        ",".join(targets),
    )
    try:
        await asyncio.sleep(float("inf"))
    except asyncio.CancelledError:
        raise
    finally:
        try:
            for handler, event_builder in event_handlers:
                try:
                    client.remove_event_handler(handler, event_builder)
                except Exception:
                    pass
        except Exception:
            pass
