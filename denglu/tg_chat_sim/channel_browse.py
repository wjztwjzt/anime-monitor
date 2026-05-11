"""
让所有已登录账号依次「浏览」指定频道：解析实体、拉取频道详情、按时间顺序遍历消息（触发服务端历史拉取）。

目标参数从 Redis 读取（与主程序共用前缀）:
  {prefix}:config:CHANNEL_BROWSE  JSON，字段见 run_channel_browse_once 内说明。

执行结束后写入:
  {prefix}:status:last_channel_browse  JSON 摘要（各账号成功与否、统计等）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from redis.asyncio import Redis
from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel

from tg_chat_sim.storage import RuntimeAccountRecord


CONFIG_KEY_SUFFIX = "config:CHANNEL_BROWSE"
STATUS_KEY_SUFFIX = "status:last_channel_browse"


def _config_key(prefix: str) -> str:
    return f"{prefix}:{CONFIG_KEY_SUFFIX}"


def _status_key(prefix: str) -> str:
    return f"{prefix}:{STATUS_KEY_SUFFIX}"


def _parse_browse_config(raw: str | None) -> dict[str, Any] | None:
    if not raw or not str(raw).strip():
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


async def run_channel_browse_once(
    *,
    redis: Redis,
    redis_key_prefix: str,
    runtime_accounts: list[RuntimeAccountRecord],
    client_by_session: dict[str, TelegramClient],
) -> None:
    cfg_key = _config_key(redis_key_prefix)
    status_key = _status_key(redis_key_prefix)
    raw_cfg = await redis.get(cfg_key)
    cfg = _parse_browse_config(raw_cfg)
    if not cfg:
        print(
            "CHANNEL_BROWSE: 跳过：Redis 中无有效 JSON 配置，请先写入 "
            f"{cfg_key}（peer 必填）。"
        )
        return

    peer = str(cfg.get("peer") or cfg.get("chat") or cfg.get("link") or "").strip()
    if not peer:
        print("CHANNEL_BROWSE: 跳过：配置缺少 peer（频道 @、链接或 chat id）。")
        return

    # 兼容单字段 message_id：视为从下界开始读（与 min_message_id 二选一）
    min_message_id = int(cfg.get("min_message_id") or 0)
    if min_message_id <= 0 and cfg.get("message_id") is not None:
        try:
            min_message_id = max(0, int(cfg.get("message_id")))
        except (TypeError, ValueError):
            min_message_id = 0

    max_messages = int(cfg.get("max_messages") or 5000)
    if max_messages <= 0:
        max_messages = 5000

    per_account_delay = float(cfg.get("per_account_delay_seconds") or 2.0)
    per_account_delay = max(0.0, per_account_delay)

    fetch_full = bool(cfg.get("fetch_full_channel", True))
    iter_batch = int(cfg.get("iter_batch_size") or 100)
    iter_batch = max(1, min(iter_batch, 500))

    rows: list[dict[str, Any]] = []
    started_at = time.monotonic()

    for acc in runtime_accounts:
        cli = client_by_session.get(acc.session_name)
        row: dict[str, Any] = {
            "session_name": acc.session_name,
            "ok": False,
            "peer": peer,
        }
        if cli is None:
            row["error"] = "no_client"
            rows.append(row)
            continue

        try:
            entity = await cli.get_entity(peer)
            row["entity_id"] = getattr(entity, "id", None)
            row["title"] = getattr(entity, "title", None) or getattr(
                entity, "username", None
            )

            if fetch_full and isinstance(entity, Channel):
                full = await cli(GetFullChannelRequest(entity))
                fc = full.full_chat if full else None
                row["participants_count"] = getattr(fc, "participants_count", None)
                row["about"] = (getattr(fc, "about", None) or "")[:500]

            n_seen = 0
            async for _msg in cli.iter_messages(
                entity,
                reverse=True,
                min_id=min_message_id,
                limit=max_messages,
                wait_time=0,
            ):
                n_seen += 1
                if n_seen % iter_batch == 0:
                    await asyncio.sleep(0)

            row["messages_iterated"] = n_seen
            row["ok"] = True
        except Exception as exc:
            row["error"] = str(exc)

        rows.append(row)
        if per_account_delay:
            await asyncio.sleep(per_account_delay)

    elapsed = time.monotonic() - started_at
    payload = {
        "peer": peer,
        "min_message_id": min_message_id,
        "max_messages": max_messages,
        "elapsed_seconds": round(elapsed, 3),
        "accounts": rows,
    }
    await redis.set(status_key, json.dumps(payload, ensure_ascii=False, indent=2))
    ok_n = sum(1 for r in rows if r.get("ok"))
    print(
        f"CHANNEL_BROWSE: 完成 peer={peer!r}，账号成功 {ok_n}/{len(rows)}，"
        f"详情见 {status_key}"
    )
