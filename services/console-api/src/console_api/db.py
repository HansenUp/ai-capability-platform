"""Console 库（S3.3）：生产为 MySQL，本机默认 SQLite。

只存运营侧控制面的持久数据：Agent、Agent 版本快照与发布 / 灰度状态、可见范围、Agent 归属（user_agents）、审计。
节点注册、心跳与指令队列是易失状态，仍在进程内存（多副本部署时迁到 Redis，见 S7）。

表名统一加 ``t_console_`` 前缀、Alembic 版本表为 ``alembic_version_console``：与 Runtime 共用同一个 MySQL 库时互不冲突。
schema 只由 Alembic 管理（``services/console-api/migrations``），启动时程序化执行 ``upgrade head``。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, Connection, DateTime, Dialect, Integer, String, Text, TypeDecorator, UniqueConstraint, make_url
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

DATABASE_URL = os.environ.get("CONSOLE_DATABASE_URL", "sqlite+aiosqlite:///./console.db")  # Console 库连接串
VERSION_TABLE = "alembic_version_console"  # 与 Runtime 的 alembic_version 区分
# 可编辑安装时为源码树下的 services/console-api/migrations；容器镜像内由 CONSOLE_MIGRATIONS_DIR 指定
MIGRATIONS_DIR = Path(os.environ.get("CONSOLE_MIGRATIONS_DIR") or Path(__file__).resolve().parents[2] / "migrations")


def _now() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """按 UTC 存取的时间类型（与 Runtime 同一约定）：写入去时区存 naive UTC，读出补回 UTC；MySQL 为 DATETIME(6)。"""

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name in ("mysql", "sqlite") else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    """Console ORM 基类。"""


class Agent(Base):
    """Agent（租户内唯一）。"""

    __tablename__ = "t_console_agent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    __table_args__ = (UniqueConstraint("tenant_id", "agent_id", name="uq_console_agent"),)


class AgentVersion(Base):
    """Agent 版本快照与发布状态：draft → published（含灰度）→ retired。"""

    __tablename__ = "t_console_agent_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | published | retired
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    model_alias: Mapped[str] = mapped_column(String(64), default="default-chat")
    harness: Mapped[str | None] = mapped_column(String(32), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    eval_run_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    canary_percent: Mapped[int] = mapped_column(Integer, default=100)  # 灰度放量比例，100 为全量
    canary_allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)  # 灰度白名单主体（如 human:alice）
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("tenant_id", "agent_id", "version", name="uq_console_agent_version"),)


class Visibility(Base):
    """Agent 在门户中的可见范围。"""

    __tablename__ = "t_console_visibility"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(String(64))
    groups: Mapped[list[str]] = mapped_column(JSON, default=list)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)
    __table_args__ = (UniqueConstraint("tenant_id", "agent_id", name="uq_console_visibility"),)


class UserAgent(Base):
    """Agent 归属：ADMIN 只能管理分配给自己的 Agent。"""

    __tablename__ = "t_console_user_agent"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    user: Mapped[str] = mapped_column(String(128))
    agent_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("tenant_id", "user", "agent_id", name="uq_console_user_agent"),)


class Audit(Base):
    """Console 变更审计：谁、何时、在哪个租户、做了什么、对象与详情。"""

    __tablename__ = "t_console_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    tenant_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    object: Mapped[str] = mapped_column(String(128))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


def normalize_database_url(url: str) -> URL:
    """MySQL 未指定 charset 时补 utf8mb4。"""
    u = make_url(url)
    if u.get_backend_name() == "mysql" and "charset" not in u.query:
        u = u.update_query_dict({"charset": "utf8mb4"})
    return u


def make_engine(url: str) -> AsyncEngine:
    """创建异步引擎；MySQL 开启 pool_pre_ping 与 pool_recycle。"""
    u = normalize_database_url(url)
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "pool_recycle": 3600} if u.get_backend_name() == "mysql" else {}
    return create_async_engine(u, **kwargs)


engine = make_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def alembic_config() -> Config:
    """指向 Console 迁移目录的 Alembic 配置（不读 alembic.ini）。"""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def _upgrade_head(connection: Connection) -> None:
    cfg = alembic_config()
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


async def init_db() -> None:
    """启动时把 Console 库升级到最新迁移版本。"""
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade_head)
