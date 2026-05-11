import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest

from tg_chat_sim.code_login import ensure_telethon_authorized
from tg_chat_sim.config import Settings
from tg_chat_sim.project_yaml import load_project_yaml, project_root
from tg_chat_sim.storage import RuntimeAccountRecord, StorageManager


class TelethonManager:
    def __init__(
        self,
        settings: Settings,
        storage: StorageManager,
        accounts: list[RuntimeAccountRecord],
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.accounts = accounts
        self.clients: list[TelegramClient] = []
        # 与 self.clients 顺序一致：仅登录成功的账号（失败则跳过，不阻塞后续账号）。
        self.connected_accounts: list[RuntimeAccountRecord] = []

    @staticmethod
    def _build_proxy(account: RuntimeAccountRecord) -> tuple | None:
        if not account.proxy_enabled:
            return None
        return (
            2,
            account.proxy_host,
            account.proxy_port,
            account.proxy_username or None,
            account.proxy_password or None,
        )

    async def start_all(self) -> None:
        if not self.accounts:
            raise ValueError(
                "未找到账号：请在项目根目录 配置.yaml 中设置「账号列表文件」并填写 账号列表.txt，"
                "或在 .env 中配置 TG*_SESSION_NAME / TG*_PHONE。"
            )
        root = project_root()
        py = load_project_yaml()
        sessions_dir = (root / str(py.login_sessions_dir).strip()).resolve()
        sessions_dir.mkdir(parents=True, exist_ok=True)

        self.clients.clear()
        self.connected_accounts.clear()

        for account in self.accounts:
            session_path = str(sessions_dir / account.session_name)
            print(
                f"[telegram] [{account.session_name}] 正在连接 Telegram …",
                flush=True,
            )
            client = TelegramClient(
                session_path,
                self.settings.telegram_api_id,
                self.settings.telegram_api_hash,
                proxy=self._build_proxy(account),
                connection_retries=5,
                retry_delay=2,
                timeout=60,
            )
            try:
                await client.connect()
                await self._ensure_authorized(client, account)
                await self._sync_profile_snapshot(client, account)
            except Exception as exc:
                print(
                    f"[telegram] [{account.session_name}] 登录失败，跳过该账号: {exc}",
                    flush=True,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                continue

            self.clients.append(client)
            self.connected_accounts.append(account)
            print(
                f"[telegram] [{account.session_name}] 已就绪。",
                flush=True,
            )

        if not self.clients:
            raise RuntimeError(
                "全部账号 Telegram 登录失败，无可用客户端；请检查 sessions、接码 API 与账号状态。"
            )

    async def stop_all(self) -> None:
        for client in self.clients:
            await client.disconnect()
        self.clients.clear()
        self.connected_accounts.clear()

    async def _ensure_authorized(
        self, client: TelegramClient, account: RuntimeAccountRecord
    ) -> None:
        # Telethon 已加载同名 session 文件：有效会话则直接返回，不经接码。
        if await client.is_user_authorized():
            return

        if (account.code_api_url or "").strip():
            ok, err = await ensure_telethon_authorized(client, account)
            if not ok:
                raise RuntimeError(
                    f"[{account.session_name}] 自动登录失败: {err or 'unknown'}"
                )
            return

        await client.send_code_request(account.phone)
        code = input(f"[{account.session_name}] 请输入 Telegram 验证码: ").strip()
        try:
            await client.sign_in(account.phone, code)
        except SessionPasswordNeededError:
            password = input(f"[{account.session_name}] 请输入二步验证密码: ").strip()
            await client.sign_in(password=password)

    async def _sync_profile_snapshot(
        self, client: TelegramClient, account: RuntimeAccountRecord
    ) -> None:
        me = await client.get_me()
        await self.storage.sync_profile_snapshot(
            session_name=account.session_name,
            user_id=me.id if me else None,
            username=me.username if me else "",
        )

    async def apply_bio_for_account(
        self, client: TelegramClient, nickname: str, about: str = ""
    ) -> None:
        nick = nickname.strip()
        bio = about.strip()
        if len(bio) > 70:
            bio = bio[:70]
        if not nick and not bio:
            return
        if not nick:
            me = await client.get_me()
            nick = (me.first_name or "").strip() if me else ""
        if not nick and not bio:
            return
        if bio:
            await client(
                UpdateProfileRequest(first_name=nick, last_name="", about=bio)
            )
        else:
            await client(UpdateProfileRequest(first_name=nick, last_name=""))

    async def apply_avatar_for_account(
        self, client: TelegramClient, avatar_local_path: str
    ) -> None:
        if not avatar_local_path:
            return
        avatar_path = Path(avatar_local_path).expanduser()
        if not avatar_path.exists() or not avatar_path.is_file():
            return
        uploaded = await client.upload_file(str(avatar_path))
        await client(UploadProfilePhotoRequest(file=uploaded))

    async def apply_username_for_account(
        self, client: TelegramClient, username_value: str
    ) -> None:
        if not username_value:
            return
        username = username_value.strip().lstrip("@")
        if not username:
            return
        await client(UpdateUsernameRequest(username=username))

    async def join_groups_for_client(
        self,
        client: TelegramClient,
        groups: list[str],
        per_group_delay_seconds: float = 1.5,
    ) -> None:
        for group in groups:
            group_value = str(group).strip()
            if not group_value:
                continue

            # Retry once for transient failures (e.g. temporary rate limiting).
            for attempt in (1, 2):
                try:
                    if "joinchat/" in group_value or "/+" in group_value:
                        invite_hash = (
                            group_value.rstrip("/").split("/")[-1].replace("+", "")
                        )
                        await client(ImportChatInviteRequest(hash=invite_hash))
                    else:
                        await client(JoinChannelRequest(channel=group_value))
                    break
                except Exception as exc:
                    if attempt == 2:
                        print(f"[JOIN_GROUPS] 加群失败: {group_value}, error={exc}")
                    else:
                        await asyncio.sleep(max(1.0, per_group_delay_seconds))
            await asyncio.sleep(max(0.0, per_group_delay_seconds))

    @asynccontextmanager
    async def running_clients(self) -> AsyncIterator[list[TelegramClient]]:
        await self.start_all()
        try:
            yield self.clients
        finally:
            await self.stop_all()
