import asyncio
import json
from typing import Dict

from redis.asyncio import Redis
from telethon.errors import (
    AuthKeyDuplicatedError,
    SessionRevokedError,
    UnauthorizedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.tl.functions.updates import GetStateRequest

from tg_chat_sim.channel_browse import run_channel_browse_once
from tg_chat_sim.chat_collector import run_live_collect_new_messages
from tg_chat_sim.chat_script_sender import send_chat_script_once
from tg_chat_sim.config import Settings
try:
    from tg_chat_sim.gram_task_runner import run_gram_tasks_once
except ImportError:
    run_gram_tasks_once = None  # type: ignore[assignment]
from tg_chat_sim.startup_runner import run_startup_from_yaml
from tg_chat_sim.storage import StorageManager
from tg_chat_sim.telethon_manager import TelethonManager


async def _read_signal(redis: Redis, key: str) -> int:
    value = await redis.get(key)
    if not value:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _online_error_status(error_text: str) -> str:
    s = (error_text or "").strip().lower()
    if not s:
        return "offline"
    if "frozen" in s or "frozenmethodinvaliderror" in s:
        return "frozen"
    if (
        "deactivated" in s
        or "deleted" in s
        or "unregistered" in s
        or "session_revoked" in s
        or "auth key duplicated" in s
        or "authkeyduplicated" in s
    ):
        return "cancelled"
    return "offline"


def _startup(msg: str) -> None:
    print(f"[startup] {msg}", flush=True)


async def main() -> None:
    _startup("加载 Settings …")
    settings = Settings()
    storage = StorageManager(settings)

    redis_client: Redis | None = None
    chat_task: asyncio.Task | None = None
    collect_task: asyncio.Task | None = None
    stop_event: asyncio.Event | None = None
    try:
        _startup(
            f"连接存储 backend={settings.storage_backend!r}（SQLite/Redis 可能各需最多约 "
            f"{int(settings.redis_socket_connect_timeout)}s 连接超时）…"
        )
        await storage.connect()
        _startup("存储已连接。")

        # Ensure the proxy/profile config rows exist (so the DB-driven runtime can load).
        _startup("同步账号配置到库 …")
        await storage.ensure_account_configs(settings.accounts, settings)
        runtime_accounts = await storage.load_runtime_accounts(settings.accounts, settings)
        _startup(f"运行中账号数: {len(runtime_accounts)}")
        tg_manager = TelethonManager(settings, storage, runtime_accounts)

        _startup("连接 Redis（信号与控制）…")
        redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_socket_connect_timeout,
            socket_timeout=settings.redis_socket_timeout,
        )
        await redis_client.ping()
        _startup("Redis 已连接。")

        prefix = settings.redis_key_prefix
        SIGNAL_CHAT_RECORD_ENABLED = f"{prefix}:signal:CHAT_RECORD_ENABLED"
        SIGNAL_CHAT_START = f"{prefix}:signal:CHAT_START"
        SIGNAL_CHAT_STOP = f"{prefix}:signal:CHAT_STOP"

        SIGNAL_PROFILE_BIO_BATCH = f"{prefix}:signal:PROFILE_BIO_BATCH"
        SIGNAL_PROFILE_AVATAR_SEQ = f"{prefix}:signal:PROFILE_AVATAR_SEQ"
        SIGNAL_PROFILE_USERNAME_SEQ = f"{prefix}:signal:PROFILE_USERNAME_SEQ"
        SIGNAL_AUTO_JOIN_GROUPS_SEQ = f"{prefix}:signal:AUTO_JOIN_GROUPS_SEQ"
        SIGNAL_CHECK_ONLINE_SEQ = f"{prefix}:signal:CHECK_ONLINE_SEQ"
        SIGNAL_GRAM_TASK_SEQ = f"{prefix}:signal:GRAM_TASK_SEQ"
        SIGNAL_CHAT_COLLECT_ENABLED = f"{prefix}:signal:CHAT_COLLECT_ENABLED"
        SIGNAL_CHAT_COLLECT_START = f"{prefix}:signal:CHAT_COLLECT_START"
        SIGNAL_CHANNEL_BROWSE_SEQ = f"{prefix}:signal:CHANNEL_BROWSE_SEQ"
        STATUS_ONLINE_JSON = f"{prefix}:status:last_online_check"

        signal_map: Dict[str, str] = {
            "chat_record_enabled": SIGNAL_CHAT_RECORD_ENABLED,
            "chat_start": SIGNAL_CHAT_START,
            "chat_stop": SIGNAL_CHAT_STOP,
            "profile_bio_batch": SIGNAL_PROFILE_BIO_BATCH,
            "profile_avatar_seq": SIGNAL_PROFILE_AVATAR_SEQ,
            "profile_username_seq": SIGNAL_PROFILE_USERNAME_SEQ,
            "auto_join_groups_seq": SIGNAL_AUTO_JOIN_GROUPS_SEQ,
            "check_online_seq": SIGNAL_CHECK_ONLINE_SEQ,
            "gram_task_seq": SIGNAL_GRAM_TASK_SEQ,
            "chat_collect_enabled": SIGNAL_CHAT_COLLECT_ENABLED,
            "chat_collect_start": SIGNAL_CHAT_COLLECT_START,
            "channel_browse_seq": SIGNAL_CHANNEL_BROWSE_SEQ,
        }

        last_seen: Dict[str, int] = {k: 0 for k in signal_map.keys()}
        action_lock = asyncio.Lock()

        # session_name -> client mapping (used for speaker mapping and profile updates).
        _startup(
            "正在连接 Telegram（优先 sessions/*.session；已授权则不调接码 API；"
            "否则 HTTP 接码，网络慢时请往下看账号级日志）…"
        )
        async with tg_manager.running_clients() as clients:
            connected_accounts = tg_manager.connected_accounts
            _startup(
                f"Telegram 客户端已就绪：成功 {len(clients)}/{len(runtime_accounts)} 个账号；"
                "失败账号已跳过，不阻塞后续账号。"
            )
            client_by_session: Dict[str, object] = {
                acc.session_name: cli
                for acc, cli in zip(connected_accounts, clients)
            }

            await run_startup_from_yaml(
                tg_manager, runtime_accounts, client_by_session
            )

            def load_nama_profiles() -> dict[str, object]:
                return storage.load_nama_profiles()

            def build_speaker_index_to_client() -> Dict[int, object]:
                speaker_map: Dict[int, object] = {}
                for i, acc in enumerate(runtime_accounts, start=1):
                    speaker_map[i] = client_by_session.get(acc.session_name)
                return speaker_map

            poll_interval_seconds = 2
            try:
                while True:
                    keys = list(signal_map.values())
                    values = await redis_client.mget(keys)
                    current: Dict[str, int] = {}
                    for idx, k in enumerate(signal_map.keys()):
                        raw = values[idx]
                        try:
                            current[k] = int(raw) if raw is not None else 0
                        except (TypeError, ValueError):
                            current[k] = 0

                    # Reset completed chat task.
                    if chat_task is not None and chat_task.done():
                        chat_task = None
                        if stop_event is not None:
                            stop_event = None
                    if collect_task is not None and collect_task.done():
                        collect_task = None

                    chat_record_enabled = current["chat_record_enabled"] == 1
                    chat_start_enabled = current["chat_start"] == 1

                    # Level-trigger chat control:
                    # - CHAT_RECORD_ENABLED=1 and CHAT_START=1 => keep running script loop
                    # - Any of them becomes 0 => stop loop
                    if chat_record_enabled and chat_start_enabled and chat_task is None:
                        stop_event = asyncio.Event()
                        speaker_map = build_speaker_index_to_client()

                        async def chat_loop() -> None:
                            assert stop_event is not None
                            while not stop_event.is_set():
                                await send_chat_script_once(
                                    clients=clients,
                                    speaker_index_to_client=speaker_map,
                                    settings=settings,
                                    storage=storage,
                                    stop_event=stop_event,
                                )
                                gap = max(
                                    0,
                                    int(
                                        getattr(
                                            settings,
                                            "chat_between_rounds_seconds",
                                            20,
                                        )
                                    ),
                                )
                                if gap:
                                    await asyncio.sleep(gap)

                        chat_task = asyncio.create_task(chat_loop())
                        print("CHAT_START: 已启动持续聊天脚本任务。")

                    if (
                        not chat_record_enabled or not chat_start_enabled
                    ) and chat_task is not None:
                        if stop_event is not None:
                            stop_event.set()
                        chat_task.cancel()
                        chat_task = None
                        stop_event = None
                        print("CHAT_START/CHAT_RECORD_ENABLED 已关闭，停止聊天脚本任务。")

                    collect_enabled = (
                        settings.chat_collect_enabled or current["chat_collect_enabled"] == 1
                    )
                    collect_start = (
                        current["chat_collect_start"] == 1
                        or (
                            settings.chat_collect_enabled
                            and bool(
                                getattr(
                                    settings,
                                    "chat_collect_immediate_start",
                                    True,
                                )
                            )
                        )
                    )
                    if collect_enabled and collect_start and collect_task is None:
                        login_tag = (settings.login_account1 or "TG1").strip()
                        collect_session = settings.resolve_session_name_from_login_tag(
                            login_tag
                        )
                        collect_client = client_by_session.get(collect_session)
                        if collect_client is None and connected_accounts:
                            collect_client = clients[0]
                            collect_session = connected_accounts[0].session_name

                        async def collect_loop() -> None:
                            try:
                                await run_live_collect_new_messages(
                                    client=collect_client,
                                    account_session_name=collect_session,
                                    settings=settings,
                                    storage=storage,
                                )
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                print(f"CHAT_COLLECT_ERROR: {exc}")
                                raise

                        if collect_client is not None:
                            collect_task = asyncio.create_task(collect_loop())
                            print("CHAT_COLLECT_START: 已启动聊天记录采集任务。")

                    if (not collect_enabled or not collect_start) and collect_task is not None:
                        collect_task.cancel()
                        collect_task = None
                        print("CHAT_COLLECT_ENABLED/CHAT_COLLECT_START 已关闭，停止采集任务。")

                    if current["chat_stop"] == 1 and last_seen["chat_stop"] == 0:
                        if chat_task is not None and stop_event is not None:
                            stop_event.set()
                            chat_task.cancel()
                            chat_task = None
                            stop_event = None
                            await redis_client.set(SIGNAL_CHAT_STOP, 0)
                            await redis_client.set(SIGNAL_CHAT_START, 0)
                            print("CHAT_STOP: 请求停止聊天脚本发送任务。")
                        else:
                            await redis_client.set(SIGNAL_CHAT_STOP, 0)
                            print("CHAT_STOP: 当前没有正在运行的聊天任务。")

                    async with action_lock:
                        if (
                            current["profile_bio_batch"] == 1
                            and last_seen["profile_bio_batch"] == 0
                        ):
                            nama_profiles = load_nama_profiles()
                            for session_name, profile in nama_profiles.items():
                                if not getattr(profile, "apply_changes", False):
                                    continue
                                cli = client_by_session.get(session_name)
                                if cli is None:
                                    continue
                                try:
                                    await tg_manager.apply_bio_for_account(
                                        cli, getattr(profile, "nickname", "")
                                    )
                                except Exception as exc:
                                    print(
                                        f"PROFILE_BIO_BATCH: 跳过账号 {session_name}，error={exc}"
                                    )
                                await asyncio.sleep(1)
                            await redis_client.set(SIGNAL_PROFILE_BIO_BATCH, 0)
                            print("PROFILE_BIO_BATCH: 已应用。")

                    if (
                        current["profile_avatar_seq"] == 1
                        and last_seen["profile_avatar_seq"] == 0
                    ):
                        nama_profiles = load_nama_profiles()
                        for session_name, profile in nama_profiles.items():
                            if not getattr(profile, "apply_changes", False):
                                continue
                            cli = client_by_session.get(session_name)
                            if cli is None:
                                continue
                            try:
                                await tg_manager.apply_avatar_for_account(
                                    cli, getattr(profile, "avatar_local_path", "")
                                )
                            except Exception as exc:
                                print(
                                    f"PROFILE_AVATAR_SEQ: 跳过账号 {session_name}，error={exc}"
                                )
                            await asyncio.sleep(2)
                        await redis_client.set(SIGNAL_PROFILE_AVATAR_SEQ, 0)
                        print("PROFILE_AVATAR_SEQ: 已应用。")

                    if (
                        current["profile_username_seq"] == 1
                        and last_seen["profile_username_seq"] == 0
                    ):
                        nama_profiles = load_nama_profiles()
                        for session_name, profile in nama_profiles.items():
                            if not getattr(profile, "apply_changes", False):
                                continue
                            cli = client_by_session.get(session_name)
                            if cli is None:
                                continue
                            try:
                                await tg_manager.apply_username_for_account(
                                    cli, getattr(profile, "username", "")
                                )
                            except Exception as exc:
                                print(
                                    f"PROFILE_USERNAME_SEQ: 跳过账号 {session_name}，error={exc}"
                                )
                            await asyncio.sleep(1)
                        await redis_client.set(SIGNAL_PROFILE_USERNAME_SEQ, 0)
                        print("PROFILE_USERNAME_SEQ: 已应用。")

                    if (
                        current["gram_task_seq"] == 1
                        and last_seen["gram_task_seq"] == 0
                    ):
                        if run_gram_tasks_once is not None:
                            await run_gram_tasks_once(
                                client_by_session=client_by_session,
                                tg_manager=tg_manager,
                            )
                        await redis_client.set(SIGNAL_GRAM_TASK_SEQ, 0)
                        print("GRAM_TASK_SEQ: 已按 gram.json 执行一次业务。")

                    if (
                        current["channel_browse_seq"] == 1
                        and last_seen["channel_browse_seq"] == 0
                    ):
                        await run_channel_browse_once(
                            redis=redis_client,
                            redis_key_prefix=prefix,
                            runtime_accounts=runtime_accounts,
                            client_by_session=client_by_session,
                        )
                        await redis_client.set(SIGNAL_CHANNEL_BROWSE_SEQ, 0)
                        print("CHANNEL_BROWSE_SEQ: 已执行一次频道浏览任务并复位信号。")

                    if (
                        current["auto_join_groups_seq"] == 1
                        and last_seen["auto_join_groups_seq"] == 0
                    ):
                        nama_profiles = load_nama_profiles()
                        for session_name, cli in client_by_session.items():
                            profile = nama_profiles.get(session_name)
                            groups = (
                                getattr(profile, "join_groups", [])
                                if profile and getattr(profile, "apply_changes", False)
                                else []
                            )
                            if not groups:
                                continue
                            try:
                                await tg_manager.join_groups_for_client(cli, groups)
                            except Exception as exc:
                                print(
                                    f"AUTO_JOIN_GROUPS_SEQ: 跳过账号 {session_name}，error={exc}"
                                )
                            await asyncio.sleep(2)
                        await redis_client.set(SIGNAL_AUTO_JOIN_GROUPS_SEQ, 0)
                        print("AUTO_JOIN_GROUPS_SEQ: 已加入指定群组。")

                    if (
                        current["check_online_seq"] == 1
                        and last_seen["check_online_seq"] == 0
                    ):
                        rows: list[dict] = []
                        for acc in runtime_accounts:
                            cli = client_by_session.get(acc.session_name)
                            if cli is None:
                                rows.append(
                                    {
                                        "session_name": acc.session_name,
                                        "ok": False,
                                        "error": "no_client",
                                    }
                                )
                                continue
                            try:
                                authorized = await cli.is_user_authorized()
                                if not authorized:
                                    rows.append(
                                        {
                                            "session_name": acc.session_name,
                                            "ok": False,
                                            "authorized": False,
                                            "status": "cancelled",
                                            "error": "not_authorized",
                                        }
                                    )
                                    continue

                                me = await cli.get_me()
                                if me is None:
                                    rows.append(
                                        {
                                            "session_name": acc.session_name,
                                            "ok": False,
                                            "authorized": True,
                                            "status": "cancelled",
                                            "error": "get_me_empty",
                                        }
                                    )
                                    continue

                                if getattr(me, "deleted", False):
                                    rows.append(
                                        {
                                            "session_name": acc.session_name,
                                            "ok": False,
                                            "authorized": True,
                                            "status": "cancelled",
                                            "user_id": me.id,
                                            "username": me.username,
                                            "first_name": getattr(me, "first_name", None),
                                            "last_name": getattr(me, "last_name", None),
                                            "error": "account_deleted",
                                        }
                                    )
                                    continue

                                # 额外做一次轻量真实 RPC，避免仅凭 get_me 或本地会话缓存误判。
                                await cli(GetStateRequest())
                                rows.append(
                                    {
                                        "session_name": acc.session_name,
                                        "ok": True,
                                        "authorized": True,
                                        "status": "online",
                                        "user_id": me.id if me else None,
                                        "username": me.username if me else None,
                                        "first_name": getattr(me, "first_name", None)
                                        if me
                                        else None,
                                        "last_name": getattr(me, "last_name", None)
                                        if me
                                        else None,
                                    }
                                )
                            except (
                                UnauthorizedError,
                                SessionRevokedError,
                                AuthKeyDuplicatedError,
                                UserDeactivatedError,
                                UserDeactivatedBanError,
                            ) as exc:
                                status = _online_error_status(str(exc))
                                rows.append(
                                    {
                                        "session_name": acc.session_name,
                                        "ok": False,
                                        "authorized": False,
                                        "status": status,
                                        "error": str(exc),
                                    }
                                )
                            except Exception as exc:
                                status = _online_error_status(str(exc))
                                rows.append(
                                    {
                                        "session_name": acc.session_name,
                                        "ok": False,
                                        "status": status,
                                        "error": str(exc),
                                    }
                                )
                        payload = json.dumps(
                            {"accounts": rows}, ensure_ascii=False, indent=2
                        )
                        await redis_client.set(STATUS_ONLINE_JSON, payload)
                        await redis_client.set(SIGNAL_CHECK_ONLINE_SEQ, 0)
                        print(
                            f"CHECK_ONLINE_SEQ: 已写入 {STATUS_ONLINE_JSON}（{len(rows)} 个账号）"
                        )

                    last_seen = current
                    await asyncio.sleep(poll_interval_seconds)
            finally:
                # Best-effort cleanup
                if chat_task is not None and stop_event is not None:
                    stop_event.set()
                    chat_task.cancel()
                if collect_task is not None:
                    collect_task.cancel()
    finally:
        try:
            if redis_client is not None:
                await redis_client.close()
        except Exception:
            pass
        await storage.close()
