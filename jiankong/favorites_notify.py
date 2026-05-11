"""
MoonTV 收藏集数变更 → Telegram 通知。

拉取 GET /api/favorites，与本机 SQLite 比对集数后发 Telegram。
SQLite 默认保存在脚本同目录（可用 CONFIG「FAVORITES_SQLITE_DB」改成绝对路径）。
收藏「备注名」存在 SQLite 表 fav_display_names；通知与日志优先显示备注名（见 DEFAULT_DISPLAY_ALIASES）。

认证（二选一）：
  A) 推荐：在服务端设置环境变量 FAVORITES_SCRIPT_TOKEN、FAVORITES_SCRIPT_USERNAME，
     本脚本 CONFIG 填写同名令牌与用户名，请求头 Authorization: Bearer <token>，
     不再依赖浏览器 Cookie（绕过 middleware 签名校验问题）。
  B) Cookie：CONFIG 中 MOONTV_COOKIE_HEADER / MOONTV_COOKIE_FILE（见下方注释）。
"""

from __future__ import annotations

DEFAULT_COOKIE_TXT = "cookies.txt"

import json
import logging
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from http import cookiejar
from pathlib import Path
from typing import Any

# 收藏 item_key（与接口返回的 source+id 一致）→ 备注显示名；启动时写入 SQLite，可按需增删改本列表
DEFAULT_DISPLAY_ALIASES: list[tuple[str, str]] = [
    ("37+97662", "择天记"),
    ("maotaizy+91", "牧神记"),
    ("maotaizy+70658", "遮天"),
    ("16+121988", "完美世界"),
    ("23+81489", "盘龙"),
    ("29+72574", "仓元图"),
    ("jisu+106821", "星辰变"),
    ("jisu+105664", "将夜"),
]


# ============ 在下面填写配置（勿提交含密钥的版本到公开仓库）============
CONFIG: dict[str, str] = {
    "BASE_URL": "https://tv.658877.xyz",
    "TELEGRAM_BOT_TOKEN": "8647634770:AAFZrE4WD4999jkCdNPiNNUiUxnjCIh3wpc",
    "TELEGRAM_CHAT_ID": "8750984781",
    # —— 推荐：与服务端 docker / Vercel 环境变量一致 ——
    # 服务端需设置：FAVORITES_SCRIPT_TOKEN、FAVORITES_SCRIPT_USERNAME（MoonTV 登录用户名）
    "FAVORITES_SCRIPT_TOKEN": "",
    "FAVORITES_SCRIPT_USERNAME": "",
    # —— 或改用 Cookie（与上面令牌二选一；令牌非空时忽略 Cookie）——
    "MOONTV_COOKIE_HEADER": "",
    "MOONTV_COOKIE_FILE": "",
    # 状态库：绝对路径，或相对脚本目录；留空 → 脚本目录/moontv_favorites_state.sqlite
    "FAVORITES_SQLITE_DB": "",
    "MOONTV_USER_LABEL": "",
    # 自动化：集数变更并通知后，解析 m3u8 → 写入 SQLite → 下载 → 上传 → 删除本地文件
    # 填 "1" 开启（默认开启）；须在 jiankong/pipeline_config.py 配置 ITEM_KEY_TO_SHOW_ID
    "PIPELINE_ENABLED": "1",
    # 默认 moon_tv：/api/search 按 source_name 匹配，读 episodes 取 m3u8；stub|placeholder|import
    "M3U8_RESOLVER_MODE": "",
}


def apply_script_config() -> None:
    for key, val in CONFIG.items():
        if val is None or str(val).strip() == "":
            continue
        os.environ[key] = str(val).strip()


