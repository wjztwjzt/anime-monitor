from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TelegramAccountConfig(BaseModel):
    session_name: str
    phone: str
    code_api_url: str = ""
    two_fa_password: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_api_id: int = Field(alias="TELEGRAM_API_ID")
    telegram_api_hash: str = Field(alias="TELEGRAM_API_HASH")

    use_proxy: bool = Field(default=False, alias="USE_PROXY")
    proxy_host: str = Field(default="127.0.0.1", alias="PROXY_HOST")
    proxy_port: int = Field(default=7890, alias="PROXY_PORT")
    proxy_username: str = Field(default="", alias="PROXY_USERNAME")
    proxy_password: str = Field(default="", alias="PROXY_PASSWORD")

    tg1_session_name: str = Field(default="", alias="TG1_SESSION_NAME")
    tg1_phone: str = Field(default="", alias="TG1_PHONE")
    tg2_session_name: str = Field(default="", alias="TG2_SESSION_NAME")
    tg2_phone: str = Field(default="", alias="TG2_PHONE")
    tg3_session_name: str = Field(default="", alias="TG3_SESSION_NAME")
    tg3_phone: str = Field(default="", alias="TG3_PHONE")
    tg4_session_name: str = Field(default="", alias="TG4_SESSION_NAME")
    tg4_phone: str = Field(default="", alias="TG4_PHONE")
    tg5_session_name: str = Field(default="", alias="TG5_SESSION_NAME")
    tg5_phone: str = Field(default="", alias="TG5_PHONE")
    tg6_session_name: str = Field(default="", alias="TG6_SESSION_NAME")
    tg6_phone: str = Field(default="", alias="TG6_PHONE")
    tg7_session_name: str = Field(default="", alias="TG7_SESSION_NAME")
    tg7_phone: str = Field(default="", alias="TG7_PHONE")
    tg8_session_name: str = Field(default="", alias="TG8_SESSION_NAME")
    tg8_phone: str = Field(default="", alias="TG8_PHONE")
    tg9_session_name: str = Field(default="", alias="TG9_SESSION_NAME")
    tg9_phone: str = Field(default="", alias="TG9_PHONE")
    tg10_session_name: str = Field(default="", alias="TG10_SESSION_NAME")
    tg10_phone: str = Field(default="", alias="TG10_PHONE")
    tg11_session_name: str = Field(default="", alias="TG11_SESSION_NAME")
    tg11_phone: str = Field(default="", alias="TG11_PHONE")
    tg12_session_name: str = Field(default="", alias="TG12_SESSION_NAME")
    tg12_phone: str = Field(default="", alias="TG12_PHONE")
    tg13_session_name: str = Field(default="", alias="TG13_SESSION_NAME")
    tg13_phone: str = Field(default="", alias="TG13_PHONE")
    tg14_session_name: str = Field(default="", alias="TG14_SESSION_NAME")
    tg14_phone: str = Field(default="", alias="TG14_PHONE")
    tg15_session_name: str = Field(default="", alias="TG15_SESSION_NAME")
    tg15_phone: str = Field(default="", alias="TG15_PHONE")
    tg16_session_name: str = Field(default="", alias="TG16_SESSION_NAME")
    tg16_phone: str = Field(default="", alias="TG16_PHONE")
    tg17_session_name: str = Field(default="", alias="TG17_SESSION_NAME")
    tg17_phone: str = Field(default="", alias="TG17_PHONE")
    tg18_session_name: str = Field(default="", alias="TG18_SESSION_NAME")
    tg18_phone: str = Field(default="", alias="TG18_PHONE")
    tg19_session_name: str = Field(default="", alias="TG19_SESSION_NAME")
    tg19_phone: str = Field(default="", alias="TG19_PHONE")
    tg20_session_name: str = Field(default="", alias="TG20_SESSION_NAME")
    tg20_phone: str = Field(default="", alias="TG20_PHONE")
    login_account: str = Field(default="TG1", alias="LOGIN_ACCOUNT")
    login_account1: str = Field(default="TG1", alias="LOGIN_ACCOUNT1")
    max_active_accounts: int = Field(default=20, alias="MAX_ACTIVE_ACCOUNTS")

    source_group: str = Field(alias="SOURCE_GROUP")
    target_group: str = Field(alias="TARGET_GROUP")
    enable_auto_join_groups: bool = Field(default=False, alias="ENABLE_AUTO_JOIN_GROUPS")
    join_groups: str = Field(default="", alias="JOIN_GROUPS")
    chat_enabled: bool = Field(default=True, alias="CHAT_ENABLED")
    profile_apply_enabled: bool = Field(default=False, alias="PROFILE_APPLY_ENABLED")
    chat_collect_enabled: bool = Field(default=False, alias="CHAT_COLLECT_ENABLED")
    # 为 True 时：只要 CHAT_COLLECT_ENABLED=1 即启动监听，不必再设 Redis CHAT_COLLECT_START=1。
    # 若需仅用 Redis 控制开关，可设为 false，并在 Redis 写入 CHAT_COLLECT_START=1。
    chat_collect_immediate_start: bool = Field(default=True, alias="CHAT_COLLECT_IMMEDIATE_START")
    chat_collect_target_group: str = Field(default="", alias="CHAT_COLLECT_TARGET_GROUP")
    chat_collect_min_chars: int = Field(default=2, alias="CHAT_COLLECT_MIN_CHARS")
    chat_collect_max_chars: int = Field(default=20, alias="CHAT_COLLECT_MAX_CHARS")
    chat_collect_days_per_run: int = Field(default=3, alias="CHAT_COLLECT_DAYS_PER_RUN")
    chat_collect_daily_target: int = Field(default=6000, alias="CHAT_COLLECT_DAILY_TARGET")
    chat_collect_scan_limit: int = Field(default=120000, alias="CHAT_COLLECT_SCAN_LIMIT")
    chat_collect_head_scan_limit: int = Field(default=2000, alias="CHAT_COLLECT_HEAD_SCAN_LIMIT")
    # 每轮从库中预约多少条爬取记录去发送；0 表示不限制（全部未使用行）
    chat_collect_send_reserve_limit: int = Field(default=0, alias="CHAT_COLLECT_SEND_RESERVE_LIMIT")

    storage_backend: Literal["sqlite", "redis", "both", "file"] = Field(
        default="file", alias="STORAGE_BACKEND"
    )

    # 相对路径时相对项目根目录（与 tg_chat_sim 同级的 liao 根）
    sqlite_database: str = Field(
        default="data/telegram_chat_sim.db", alias="SQLITE_DATABASE"
    )

    # file 后端：与 prepare_chat_records.py 生成的 chat_records.xlsx（项目根目录）
    chat_records_xlsx: str = Field(
        default="chat_records.xlsx", alias="CHAT_RECORDS_XLSX"
    )
    # 从 xlsx 生成聊天脚本时间轴时，相邻两条消息在「计划时间轴」上的间隔（秒）；越大发言越稀
    chat_script_row_interval_seconds: int = Field(
        default=15, alias="CHAT_SCRIPT_ROW_INTERVAL_SECONDS"
    )
    # 每轮 send_chat_script_once 跑完后，再等待多少秒才开始下一轮（整轮 xlsx 循环之间的空隙）
    chat_between_rounds_seconds: int = Field(
        default=20, alias="CHAT_BETWEEN_ROUNDS_SECONDS"
    )
    # 每条消息 send_message 成功后，额外随机等待 [min,max] 秒（0 与 0 表示关闭）
    chat_after_send_min_seconds: int = Field(
        default=4, alias="CHAT_AFTER_SEND_MIN_SECONDS"
    )
    chat_after_send_max_seconds: int = Field(
        default=12, alias="CHAT_AFTER_SEND_MAX_SECONDS"
    )
    # 从 chat_records.xlsx 加载脚本时最多取多少行（与 SIMULATE_SEND_LIMIT 无关）
    chat_script_xlsx_message_limit: int = Field(
        default=2000, alias="CHAT_SCRIPT_XLSX_MESSAGE_LIMIT"
    )

    redis_host: str = Field(default="127.0.0.1", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_db: int = Field(default=0, alias="REDIS_DB")
    redis_password: str = Field(default="", alias="REDIS_PASSWORD")
    redis_key_prefix: str = Field(default="tg_sim", alias="REDIS_KEY_PREFIX")
    redis_socket_connect_timeout: float = Field(
        default=10.0, alias="REDIS_SOCKET_CONNECT_TIMEOUT"
    )
    redis_socket_timeout: float = Field(default=30.0, alias="REDIS_SOCKET_TIMEOUT")

    cp_limit: int = Field(default=100, alias="CP_LIMIT")
    simulate_send_limit: int = Field(default=40, alias="SIMULATE_SEND_LIMIT")
    simulate_min_interval_seconds: int = Field(
        default=3, alias="SIMULATE_MIN_INTERVAL_SECONDS"
    )
    simulate_max_interval_seconds: int = Field(
        default=12, alias="SIMULATE_MAX_INTERVAL_SECONDS"
    )
    simulate_loop: bool = Field(default=True, alias="SIMULATE_LOOP")
    enable_human_style: bool = Field(default=True, alias="ENABLE_HUMAN_STYLE")
    rewrite_probability: float = Field(default=0.6, alias="REWRITE_PROBABILITY")
    mention_probability: float = Field(default=0.35, alias="MENTION_PROBABILITY")
    emoji_probability: float = Field(default=0.5, alias="EMOJI_PROBABILITY")
    mention_candidates: str = Field(default="", alias="MENTION_CANDIDATES")
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL"
    )
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")
    deepseek_system_prompt: str = Field(
        default="你是一个中文社群聊天润色助手，输出自然、简短、有人味的文本。",
        alias="DEEPSEEK_SYSTEM_PROMPT",
    )
    filter_max_chars: int = Field(default=180, alias="FILTER_MAX_CHARS")
    ad_keywords: str = Field(
        default="加我,私聊,兼职,返利,推广,代理,招商,引流,点击链接,t.me/,http://,https://",
        alias="AD_KEYWORDS",
    )
    persona_1: str = Field(default="理性简短，喜欢给结论", alias="PERSONA_1")
    persona_2: str = Field(default="热情活跃，常用感叹句", alias="PERSONA_2")
    persona_3: str = Field(default="中立补充，偏解释型", alias="PERSONA_3")
    persona_4: str = Field(default="轻松幽默，偶尔调侃", alias="PERSONA_4")
    persona_5: str = Field(default="温和礼貌，偏鼓励", alias="PERSONA_5")
    persona_6: str = Field(default="务实直接，少废话", alias="PERSONA_6")
    persona_7: str = Field(default="好奇提问，推动讨论", alias="PERSONA_7")
    persona_8: str = Field(default="观察总结，客观中立", alias="PERSONA_8")
    persona_9: str = Field(default="轻松口语，接地气", alias="PERSONA_9")
    persona_10: str = Field(default="理性分析，给建议", alias="PERSONA_10")

    @field_validator("proxy_port", mode="before")
    @classmethod
    def _normalize_proxy_port(cls, value: object) -> object:
        if value == "":
            return 7890
        return value

    @field_validator("storage_backend", mode="before")
    @classmethod
    def _normalize_storage_backend(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() == "mysql":
            return "sqlite"
        return value

    @property
    def chat_records_path(self) -> Path:
        p = Path(self.chat_records_xlsx)
        if p.is_absolute():
            return p
        return Path(__file__).resolve().parent.parent / p

    @property
    def sqlite_path(self) -> Path:
        p = Path(self.sqlite_database)
        if p.is_absolute():
            return p
        return Path(__file__).resolve().parent.parent / p

    @property
    def sqlite_url(self) -> str:
        path = self.sqlite_path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path.as_posix()}"

    @property
    def redis_url(self) -> str:
        auth_part = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth_part}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def accounts(self) -> list[TelegramAccountConfig]:
        try:
            from tg_chat_sim.project_yaml import load_telegram_accounts_from_yaml

            yaml_accounts = load_telegram_accounts_from_yaml(self)
            if yaml_accounts:
                return yaml_accounts
        except Exception:
            pass

        raw_accounts = [
            (self.tg1_session_name, self.tg1_phone),
            (self.tg2_session_name, self.tg2_phone),
            (self.tg3_session_name, self.tg3_phone),
            (self.tg4_session_name, self.tg4_phone),
            (self.tg5_session_name, self.tg5_phone),
            (self.tg6_session_name, self.tg6_phone),
            (self.tg7_session_name, self.tg7_phone),
            (self.tg8_session_name, self.tg8_phone),
            (self.tg9_session_name, self.tg9_phone),
            (self.tg10_session_name, self.tg10_phone),
            (self.tg11_session_name, self.tg11_phone),
            (self.tg12_session_name, self.tg12_phone),
            (self.tg13_session_name, self.tg13_phone),
            (self.tg14_session_name, self.tg14_phone),
            (self.tg15_session_name, self.tg15_phone),
            (self.tg16_session_name, self.tg16_phone),
            (self.tg17_session_name, self.tg17_phone),
            (self.tg18_session_name, self.tg18_phone),
            (self.tg19_session_name, self.tg19_phone),
            (self.tg20_session_name, self.tg20_phone),
        ]
        accounts = [
            TelegramAccountConfig(session_name=session_name.strip(), phone=phone.strip())
            for session_name, phone in raw_accounts
            if session_name.strip() and phone.strip()
        ]
        return accounts[: max(1, int(self.max_active_accounts))]

    @property
    def personas(self) -> list[str]:
        return [
            self.persona_1,
            self.persona_2,
            self.persona_3,
            self.persona_4,
            self.persona_5,
            self.persona_6,
            self.persona_7,
            self.persona_8,
            self.persona_9,
            self.persona_10,
        ]

    @property
    def mention_users(self) -> list[str]:
        return [x.strip().lstrip("@") for x in self.mention_candidates.split(",") if x.strip()]

    @property
    def ad_keyword_list(self) -> list[str]:
        return [x.strip().lower() for x in self.ad_keywords.split(",") if x.strip()]

    @property
    def join_group_list(self) -> list[str]:
        return [x.strip() for x in self.join_groups.split(",") if x.strip()]

    def resolve_session_name_from_login_tag(self, tag: str | None) -> str:
        """
        LOGIN_ACCOUNT / LOGIN_ACCOUNT1 填 TG1..TG20 时按当前 accounts 顺序解析 session_name；
        也可直接填 tg_user_1 等与账号列表一致的 session 名。
        """
        t = (tag or "TG1").strip()
        if not t:
            return ""
        up = t.upper()
        accs = self.accounts
        if up.startswith("TG") and len(up) > 2 and up[2:].isdigit():
            idx = int(up[2:]) - 1
            if 0 <= idx < len(accs):
                return accs[idx].session_name
        for a in accs:
            if a.session_name == t:
                return t
        env_key = f"{up.lower()}_session_name"
        if hasattr(self, env_key):
            v = getattr(self, env_key, "")
            return v if isinstance(v, str) else ""
        return ""
