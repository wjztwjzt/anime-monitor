from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CopiedMessage(Base):
    __tablename__ = "copied_messages"
    __table_args__ = (
        UniqueConstraint("source_group", "source_message_id", name="uniq_source_msg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_group: Mapped[str] = mapped_column(String(255), nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    message_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AccountProxyConfig(Base):
    __tablename__ = "account_proxy_configs"

    session_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    proxy_host: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    proxy_port: Mapped[int] = mapped_column(Integer, default=7890, nullable=False)
    proxy_username: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    proxy_password: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class AccountProfileConfig(Base):
    __tablename__ = "account_profile_configs"

    session_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    avatar_local_path: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    bio: Mapped[str] = mapped_column(Text, default="", nullable=False)
    apply_changes: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ChatScript(Base):
    __tablename__ = "chat_scripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_group: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class ChatScriptMessage(Base):
    __tablename__ = "chat_script_messages"
    __table_args__ = (
        UniqueConstraint("script_id", "speaker_index", "seq_in_speaker", name="uniq_chat_msg"),
        # When ordering by time, an index helps for large scripts.
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    script_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_scripts.id"), nullable=False, index=True
    )
    # 1..10 mapping to user1..user10 in the excel we upload.
    speaker_index: Mapped[int] = mapped_column(Integer, nullable=False)
    seq_in_speaker: Mapped[int] = mapped_column(Integer, nullable=False)
    message_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)


class ChatCollectedMessage(Base):
    __tablename__ = "chat_collected_messages"
    __table_args__ = (
        UniqueConstraint("source_group", "source_message_id", name="uniq_collected_source_msg"),
    )

    # SQLite：自增主键须为 Integer，勿用 BigInteger 作主键（否则无 AUTOINCREMENT，INSERT 会报 id NOT NULL）
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_session_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_group: Mapped[str] = mapped_column(String(255), nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_username: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    sender_display_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    used_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChatCollectProgress(Base):
    __tablename__ = "chat_collect_progress"
    __table_args__ = (
        UniqueConstraint(
            "account_session_name",
            "source_group",
            "collect_date",
            name="uniq_collect_progress",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_session_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_group: Mapped[str] = mapped_column(String(255), nullable=False)
    collect_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="done", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChatCollectState(Base):
    __tablename__ = "chat_collect_state"
    __table_args__ = (
        UniqueConstraint("account_session_name", "source_group", name="uniq_collect_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_session_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_group: Mapped[str] = mapped_column(String(255), nullable=False)
    oldest_scanned_msg_id: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_collect_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    daily_collected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
