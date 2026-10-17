"""Engine database (MySQL in production, SQLite for local dev). Only configuration, runs, the Run
event log, usage and audit live here. Traces and evaluation data are external (Langfuse).
（引擎数据库（生产环境为 MySQL，本地开发为 SQLite）：仅存放配置（AgentVersion）、Run 元信息、
Run 事件日志、用量与审计数据；调用链追踪（trace）与评测数据存放在外部系统（Langfuse），
不在本库范围内。）
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, Connection, DateTime, Dialect, Index, Integer, String, Text, UniqueConstraint, inspect, make_url
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

from runtime.config import settings

# Alembic 迁移目录：默认取源码布局下的 services/runtime/migrations（db.py 向上 3 级即 services/runtime）；
# 非可编辑安装（如容器镜像）时包已不在源码树里，由 RUNTIME_MIGRATIONS_DIR 指向随镜像拷贝的迁移目录。
MIGRATIONS_DIR = Path(os.environ.get("RUNTIME_MIGRATIONS_DIR") or Path(__file__).resolve().parents[3] / "migrations")


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""

    pass


def _now() -> datetime:
    """返回当前 UTC 时间，用作各表时间字段的默认值工厂。"""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """统一按 UTC 存取的时间类型，屏蔽各方言对时区支持的差异。

    MySQL 的 DATETIME 与 SQLite 都不保存时区：写入带时区的值会被直接丢弃偏移，读出的是
    naive datetime，与内存中的 aware datetime 比较会抛 "can't compare offset-naive and
    offset-aware datetimes"。这里约定：写入前一律换算到 UTC 并去掉 tzinfo（naive 输入视作 UTC），
    读出时补回 UTC 时区，保证业务代码拿到的永远是 aware UTC 时间。
    MySQL 下使用 DATETIME(6) 保留微秒，避免同一秒内的事件时间被截断成相同值。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """按方言选择底层列类型：MySQL 用 DATETIME(fsp=6)，其余沿用 DateTime(timezone=True)。"""
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """写入：aware 值换算到 UTC；不支持时区的方言（MySQL/SQLite）再去掉 tzinfo 以 naive UTC 存储。"""
        if value is None:
            return None
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        if dialect.name in ("mysql", "sqlite"):
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """读出：naive 值按 UTC 解释并补上时区；aware 值统一换算到 UTC。"""
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AgentVersion(Base):
    """Published snapshot of an agent. Pins model alias, skill versions and the eval gate reference.
    （Agent 的已发布快照：固定模型别名、技能版本与评测闸门引用，保证同一版本的行为可复现。）
    """

    __tablename__ = "t_agent_version"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)  # Agent 标识
    version: Mapped[str] = mapped_column(String(32))  # 版本号
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")  # 租户标识
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | published | retired（版本状态）
    model_alias: Mapped[str] = mapped_column(String(64), default="default-chat")  # 绑定的模型别名
    harness: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 指定 harness，缺省用节点默认
    system_prompt: Mapped[str] = mapped_column(Text, default="")  # 系统提示词
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # tools, skills, budgets, redlines（工具/技能/预算/红线配置）
    # 发布快照的可复现字段：发布时一并冻结，保证"同一版本 = 同一套 harness/模型/技能/工具/检索配置"。
    harness_version: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 发布时所选 harness 的版本
    model_resolved: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 模型别名解析后的真实模型标识
    skills_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 全部技能 SKILL.md 内容的总摘要
    tools_schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 工具 schema 版本
    retrieval_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # 检索（RAG）配置快照
    eval_run_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 评测运行引用
    eval_passed: Mapped[bool | None] = mapped_column(nullable=True)  # 评测是否通过
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)  # 创建时间
    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_version"),)


class Run(Base):
    """单次 Run 的元信息表（当前状态快照，详细历史见 RunEventRow）。"""

    __tablename__ = "t_run"
    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)  # Run 标识
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")  # 租户标识
    agent_id: Mapped[str] = mapped_column(String(64), index=True)  # Agent 标识
    agent_version: Mapped[str] = mapped_column(String(32))  # Agent 版本
    model_alias: Mapped[str] = mapped_column(String(64))  # 模型别名
    subject: Mapped[str] = mapped_column(String(128), index=True)  # 发起主体
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)  # 关联会话标识
    state: Mapped[str] = mapped_column(String(20), default="submitted", index=True)  # 当前 RunState 值
    last_seq: Mapped[int] = mapped_column(Integer, default=0)  # 事件日志中最新的 seq（用于计算下一个 seq）
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 可观测性追踪 ID
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)  # 创建时间
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # 结束时间（终态时写入）


