import asyncio
import random
import re
from datetime import timezone
from typing import Any

from telethon import TelegramClient
from telethon.tl.custom.message import Message

from tg_chat_sim.config import Settings
from tg_chat_sim.storage import MessageRecord, StorageManager


def _extract_sender_name(message: Message) -> str | None:
    sender = message.sender
    if not sender:
        return None
    if hasattr(sender, "username") and sender.username:
        return sender.username
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    full = f"{first} {last}".strip()
    return full or None


def _is_bot_sender(message: Message) -> bool:
    sender = message.sender
    if not sender:
        return False
    return bool(getattr(sender, "bot", False))


def _looks_like_ad(text: str, ad_keywords: list[str]) -> bool:
    lower_text = text.lower()
    if re.search(r"(https?://|t\.me/|@\w{5,})", lower_text):
        return True
    return any(keyword in lower_text for keyword in ad_keywords)


EMOJIS = ["🙂", "😂", "😅", "🤔", "👍", "🔥", "💬", "😄"]


def _rewrite_text(text: str) -> str:
    # Lightweight random rewrite without external NLP dependencies.
    suffixes = ["我觉得", "看起来", "感觉", "说实话", "按这个节奏"]
    closings = ["可以再看看", "先这样", "你们怎么看", "我同意", "继续跟进"]
    text = text.strip()
    if len(text) < 8:
        return text
    mode = random.choice([1, 2, 3])
    if mode == 1:
        return f"{random.choice(suffixes)}，{text}"
    if mode == 2:
        return f"{text}，{random.choice(closings)}。"
    return text.replace("。", "，").replace("！", "!")


def _apply_persona(text: str, persona: str) -> str:
    persona = persona.strip()
    if "理性" in persona or "结论" in persona:
        return f"结论：{text}"
    if "热情" in persona or "感叹" in persona:
        return f"{text}！"
    if "解释" in persona or "补充" in persona:
        return f"补充一下，{text}"
    if "幽默" in persona or "调侃" in persona:
        return f"{text} 哈哈"
    return text


def _maybe_add_mention(text: str, candidates: list[str], probability: float) -> str:
    if not candidates or random.random() > probability:
        return text
    username = random.choice(candidates)
    return f"@{username} {text}"


def _maybe_add_emoji(text: str, probability: float) -> str:
    if random.random() > probability:
        return text
    return f"{text} {random.choice(EMOJIS)}"


def _humanize_text(
    original: str,
    account_index: int,
    settings: Settings,
) -> str:
    text = original.strip()
    if not settings.enable_human_style:
        return text

    if random.random() <= settings.rewrite_probability:
        text = _rewrite_text(text)

    persona = settings.personas[account_index % len(settings.personas)]
    text = _apply_persona(text, persona)
    text = _maybe_add_mention(text, settings.mention_users, settings.mention_probability)
    text = _maybe_add_emoji(text, settings.emoji_probability)
    return text


class DeepSeekHumanizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Any | None = None
        if not settings.deepseek_api_key:
            return
        try:
            from openai import OpenAI  # type: ignore
        except ImportError:
            return
        self.client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def rewrite(self, text: str, persona: str) -> str:
        if not self.client:
            return text
        return await asyncio.to_thread(self._rewrite_sync, text, persona)

    def _rewrite_sync(self, text: str, persona: str) -> str:
        assert self.client is not None
        response = self.client.chat.completions.create(
            model=self.settings.deepseek_model,
            messages=[
                {"role": "system", "content": self.settings.deepseek_system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"请把下面这段消息改写得更像真人聊天，保留原意，简短自然。"
                        f"\n人设：{persona}\n原文：{text}"
                    ),
                },
            ],
            stream=False,
        )
        content = (response.choices[0].message.content or "").strip()
        return content or text


async def copy_messages_from_source(
    client: TelegramClient, settings: Settings, storage: StorageManager
) -> int:
    entity = await client.get_entity(settings.source_group)
    records: list[MessageRecord] = []

    async for message in client.iter_messages(entity, limit=settings.cp_limit, reverse=True):
        if not message.message or not message.message.strip():
            continue
        text = message.message.strip()
        if _is_bot_sender(message):
            continue
        if len(text) > settings.filter_max_chars:
            continue
        if _looks_like_ad(text, settings.ad_keyword_list):
            continue
        msg_dt = message.date
        if msg_dt.tzinfo:
            msg_dt = msg_dt.astimezone(timezone.utc).replace(tzinfo=None)
        records.append(
            MessageRecord(
                source_group=str(settings.source_group),
                source_message_id=message.id,
                sender_id=message.sender_id,
                sender_name=_extract_sender_name(message),
                text_content=text,
                message_date=msg_dt,
            )
        )

    await storage.save_messages(records)
    return len(records)


async def simulate_chat_to_target(
    clients: list[TelegramClient], settings: Settings, storage: StorageManager
) -> None:
    messages = await storage.load_messages(settings.simulate_send_limit)
    if not messages:
        raise RuntimeError("没有可发送的消息。请先执行拷贝流程。")

    target_entities: dict[int, object] = {}
    deepseek = DeepSeekHumanizer(settings)

    async def run_once() -> None:
        for index, message in enumerate(messages):
            client = clients[index % len(clients)]
            client_key = id(client)
            if client_key not in target_entities:
                # Entity objects are client-scoped in Telethon. Resolve per account.
                target_entities[client_key] = await client.get_entity(settings.target_group)
            persona = settings.personas[index % len(settings.personas)]
            final_text = _humanize_text(message.text_content, index, settings)
            if deepseek.enabled:
                try:
                    final_text = await deepseek.rewrite(final_text, persona)
                except Exception:
                    pass
            await client.send_message(target_entities[client_key], final_text)
            sleep_seconds = random.randint(
                settings.simulate_min_interval_seconds,
                settings.simulate_max_interval_seconds,
            )
            await asyncio.sleep(sleep_seconds)

    if settings.simulate_loop:
        while True:
            await run_once()
    else:
        await run_once()
