"""Engine database (MySQL in production, SQLite for local dev). Only configuration, runs, the Run
event log, usage and audit live here. Traces and evaluation data are external (Langfuse).
（引擎数据库（生产环境为 MySQL，本地开发为 SQLite）：仅存放配置（AgentVersion）、Run 元信息、
Run 事件日志、用量与审计数据；调用链追踪（trace）与评测数据存放在外部系统（Langfuse），
不在本库范围内。）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from runtime.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""

    pass


def _now() -> datetime:
    """返回当前 UTC 时间，用作各表时间字段的默认值工厂。"""
    return datetime.now(UTC)


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)  # 创建时间
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)  # 创建时间
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 结束时间（终态时写入）


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
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)  # 事件产生时间
    __table_args__ = (Index("ix_run_event_idem", "idempotency_key"),)  # 加速按幂等键查重


class Audit(Base):
    """管理类操作（如发布 Agent 版本）的审计记录表。"""

    __tablename__ = "t_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)  # 操作时间
    actor: Mapped[str] = mapped_column(String(128))  # 操作者身份
    action: Mapped[str] = mapped_column(String(64), index=True)  # 操作类型
    object: Mapped[str] = mapped_column(String(128))  # 操作对象
    result: Mapped[str] = mapped_column(String(16))  # 操作结果
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 关联追踪 ID
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 附加详情


engine = create_async_engine(settings.database_url, future=True)  # 异步数据库引擎，连接串来自配置
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)  # 会话工厂


async def init_db() -> None:
    """建表（若不存在）。开发/测试环境下用于快速起库；生产环境应使用正式迁移工具管理 schema。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