def setup_logging() -> None:
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    cfg = dict(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    if sys.version_info >= (3, 8):
        cfg["force"] = True
    logging.basicConfig(**cfg)


def normalize_base_url(url: str) -> str:
    url = url.replace("\r", "").strip()
    if url.startswith("["):
        url = url.strip("[]")
    return url.rstrip("/")


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def resolve_favorites_db_path() -> Path:
    """优先使用项目根 config.yaml 的 database.path，与下载模块共用 SQLite。"""
    root = script_dir().parent
    rs = str(root.resolve())
    if rs not in sys.path:
        sys.path.insert(0, rs)
    try:
        from app.config_loader import database_path, load_config

        return database_path(load_config())
    except Exception as e:
        logging.debug("使用默认本地库（检查项目根 config.yaml）: %s", e)
        return script_dir() / "moontv_favorites_state.sqlite"


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def normalize_moontv_cookie_header(raw: str) -> str:
    s = raw.strip()
    if not s:
        return s
    if s[:8].lower().startswith("cookie:"):
        s = s.split(":", 1)[1].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    if "=" in s:
        return s
    logging.info("Cookie 无「名=值」，已自动加 auth= 前缀")
    return f"auth={s}"


def _browser_headers(*, cookie: str | None, origin: str, bearer: str | None) -> dict[str, str]:
    h: dict[str, str] = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if origin:
        o = origin.rstrip("/")
        h["Origin"] = o
        h["Referer"] = f"{o}/"
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    if cookie:
        h["Cookie"] = cookie
    return h


def resolve_browser_cookie_only() -> tuple[str | None, str | None]:
    header = (os.environ.get("MOONTV_COOKIE_HEADER") or "").strip()
    if header:
        return normalize_moontv_cookie_header(header), None

    cf_raw = (os.environ.get("MOONTV_COOKIE_FILE") or "").strip()
    if cf_raw:
        p = Path(cf_raw).expanduser().resolve()
        if not p.is_file():
            logging.error("MOONTV_COOKIE_FILE 不存在: %s", p)
            return None, None
        return None, str(p)

    default_path = script_dir() / DEFAULT_COOKIE_TXT
    if default_path.is_file():
        return None, str(default_path.resolve())

    logging.error(
        "未配置 FAVORITES_SCRIPT_TOKEN 时，需要 MOONTV_COOKIE_* 或脚本目录 %s",
        default_path,
    )
    return None, None


def _cookie_plain_file_to_header(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
    parts = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not parts:
        raise ValueError("cookies.txt 无有效内容")
    merged = "; ".join(parts)
    return normalize_moontv_cookie_header(merged)


def http_get_favorites(
    url: str,
    *,
    origin: str,
    bearer_token: str | None,
    cookie_header: str | None,
    cookie_file: str | None,
) -> bytes:
    logging.info("HTTP GET %s", url)

    if bearer_token:
        req = urllib.request.Request(
            url,
            headers=_browser_headers(
                cookie=None, origin=origin, bearer=bearer_token.strip()
            ),
            method="GET",
        )
        return urllib.request.urlopen(req, timeout=90).read()

    if cookie_header:
        ch = normalize_moontv_cookie_header(cookie_header.strip())
        req = urllib.request.Request(
            url,
            headers=_browser_headers(cookie=ch, origin=origin, bearer=None),
            method="GET",
        )
        return urllib.request.urlopen(req, timeout=90).read()

    if cookie_file:
        try:
            cj = cookiejar.MozillaCookieJar(cookie_file)
            cj.load(ignore_discard=True, ignore_expires=True)
        except (cookiejar.LoadError, OSError) as e:
            logging.debug("Netscape 解析失败，改纯文本: %s", e)
            try:
                merged = _cookie_plain_file_to_header(Path(cookie_file))
            except ValueError as ve:
                logging.error("%s", ve)
                raise SystemExit(2) from ve
            req = urllib.request.Request(
                url,
                headers=_browser_headers(cookie=merged, origin=origin, bearer=None),
                method="GET",
            )
            return urllib.request.urlopen(req, timeout=90).read()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj)
        )
        req = urllib.request.Request(
            url,
            headers=_browser_headers(cookie=None, origin=origin, bearer=None),
            method="GET",
        )
        return opener.open(req, timeout=90).read()

    raise SystemExit("需要 Bearer 令牌或 Cookie")


