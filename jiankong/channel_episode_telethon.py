"""
用 Telethon 扫目标频道历史消息，结合 #标签 与「第N集」文案推断频道内已发布的最大集数。

供 config_monitor 在「网站集数上涨」时二次确认：若频道已不落后于站点集数则跳过流水线。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)

_EP_PATTERNS = [
    re.compile(r"第\s*(\d+)\s*集"),
    re.compile(r"第\s*(\d+)\s*话"),
    re.compile(r"(?i)episode\s*#?\s*(\d+)"),
]


def _max_episode_in_text(text: str, *, hashtag_needle: str) -> int:
    if not text:
        return 0
    if hashtag_needle and hashtag_needle not in text:
        return 0
    m = 0
    for pat in _EP_PATTERNS:
        for x in pat.finditer(text):
            try:
                m = max(m, int(x.group(1)))
            except (TypeError, ValueError):
                pass
    return m


async def scan_channel_max_episode(
    *,
    telegram_chat_id: str,
    hashtag: str,
    message_limit: int = 400,
) -> int:
    """
    在频道/群内拉取最近 message_limit 条消息，筛出含 hashtag 的文本，取「第N集」等模式的最大 N。
    hashtag 例如 #仙逆；为空则不过滤标签，仅按集数文案取 max（慎用）。
    """
    from telegram_manager import TelegramManager

    peer = telegram_chat_id.strip()
    if not peer:
        return 0

    tag = (hashtag or "").strip()
    mgr = TelegramManager()
    try:
        await mgr.login()
        client = mgr.client
        ent = await client.get_entity(int(peer) if peer.lstrip("-").isdigit() else peer)
        max_ep = 0
        n = 0
        async for msg in client.iter_messages(ent, limit=max(1, int(message_limit))):
            n += 1
            text = (getattr(msg, "message", None) or getattr(msg, "text", None) or "") or ""
            if not text.strip():
                continue
            v = _max_episode_in_text(text, hashtag_needle=tag)
            if v > 0:
                max_ep = max(max_ep, v)
        logger.info(
            "Telethon 扫频道 peer=%s tag=%s 条数=%s → 推断最大集=%s",
            peer,
            tag or "(无)",
            n,
            max_ep,
        )
        return max_ep
    finally:
        await mgr.disconnect()


def scan_channel_max_episode_blocking(
    *,
    telegram_chat_id: str,
    hashtag: str,
    message_limit: int = 400,
) -> int:
    """同步封装（内部 asyncio.run）。"""
    return asyncio.run(
        scan_channel_max_episode(
            telegram_chat_id=telegram_chat_id,
            hashtag=hashtag,
            message_limit=message_limit,
        )
    )
