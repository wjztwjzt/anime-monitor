"""
统一 Telegram 管理器：登录、上传、资料修改、加群/频道。

登录流程（参考 denglu/）：
  1. 优先使用 session 文件（如果已授权则直接复用）
  2. 否则通过 API txt 文件登录（格式: 手机号|接码API[|二步验证密码]）
  3. 也支持直接从 config.yaml 读取 api_id/api_hash

用法：
  # 作为库使用
  from telegram_manager import TelegramManager
  mgr = TelegramManager()
  await mgr.login()
  await mgr.upload_video("video.mp4", "标题", target="-100xxx")
  await mgr.update_profile(name="新名字", bio="新简介")
  await mgr.join_groups(["@channel1", "https://t.me/+xxxxx"])

  # CLI: 资料管理
  python telegram_manager.py profile --name "新名字" --bio "新简介"
  python telegram_manager.py profile --avatar ./avatar.jpg
  python telegram_manager.py profile --username myusername
  python telegram_manager.py join --groups "@ch1,@ch2"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Optional

# 确保项目根在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config_loader import load_config, resolve_rel  # noqa: E402
from app.paths import project_root  # noqa: E402
from app.proxy_util import build_socks5_url  # noqa: E402

logger = logging.getLogger(__name__)


def _ensure_logging() -> None:
    if not logger.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)


class TelegramManager:
    """统一的 Telegram 客户端管理器。"""

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        session_path: str | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
        phone: str | None = None,
        proxy_url: str | None = None,
    ) -> None:
        _ensure_logging()

        self._client: Any = None  # TelegramClient
        self._me: Any = None

        # 加载 config.yaml
        cfg = load_config(config_path)
        tg_cfg = cfg.get("telegram") or {}
        if not isinstance(tg_cfg, dict):
            tg_cfg = {}

        # API 凭据
        self.api_id = api_id or int(str(tg_cfg.get("api_id") or "0").strip())
        self.api_hash = api_hash or str(tg_cfg.get("api_hash") or "").strip()

        # Session 文件
        root = project_root()
        if session_path:
            sp = Path(session_path)
            if not sp.is_absolute():
                sp = (root / sp).resolve()
        else:
            sess_rel = str(tg_cfg.get("session_path") or "Telethon-FastUpload/session.session").strip()
            sp = resolve_rel(root, sess_rel)
        sp.parent.mkdir(parents=True, exist_ok=True)
        self.session_path = sp

        # 手机号
        self.phone = phone or str(tg_cfg.get("phone") or "").strip()

        # 代理
        if proxy_url:
            self.proxy = self._parse_proxy_url(proxy_url)
        else:
            proxy_cfg = cfg.get("proxy") or {}
            upload_proxy = proxy_cfg.get("upload") if isinstance(proxy_cfg, dict) else {}
            socks_url = build_socks5_url(upload_proxy if isinstance(upload_proxy, dict) else {})
            self.proxy = self._parse_proxy_url(socks_url) if socks_url else None

        # API txt 文件（账号列表）
        self._account_list: list[dict[str, str]] = []
        self._api_txt_path: Path | None = None

        # 配置缓存
        self._cfg = cfg

    @staticmethod
    def _parse_proxy_url(url: str) -> tuple | None:
        """解析 socks5://[user:pass@]host:port 为 Telethon proxy tuple。"""
        if not url:
            return None
        try:
            from python_socks import ProxyType
        except ImportError:
            logger.warning("python-socks 未安装，代理可能不可用。pip install python-socks")
            return None

        from urllib.parse import urlparse, unquote

        parsed = urlparse(url)
        if parsed.scheme not in ("socks5", "socks5h"):
            logger.warning("仅支持 SOCKS5 代理，收到: %s", parsed.scheme)
            return None
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 1080
        username = unquote(parsed.username) if parsed.username else None
        password = unquote(parsed.password) if parsed.password else None
        return (ProxyType.SOCKS5, host, port, True, username, password)

    # ---- API txt 文件加载 ----

    def load_account_list(self, txt_path: str | Path) -> list[dict[str, str]]:
        """
        加载账号列表文件（参考 denglu/ 格式）。
        每行: 手机号|接码API地址[|二步验证密码]
        以 # 开头的行为注释。
        """
        path = Path(txt_path)
        if not path.is_absolute():
            path = (project_root() / path).resolve()
        self._api_txt_path = path

        if not path.is_file():
            logger.warning("账号列表文件不存在: %s", path)
            return []

        accounts: list[dict[str, str]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" not in line:
                continue
            parts = line.split("|")
            phone = parts[0].strip()
            code_url = parts[1].strip() if len(parts) > 1 else ""
            two_fa = parts[2].strip() if len(parts) > 2 else ""

            if phone and code_url:
                accounts.append({
                    "phone": phone,
                    "code_api_url": code_url,
                    "two_fa_password": two_fa,
                })

        self._account_list = accounts
        logger.info("从 %s 加载了 %s 个账号", path.name, len(accounts))
        return accounts

    # ---- 登录 ----

    async def login(
        self,
        *,
        force_relogin: bool = False,
        code_callback: callable | None = None,
    ) -> Any:
        """
        登录 Telegram。
        1. 优先使用 session 文件
        2. 如果未授权，尝试通过 API 接码登录
        3. 如果 API 不可用，交互式输入验证码

        返回 TelegramClient 实例。
        """
        try:
            from telethon import TelegramClient
            from telethon.errors import SessionPasswordNeededError
        except ImportError:
            raise SystemExit("请安装 telethon: pip install telethon")

        client = TelegramClient(
            str(self.session_path),
            self.api_id,
            self.api_hash,
            connection_retries=5,
            retry_delay=3,
            proxy=self.proxy,
        )

        await client.connect()

        if not force_relogin and await client.is_user_authorized():
            self._client = client
            self._me = await client.get_me()
            logger.info(
                "Session 有效，已登录: %s (id=%s)",
                getattr(self._me, "first_name", ""),
                self._me.id if self._me else "?",
            )
            return client

        # 需要登录
        phone = self.phone
        if not phone and self._account_list:
            phone = self._account_list[0]["phone"]
        if not phone:
            raise RuntimeError("未配置手机号：请在 config.yaml 或 API txt 中提供")

        # 尝试通过 API 接码登录
        account = self._account_list[0] if self._account_list else None
        if account and account.get("code_api_url"):
            logger.info("尝试通过接码 API 登录: %s", phone)
            ok = await self._login_via_api(client, account, code_callback)
            if ok:
                self._client = client
                self._me = await client.get_me()
                return client

        # 回退：交互式登录
        logger.info("交互式登录: %s", phone)
        await client.send_code_request(phone)

        if code_callback:
            code = await code_callback()
        else:
            code = input("请输入 Telegram 验证码: ").strip()

        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            password = account.get("two_fa_password") if account else ""
            if not password:
                password = input("请输入二步验证密码: ").strip()
            await client.sign_in(password=password)

        self._client = client
        self._me = await client.get_me()
        logger.info(
            "登录成功: %s (id=%s)",
            getattr(self._me, "first_name", ""),
            self._me.id if self._me else "?",
        )
        return client

    async def _login_via_api(
        self,
        client: Any,
        account: dict[str, str],
        code_callback: callable | None = None,
    ) -> bool:
        """通过接码 API 自动登录（参考 denglu/tg_chat_sim/code_login.py）。"""
        from telethon.errors import (
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )

        phone = account["phone"]
        code_api_url = account["code_api_url"]
        two_fa = account.get("two_fa_password", "")

        for attempt in range(1, 4):
            try:
                await client.send_code_request(phone)
            except Exception as e:
                logger.error("send_code_request 失败: %s", e)
                return False

            code, api_pw = await self._fetch_code_from_api(code_api_url, attempt)

            if not code:
                logger.warning("第 %s 次获取验证码失败，重试...", attempt)
                await asyncio.sleep(5)
                continue

            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                password = api_pw or two_fa
                if not password:
                    if code_callback:
                        password = await code_callback()
                    else:
                        password = input("请输入二步验证密码: ").strip()
                if not password:
                    return False
                try:
                    await client.sign_in(password=password)
                except Exception as e:
                    logger.error("2FA 登录失败: %s", e)
                    return False
            except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
                logger.warning("验证码无效/过期: %s", e)
                await asyncio.sleep(3)
                continue
            except Exception as e:
                logger.error("sign_in 失败: %s", e)
                return False

            if await client.is_user_authorized():
                logger.info("API 接码登录成功")
                return True

        return False

    async def _fetch_code_from_api(
        self, code_api_url: str, attempt: int
    ) -> tuple[str | None, str | None]:
        """从接码 API 获取验证码。"""
        import json
        import re

        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp 未安装，无法使用接码 API")
            return None, None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    code_api_url,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("接码 API 返回 %s", resp.status)
                        return None, None
                    body = await resp.text()
        except Exception as e:
            logger.warning("接码 API 请求失败: %s", e)
            return None, None

        body = body.strip()

        # 尝试 JSON 解析
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                code = (
                    data.get("code")
                    or data.get("verify_code")
                    or data.get("sms_code")
                    or data.get("phone_code")
                )
                if not code and "data" in data and isinstance(data["data"], dict):
                    code = (
                        data["data"].get("code")
                        or data["data"].get("verify_code")
                    )
                pw = (
                    data.get("password")
                    or data.get("two_fa_password")
                    or data.get("2fa")
                )
                if code:
                    return str(code), str(pw) if pw else None
        except (json.JSONDecodeError, ValueError):
            pass

        # 简单文本解析
        if len(body) < 100:
            for sep in ("----", "---", "--", "\n", "|", ","):
                if sep in body:
                    parts = body.split(sep, 1)
                    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else None)
            if len(body) <= 10:
                return body, None

        # HTML 解析
        for label in ("验证码", "code"):
            pattern = re.compile(
                rf'{label}[^<]*(?:<[^>]*>)*\s*<input[^>]*value=[\"\']([^\"\']*)[\"\']',
                re.IGNORECASE,
            )
            m = pattern.search(body)
            if m:
                return m.group(1).strip(), None

        logger.warning("无法从 API 响应中提取验证码: %s", body[:200])
        return None, None

    @property
    def client(self) -> Any:
        if not self._client:
            raise RuntimeError("未登录，请先调用 await login()")
        return self._client

    @property
    def me(self) -> Any:
        return self._me

    # ---- 上传视频 ----

    async def upload_video(
        self,
        file_path: str | Path,
        caption: str = "",
        target: str | None = None,
        *,
        thumb_path: str | None = None,
        progress_callback: callable | None = None,
    ) -> Any:
        """
        上传视频到 Telegram 频道/群组。

        target: 目标频道/群组（如 '-1003966238914'），留空则使用 config.yaml 中的 target。
        caption: 视频文案（支持多行）。
        thumb_path: 封面图路径，留空则使用 config.yaml 中的 paths.cover。
        """
        from telethon.tl import types as tl_types

        client = self.client
        cfg = self._cfg

        # 目标
        if not target:
            tg_cfg = cfg.get("telegram") or {}
            target = str(tg_cfg.get("target") or "").strip()
        if not target:
            raise ValueError("未指定上传目标（target 参数或 config.yaml telegram.target）")

        # 文件路径
        fp = Path(file_path)
        if not fp.is_absolute():
            fp = (project_root() / fp).resolve()
        if not fp.is_file():
            raise FileNotFoundError(f"视频文件不存在: {fp}")

        # 封面
        thumb = None
        if thumb_path:
            tp = Path(thumb_path)
            if not tp.is_absolute():
                tp = (project_root() / tp).resolve()
            if tp.is_file():
                thumb = str(tp)
        else:
            paths_cfg = cfg.get("paths") or {}
            cover_rel = str(paths_cfg.get("cover") or "Telethon-FastUpload/cover.jpeg").strip()
            cp = resolve_rel(project_root(), cover_rel)
            if cp.is_file():
                thumb = str(cp)

        target_entity = await client.get_entity(
            int(target) if target.lstrip("-").isdigit() else target
        )

        logger.info("上传: %s -> %s", fp.name, target)

        # 使用 Telethon 内置上传（视频可 inline 播放）
        kwargs: dict[str, Any] = dict(
            entity=target_entity,
            file=str(fp),
            caption=caption or None,
            supports_streaming=True,
            force_document=False,
            progress_callback=progress_callback,
        )
        if thumb:
            kwargs["thumb"] = thumb

        # 检测音轨
        nosound = self._check_nosound(fp)
        if nosound is not None:
            kwargs["nosound_video"] = nosound

        msg = await client.send_file(**kwargs)
        logger.info("上传完成: %s", fp.name)
        return msg

    @staticmethod
    def _check_nosound(file_path: Path) -> bool | None:
        """检测视频是否有音轨。"""
        import shutil
        import subprocess

        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None
        try:
            r = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-select_streams", "a",
                    "-show_entries", "stream=codec_type",
                    "-of", "csv=p=0",
                    str(file_path),
                ],
                capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        has_audio = bool([ln for ln in (r.stdout or "").splitlines() if ln.strip()])
        return False if has_audio else True

    # ---- 资料修改 ----

    async def update_profile(
        self,
        *,
        name: str | None = None,
        bio: str | None = None,
        avatar: str | None = None,
        username: str | None = None,
    ) -> dict[str, bool]:
        """
        修改账号资料。

        参数均可选，传 None 表示不修改该项。
        返回 {操作: 是否成功}。
        """
        from telethon.tl.functions.account import (
            UpdateProfileRequest,
            UpdateUsernameRequest,
        )
        from telethon.tl.functions.photos import UploadProfilePhotoRequest

        client = self.client
        result: dict[str, bool] = {}

        # 修改名字和简介
        if name is not None or bio is not None:
            me = self.me or await client.get_me()
            new_name = (name or me.first_name or "").strip()
            new_bio = (bio or "").strip()
            if len(new_bio) > 70:
                new_bio = new_bio[:70]

            await client(UpdateProfileRequest(
                first_name=new_name,
                last_name="",
                about=new_bio if new_bio else None,
            ))
            result["name"] = True
            if bio is not None:
                result["bio"] = True
            logger.info("资料已更新: name=%s, bio=%s", new_name, new_bio[:50] if new_bio else "")

        # 修改头像
        if avatar:
            avatar_path = Path(avatar)
            if not avatar_path.is_absolute():
                avatar_path = (project_root() / avatar_path).resolve()
            if not avatar_path.is_file():
                logger.error("头像文件不存在: %s", avatar_path)
                result["avatar"] = False
            else:
                uploaded = await client.upload_file(str(avatar_path))
                await client(UploadProfilePhotoRequest(file=uploaded))
                result["avatar"] = True
                logger.info("头像已更新")

        # 修改用户名
        if username is not None:
            uname = username.strip().lstrip("@")
            if uname:
                try:
                    await client(UpdateUsernameRequest(username=uname))
                    result["username"] = True
                    logger.info("用户名已更新: @%s", uname)
                except Exception as e:
                    logger.error("修改用户名失败: %s", e)
                    result["username"] = False

        return result

    # ---- 加群/频道 ----

    async def join_groups(
        self,
        groups: list[str],
        *,
        delay: float = 1.5,
    ) -> dict[str, bool]:
        """
        加入群组/频道。
        groups: 群组/频道链接列表，支持格式:
          - @username
          - https://t.me/username
          - https://t.me/+xxxxx (邀请链接)
          - https://t.me/joinchat/xxxxx

        返回 {链接: 是否成功}。
        """
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import ImportChatInviteRequest

        client = self.client
        result: dict[str, bool] = {}

        for group in groups:
            g = group.strip()
            if not g:
                continue

            success = False
            for attempt in (1, 2):
                try:
                    if "joinchat/" in g or "/+" in g:
                        invite = g.rstrip("/").split("/")[-1].replace("+", "")
                        await client(ImportChatInviteRequest(hash=invite))
                    else:
                        # 提取 username: @xxx 或 https://t.me/xxx
                        name = g
                        if "/" in g:
                            name = g.rstrip("/").split("/")[-1]
                        name = name.lstrip("@")
                        await client(JoinChannelRequest(channel=name))
                    success = True
                    break
                except Exception as e:
                    if attempt == 2:
                        logger.error("加群失败: %s -> %s", g, e)
                    else:
                        await asyncio.sleep(max(1.0, delay))

            result[g] = success
            if success:
                logger.info("已加入: %s", g)
            await asyncio.sleep(max(0.0, delay))

        return result

    # ---- 清理 ----

    async def disconnect(self) -> None:
        if self._client:
            await self._client.disconnect()
            self._client = None
            self._me = None

    async def __aenter__(self):
        await self.login()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()