def ensure_display_alias_table(cur: sqlite3.Cursor) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fav_display_names (
          item_key TEXT PRIMARY KEY,
          display_name TEXT NOT NULL
        )
        """
    )


def upsert_display_aliases(cur: sqlite3.Cursor) -> None:
    """把脚本里的默认映射同步进库（同一 item_key 会更新备注名）。"""
    for item_key, display_name in DEFAULT_DISPLAY_ALIASES:
        cur.execute(
            """
            INSERT INTO fav_display_names (item_key, display_name)
            VALUES (?, ?)
            ON CONFLICT(item_key) DO UPDATE SET display_name = excluded.display_name
            """,
            (item_key, display_name),
        )


def load_alias_map(cur: sqlite3.Cursor) -> dict[str, str]:
    cur.execute("SELECT item_key, display_name FROM fav_display_names")
    return dict(cur.fetchall())


def label_for_item(alias_map: dict[str, str], item_key: str, api_title: str) -> str:
    return (alias_map.get(item_key) or "").strip() or api_title


def send_telegram(chat_id: str, text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    payload = json.dumps(
        {"chat_id": chat_id, "text": text}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=45).read()
    logging.info("已发送 Telegram 至 chat_id=%s", chat_id)


def run() -> int:
    apply_script_config()

    base_raw = (os.environ.get("BASE_URL") or "").strip()
    if not base_raw:
        logging.error('请填写 CONFIG["BASE_URL"]')
        return 1

    base = normalize_base_url(base_raw)
    if not base.startswith(("http://", "https://")):
        logging.error("BASE_URL 无效: %s", base[:80])
        return 1

    bearer = (os.environ.get("FAVORITES_SCRIPT_TOKEN") or "").strip()
    script_user = (os.environ.get("FAVORITES_SCRIPT_USERNAME") or "").strip()

    cookie_header: str | None = None
    cookie_file: str | None = None

    if bearer:
        if not script_user:
            logging.error(
                "使用 FAVORITES_SCRIPT_TOKEN 时必须填写 CONFIG[FAVORITES_SCRIPT_USERNAME]（MoonTV 用户名）"
            )
            return 1
        logging.info(
            "使用 Bearer 令牌拉取用户「%s」的收藏（服务端需配置同名 TOKEN 与 USERNAME）",
            script_user,
        )
    else:
        cookie_header, cookie_file = resolve_browser_cookie_only()
        if not cookie_header and not cookie_file:
            return 1

    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(key):
            logging.error("请填写 CONFIG[%s]", repr(key))
            return 1

    url = f"{base}/api/favorites"
    try:
        raw = http_get_favorites(
            url,
            origin=base,
            bearer_token=bearer if bearer else None,
            cookie_header=cookie_header,
            cookie_file=cookie_file,
        )
    except urllib.error.HTTPError as e:
        logging.error("HTTP %s %s", e.code, e.reason)
        try:
            logging.info("响应片段: %s", e.read().decode("utf-8", errors="replace")[:800])
        except Exception:
            pass
        if e.code == 401:
            logging.error(
                "401：若用 Cookie，请从 Network 复制完整 Cookie；若用 Bearer，请核对服务端 TOKEN/USERNAME 并重新部署"
            )
        elif e.code == 500:
            logging.error(
                "500：服务端是否已设置 FAVORITES_SCRIPT_USERNAME（与 Bearer 搭配）？"
            )
        return 1
    except Exception as e:
        logging.exception("请求失败: %s", e)
        return 1

    text_body = raw.decode("utf-8", errors="replace")

    try:
        data = json.loads(text_body)
    except json.JSONDecodeError as e:
        logging.error("非 JSON 响应: %s\n片段: %s", e, text_body[:800])
        return 1

    if isinstance(data, dict) and data.get("error"):
        logging.error("接口错误: %s", data)
        return 1

    if not isinstance(data, dict):
        logging.error("期望 JSON 对象，实际 %s", type(data).__name__)
        return 1

    env_db = (os.environ.get("FAVORITES_SQLITE_DB") or "").strip()
    if env_db:
        db_path = Path(env_db).expanduser()
        if not db_path.is_absolute():
            db_path = (script_dir() / db_path).resolve()
    else:
        db_path = resolve_favorites_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    logging.info("SQLite: %s", db_path)

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    try:
        rp = str(script_dir().parent.resolve())
        if rp not in sys.path:
            sys.path.insert(0, rp)
        from app.store import ensure_schema

        ensure_schema(cur)
    except Exception as e:
        logging.debug("扩展 SQLite 表结构（与下载模块统一）: %s", e)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fav_items (
          item_key TEXT PRIMARY KEY,
          total_episodes INTEGER NOT NULL,
          title TEXT,
          last_total INTEGER NOT NULL
        )
        """
    )
    ensure_display_alias_table(cur)
    upsert_display_aliases(cur)
    conn.commit()

    alias_map = load_alias_map(cur)

    # --- 多提供商比较：按动漫名称分组，取每组最高集数 ---
    from jiankong.provider_compare import compare_and_get_max_episodes

    grouped = compare_and_get_max_episodes(data, alias_map)

    changes: list[dict[str, Any]] = []
    items_seen = len(data)

    # 创建以 item_key 索引的旧集数映射
    old_total_map: dict[str, int] = {}
    for row_old in cur.execute("SELECT item_key, total_episodes, last_total FROM fav_items").fetchall():
        old_total_map[row_old[0]] = int(row_old[1])

    for display_name, best in grouped.items():
        item_key = best["key"]
        title = best["title"]
        total = best["total"]
        source_name = best["source_name"]
        source_id = best["source_id"]
        vod_id = best["vod_id"]

        old = old_total_map.get(item_key)
        if old is None:
            # 新条目：基线入库
            cur.execute(
                """INSERT INTO fav_items (item_key, total_episodes, title, last_total)
                   VALUES (?,?,?,?)""",
                (item_key, total, title, total),
            )
            logging.info("基线入库（不发通知）: %s total=%s", item_key, total)
            continue

        # 检查是否所有提供商的最高集数超过了旧记录
        if total != old:
            changes.append(
                {
                    "key": item_key,
                    "title": title,
                    "display_name": display_name,
                    "oldTotal": old,
                    "newTotal": total,
                    "source_name": source_name,
                    "source_id": source_id,
                    "vod_id": vod_id,
                }
            )
            cur.execute(
                """UPDATE fav_items SET total_episodes = ?, title = ?, last_total = ?
                   WHERE item_key = ?""",
                (total, title, total, item_key),
            )
            logging.info(
                "集数变化（多提供商最高）: %s | %s → %s (provider=%s)",
                title or item_key, old, total,
                source_name or source_id or "unknown",
            )

    conn.commit()
    conn.close()

    logging.info("收藏条目数 %s，变更 %s", items_seen, len(changes))

    if not changes:
        logging.info("无变更，不发 Telegram")
        return 0

    label = (os.environ.get("MOONTV_USER_LABEL") or "").strip()
    hdr = "📺 MoonTV 收藏集数变更"
    if label:
        hdr += f" [{label}]"

    lines = [
        f"- {c.get('display_name') or c['title'] or c['key']} "
        f"{c['oldTotal']}→{c['newTotal']}"
        for c in changes
    ]
    msg = (hdr + "\n" + "\n".join(lines))[:3900]

    try:
        send_telegram(os.environ["TELEGRAM_CHAT_ID"], msg)
    except Exception:
        logging.exception("Telegram 发送失败")
        return 1

    logging.info("通知完成，%s 条变更", len(changes))

    pe = (os.environ.get("PIPELINE_ENABLED") or "").strip().lower()
    if pe in ("1", "true", "yes", "on"):
        try:
            repo_root = script_dir().parent
            root_s = str(repo_root)
            if root_s not in sys.path:
                sys.path.insert(0, root_s)
            from jiankong.pipeline import run_pipeline_for_changes

            run_pipeline_for_changes(changes)
        except Exception:
            logging.exception("自动化流水线失败（Telegram 通知已成功发出）")

    return 0


