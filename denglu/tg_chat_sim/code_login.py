"""Telegram login: session first, then send_code + aiohttp poll + 2FA (Telethon)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Optional

import aiohttp
from aiohttp import BasicAuth
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from tg_chat_sim.account_api_keywords import (
    body_indicates_dead_or_frozen,
)
from tg_chat_sim.storage import RuntimeAccountRecord

try:
    from aiohttp_socks import ProxyConnector  # type: ignore
except Exception:
    ProxyConnector = None  # type: ignore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _telegram_error_suggests_banned_or_frozen(exc: BaseException) -> bool:
    """Telegram RPC 文案里常见封禁/冻结表述，用于尽快结束接码重试。"""
    s = str(exc).lower()
    tokens = (
        "banned",
        "deactivated",
        "frozen",
        "user_deactivated",
        "phone_number_banned",
        "封禁",
        "冻结",
    )
    return any(t in s for t in tokens)


class AccountFrozenFromApiResponse(Exception):
    """接码 API 返回体命中死号/冻结关键词（见 account_api_keywords）。"""

    def __init__(self, matched_keywords: list[str]) -> None:
        self.matched_keywords = list(matched_keywords)
        super().__init__(
            ",".join(self.matched_keywords) if self.matched_keywords else "frozen"
        )


def _build_http_proxy_url(account: RuntimeAccountRecord) -> Optional[str]:
    if not account.proxy_enabled:
        return None
    host = (account.proxy_host or "").strip()
    port = account.proxy_port
    if not host or not port:
        return None
    user = (account.proxy_username or "").strip()
    pw = (account.proxy_password or "").strip()
    scheme = "socks5"
    if user and pw:
        return f"{scheme}://{user}:{pw}@{host}:{int(port)}"
    if user:
        return f"{scheme}://{user}@{host}:{int(port)}"
    return f"{scheme}://{host}:{int(port)}"


def _proxy_scheme_for_connector(account: RuntimeAccountRecord) -> bool:
    return bool(account.proxy_enabled and (account.proxy_host or "").strip())


async def fetch_code_from_api(
    *,
    code_api_url: str,
    account: RuntimeAccountRecord,
    session_label: str,
    max_retries: int = 12,
    interval: float = 5.0,
) -> tuple[Optional[str], Optional[str]]:
    if not code_api_url:
        logger.warning("[%s] No code_api_url", session_label)
        return None, None

    proxy_url = _build_http_proxy_url(account)
    proxy_auth = None
    if proxy_url and account.proxy_username:
        proxy_auth = BasicAuth(
            account.proxy_username, account.proxy_password or ""
        )

    use_socks_connector = _proxy_scheme_for_connector(account)
    if use_socks_connector and ProxyConnector is None:
        logger.warning(
            "[%s] aiohttp-socks missing, code API will try without SOCKS proxy",
            session_label,
        )
        use_socks_connector = False
        proxy_url = None
        proxy_auth = None

    for attempt in range(1, max_retries + 1):
        # 首次立即拉码，命中冻结关键词时可立刻退出，避免先空等 interval。
        if attempt > 1:
            await asyncio.sleep(interval)
        logger.info(
            "[%s] Fetching code from API (attempt %s/%s)...",
            session_label,
            attempt,
            max_retries,
        )
        try:
            connector = None
            if use_socks_connector and ProxyConnector is not None:
                connector = ProxyConnector.from_url(_build_http_proxy_url(account))
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    code_api_url,
                    timeout=aiohttp.ClientTimeout(total=30),
                    proxy=None if connector else proxy_url,
                    proxy_auth=None if connector else proxy_auth,
                ) as resp:
                    status = resp.status
                    body = await resp.text()
                    frozen_hits = body_indicates_dead_or_frozen(body)
                    if frozen_hits:
                        logger.warning(
                            "[%s] 接码 API 响应命中冻结/死号关键词: %s",
                            session_label,
                            frozen_hits,
                        )
                        raise AccountFrozenFromApiResponse(frozen_hits)
                    if status == 404:
                        continue
                    if status != 200:
                        logger.warning("[%s] API status %s", session_label, status)
                        continue
                    code, pw = _parse_code_response(body, session_label)
                    if code:
                        return code, pw
        except AccountFrozenFromApiResponse:
            raise
        except asyncio.TimeoutError:
            logger.warning("[%s] API timeout attempt %s", session_label, attempt)
        except Exception as exc:
            logger.warning("[%s] API error attempt %s: %s", session_label, attempt, exc)

    return None, None


def _parse_code_response(body: str, session_label: str) -> tuple[Optional[str], Optional[str]]:
    body = body.strip()
    if "错误" in body or "频繁" in body or "发生错误" in body:
        return None, None

    code = _extract_html_field(body, ["设备验证码", "验证码", "code"])
    password = _extract_html_field(
        body, ["2fa/密码", "2fa密码", "密码", "password", "2fa"]
    )
    if code:
        return code, password

    try:
        data = json.loads(body)
        if isinstance(data, dict):
            code = _extract_field(
                data,
                ["code", "verify_code", "verification_code", "sms_code", "phone_code"],
            )
            password = _extract_field(
                data,
                ["password", "two_fa_password", "2fa_password", "twofa", "2fa"],
            )
            if code is None and "data" in data and isinstance(data["data"], dict):
                inner = data["data"]
                code = _extract_field(
                    inner,
                    ["code", "verify_code", "verification_code", "sms_code", "phone_code"],
                )
                if password is None:
                    password = _extract_field(
                        inner,
                        ["password", "two_fa_password", "2fa_password", "twofa", "2fa"],
                    )
            if code:
                return str(code), str(password) if password else None
        elif isinstance(data, (str, int)):
            return str(data), None
    except (json.JSONDecodeError, ValueError):
        pass

    if body and len(body) < 100:
        for sep in ["----", "---", "--", "\n", "\t", "|", ","]:
            if sep in body:
                parts = body.split(sep, 1)
                code_part = parts[0].strip()
                pw_part = parts[1].strip() if len(parts) > 1 else None
                if code_part and len(code_part) <= 10:
                    return code_part, pw_part if pw_part else None
        if len(body) <= 10:
            return body, None

    logger.warning("[%s] Unparseable API body: %s", session_label, body[:200])
    return None, None


def _extract_html_field(html: str, labels: list[str]) -> Optional[str]:
    id_map = {
        "code": ["设备验证码", "验证码", "code"],
        "pass2fa": ["2fa/密码", "2fa密码", "密码", "password", "2fa"],
    }
    for input_id, matching_labels in id_map.items():
        if any(lbl in labels for lbl in matching_labels):
            pattern = (
                rf'<input[^>]*id=["\']?{input_id}["\']?[^>]*value=["\']([^"\']*)["\']'
            )
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                val = match.group(1).strip()
                if val:
                    return val
    for label in labels:
        escaped = re.escape(label)
        pattern = rf"{escaped}[^<]*(?:<[^>]*>)*\s*<input[^>]*value=[\"']([^\"']*)[\"']"
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            val = match.group(1).strip()
            if val:
                return val
    text = re.sub(r"<[^>]+>", "\n", html)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    for i, line in enumerate(lines):
        for label in labels:
            if label in line:
                colon_pos = line.find(":")
                if colon_pos >= 0:
                    val = line[colon_pos + 1 :].strip()
                    if val and not any(
                        lbl in val
                        for lbl in ["时间", "信息", "错误", "验证", "登录"]
                    ):
                        return val
                if i + 1 < len(lines):
                    next_val = lines[i + 1].strip()
                    if (
                        next_val
                        and len(next_val) < 50
                        and ":" not in next_val
                        and not any(
                            lbl in next_val
                            for lbl in ["时间", "信息", "错误", "验证", "登录"]
                        )
                    ):
                        return next_val
    return None


def _extract_field(data: dict, field_names: list[str]) -> Optional[str]:
    for name in field_names:
        val = data.get(name)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


async def ensure_telethon_authorized(
    client: TelegramClient,
    account: RuntimeAccountRecord,
    *,
    login_rounds: int = 3,
    code_fetch_retries: int = 12,
    code_fetch_interval: float = 5.0,
    pause_between_rounds: float = 8.0,
) -> tuple[bool, Optional[str]]:
    """
    Prefer existing session; otherwise send_code + aiohttp + optional 2FA.
    Retries full code round on invalid/expired code.
    """
    label = account.session_name
    try:
        if not client.is_connected():
            await client.connect()
    except Exception as exc:
        return False, f"Cannot connect: {exc}"

    if await client.is_user_authorized():
        logger.info("[%s] Session already authorized", label)
        return True, None

    if not account.phone:
        return False, "Phone number missing"

    if not account.code_api_url:
        return False, "code_api_url missing (账号列表.txt 第二列)"

    for round_idx in range(1, login_rounds + 1):
        try:
            await client.send_code_request(account.phone)
        except PhoneNumberBannedError:
            return False, "手机号已被 Telegram 封禁 (PhoneNumberBannedError)"
        except PhoneNumberInvalidError:
            return False, "Phone number invalid"
        except Exception as exc:
            if _telegram_error_suggests_banned_or_frozen(exc):
                return False, f"send_code 被拒绝（疑似冻结/封禁）: {exc}"
            return False, f"send_code_request failed: {exc}"

        try:
            code, api_pw = await fetch_code_from_api(
                code_api_url=account.code_api_url,
                account=account,
                session_label=label,
                max_retries=code_fetch_retries,
                interval=code_fetch_interval,
            )
        except AccountFrozenFromApiResponse as exc:
            return False, (
                "账号疑似冻结或死号（接码 API 响应包含: "
                + ",".join(exc.matched_keywords)
                + "）"
            )
        if not code:
            if round_idx < login_rounds:
                logger.warning("[%s] No code, retry round %s", label, round_idx + 1)
                await asyncio.sleep(pause_between_rounds)
                continue
            return False, "Failed to get verification code from API"

        try:
            await client.sign_in(account.phone, code)
        except SessionPasswordNeededError:
            password = (api_pw or account.two_fa_password or "").strip()
            if not password:
                return False, "2FA required but no password (API or 第三列)"
            try:
                await client.sign_in(password=password)
            except Exception as exc:
                return False, f"2FA sign_in failed: {exc}"
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
            logger.warning("[%s] Code invalid/expired: %s", label, exc)
            if round_idx < login_rounds:
                await asyncio.sleep(pause_between_rounds)
                continue
            return False, f"Verification code invalid or expired: {exc}"
        except Exception as exc:
            if _telegram_error_suggests_banned_or_frozen(exc):
                return False, f"sign_in 被拒绝（疑似冻结/封禁）: {exc}"
            return False, f"sign_in error: {exc}"

        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info("[%s] API login ok user_id=%s", label, me.id if me else "?")
            return True, None

        return False, "Login finished but not authorized"

    return False, "Login retries exhausted"