class RunEventRow(Base):
    """Append-only event log: the source of truth for a Run.
    （追加写事件日志表：一个 Run 的唯一事实来源，Run 的当前状态与消息历史都可从此表重建。）
    """

    __tablename__ = "t_run_event"
    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)  # 所属 Run 标识
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)  # 同一 run 内严格递增的序号（联合主键的一部分）
    type: Mapped[str] = mapped_column(String(40))  # 事件类型（RunEventType 的字符串值）
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 事件负载
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)  # 幂等键，用于去重判断
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)  # 事件产生时间
    __table_args__ = (Index("ix_run_event_idem", "idempotency_key"),)  # 加速按幂等键查重


class Audit(Base):
    """统一审计表（S2.3）：每次 API 请求、每次工具闸口判定、每次管理写操作各一行，
    回答"谁（actor）、何时（ts）、以何身份（act / auth_method / tenant_id）、调了什么（action / object）、
    判定结果（result）"。"""

    __tablename__ = "t_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)  # 操作时间
    actor: Mapped[str] = mapped_column(String(128), index=True)  # 操作者身份（Principal.subject；未认证为 anonymous）
    act: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 代为执行的 Agent / 应用（act 声明或签发客户端）
    auth_method: Mapped[str | None] = mapped_column(String(24), nullable=True)  # dev | user | client_credentials | none
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 租户
    action: Mapped[str] = mapped_column(String(64), index=True)  # 操作类型
    object: Mapped[str] = mapped_column(String(128))  # 操作对象
    result: Mapped[str] = mapped_column(String(16))  # 操作结果
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 关联追踪 ID
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 附加详情


def normalize_database_url(url: str) -> URL:
    """规范化连接串：MySQL 未显式指定 charset 时追加 charset=utf8mb4，保证中文与 emoji 不被截断/乱码。"""
    u = make_url(url)
    if u.get_backend_name() == "mysql" and "charset" not in u.query:
        u = u.update_query_dict({"charset": "utf8mb4"})
    return u


def make_engine(url: str) -> AsyncEngine:
    """按连接串创建异步引擎。MySQL 额外开启 pool_pre_ping 与 pool_recycle=3600，
    避免连接池拿到被服务端 wait_timeout 断开的陈旧连接。供本模块与 Alembic env.py 共用。"""
    u = normalize_database_url(url)
    kwargs: dict[str, Any] = {}
    if u.get_backend_name() == "mysql":
        kwargs.update(pool_pre_ping=True, pool_recycle=3600)
    return create_async_engine(u, **kwargs)


engine = make_engine(settings.database_url)  # 异步数据库引擎，连接串来自配置
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)  # 会话工厂


def alembic_config() -> Config:
    """构造指向 services/runtime/migrations 的 Alembic 配置（不读 alembic.ini，避免程序化调用时
    env.py 用 ini 里的 logging 配置覆盖应用自身的日志设置）。"""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


# Phase 0 用 Base.metadata.create_all 建出的四张表，与初始迁移 0001 的表结构一致。
LEGACY_TABLES = frozenset({"t_agent_version", "t_run", "t_run_event", "t_audit"})
BASELINE_REVISION = "0001"  # 旧库接入迁移时打标的基线版本（初始迁移）


def _upgrade_head(connection: Connection) -> None:
    """在给定的同步连接上执行 alembic upgrade head（env.py 检测到 attributes["connection"] 后直接复用它）。

    兼容 Phase 0 旧库：库里已有 create_all 建出的四张表却没有 alembic_version 时，直接 upgrade 会因
    "表已存在"失败。这里在同一连接上检查：四张表齐全 → 先 stamp 到基线 0001 再 upgrade；只有部分表 →
    无法判断该从哪个版本接入，抛出 RuntimeError 交由人工处理，绝不自动删表或补表。
    """
    cfg = alembic_config()
    cfg.attributes["connection"] = connection
    tables = set(inspect(connection).get_table_names())
    if "alembic_version" not in tables:
        present = LEGACY_TABLES & tables
        if present == LEGACY_TABLES:
            command.stamp(cfg, BASELINE_REVISION)
        elif present:
            missing = sorted(LEGACY_TABLES - present)
            raise RuntimeError(
                f"数据库中只有部分 Runtime 表 {sorted(present)}（缺少 {missing}）且没有 alembic_version，"
                "无法确定迁移基线：请人工核对后补齐表并执行 alembic stamp，或清空这些表后重启"
            )
    command.upgrade(cfg, "head")


async def init_db() -> None:
    """启动时把数据库 schema 升级到最新迁移版本（alembic upgrade head）。

    schema 只有 Alembic 迁移这一个来源，不再使用 create_all。迁移通过 run_sync 在引擎自己的连接上
    同步执行：env.py 复用传入的连接，不会再起新的事件循环，因此可安全地在 FastAPI lifespan 中调用。
    """
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade_head)