def main() -> int:
    """命令行入口，支持单次运行和循环监控模式。

    用法:
      python jiankong/favorites_notify.py              # 单次检查
      python jiankong/favorites_notify.py --loop        # 循环监控（默认 30 分钟）
      python jiankong/favorites_notify.py --loop --interval 600  # 每 10 分钟
    """
    import argparse

    parser = argparse.ArgumentParser(description="MoonTV 收藏监控 + 自动下载上传")
    parser.add_argument("--loop", action="store_true", help="循环监控模式")
    parser.add_argument(
        "--interval", type=int, default=1800,
        help="循环间隔秒数（默认 1800 = 30 分钟）"
    )
    args = parser.parse_args()

    setup_logging()

    if not args.loop:
        logging.info("启动 favorites_notify（单次）")
        try:
            return run()
        except KeyboardInterrupt:
            logging.info("中断")
            return 130

    # 循环监控模式
    logging.info(
        "启动 favorites_notify 循环监控（间隔 %s 秒 = %s 分钟）",
        args.interval, round(args.interval / 60, 1),
    )
    import time

    while True:
        try:
            ret = run()
            if ret != 0:
                logging.warning("本轮检查返回码=%s，继续下一轮", ret)
        except KeyboardInterrupt:
            logging.info("循环监控已停止")
            return 0
        except Exception:
            logging.exception("本轮检查异常，%s 秒后重试", args.interval)

        logging.info("等待 %s 秒后下一轮检查...", args.interval)
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            logging.info("循环监控已停止")
            return 0


if __name__ == "__main__":
    sys.exit(main())