# ========================================================================
# CLI: 资料管理 & 加群
# ========================================================================


async def _cli_profile(args: argparse.Namespace) -> int:
    """CLI: 修改资料。"""
    mgr = TelegramManager()
    try:
        await mgr.login()

        name = getattr(args, "name", None)
        bio = getattr(args, "bio", None)
        avatar_path = getattr(args, "avatar", None)
        username = getattr(args, "username", None)

        if not any([name, bio, avatar_path, username]):
            # 显示当前状态
            me = mgr.me or await mgr.client.get_me()
            print(f"当前账号信息:")
            print(f"  名字: {me.first_name or ''} {me.last_name or ''}")
            print(f"  用户名: @{me.username or '(未设置)'}")
            print(f"  电话: {me.phone or '(隐藏)'}")
            return 0

        result = await mgr.update_profile(
            name=name,
            bio=bio,
            avatar=avatar_path,
            username=username,
        )

        for action, ok in result.items():
            status = "✓ 成功" if ok else "✗ 失败"
            print(f"  {action}: {status}")
        return 0
    finally:
        await mgr.disconnect()


async def _cli_join(args: argparse.Namespace) -> int:
    """CLI: 加群/频道。"""
    groups_str = getattr(args, "groups", "")
    if not groups_str:
        print("请用 --groups 指定群组/频道链接（逗号分隔）")
        return 1

    groups = [g.strip() for g in groups_str.split(",") if g.strip()]
    if not groups:
        print("无有效链接")
        return 1

    mgr = TelegramManager()
    try:
        await mgr.login()
        result = await mgr.join_groups(groups)
        for g, ok in result.items():
            print(f"  {'✓' if ok else '✗'} {g}")
        return 0
    finally:
        await mgr.disconnect()


async def _cli_login(args: argparse.Namespace) -> int:
    """CLI: 仅登录测试。"""
    mgr = TelegramManager()
    try:
        account_file = getattr(args, "account_file", None)
        if account_file:
            mgr.load_account_list(account_file)

        await mgr.login(force_relogin=getattr(args, "relogin", False))
        me = mgr.me
        print(f"登录成功!")
        print(f"  名字: {me.first_name or ''} {me.last_name or ''}")
        print(f"  用户名: @{me.username or '(未设置)'}")
        print(f"  ID: {me.id}")
        return 0
    finally:
        await mgr.disconnect()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Telegram 统一管理器：登录、上传、资料修改、加群",
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # login
    p_login = sub.add_parser("login", help="登录 Telegram")
    p_login.add_argument("--account-file", help="API 账号列表文件路径")
    p_login.add_argument("--relogin", action="store_true", help="强制重新登录")

    # profile
    p_profile = sub.add_parser("profile", help="查看/修改资料")
    p_profile.add_argument("--name", help="新名字")
    p_profile.add_argument("--bio", help="新简介（最多70字符）")
    p_profile.add_argument("--avatar", help="头像图片路径")
    p_profile.add_argument("--username", help="新用户名（不带@）")

    # join
    p_join = sub.add_parser("join", help="加入群组/频道")
    p_join.add_argument("--groups", required=True, help="群组/频道链接，逗号分隔")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "login":
        asyncio.run(_cli_login(args))
    elif args.command == "profile":
        asyncio.run(_cli_profile(args))
    elif args.command == "join":
        asyncio.run(_cli_join(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
