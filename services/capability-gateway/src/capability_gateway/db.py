"""能力目录的 MySQL 实现（``CATALOG_BACKEND=mysql``，D122：MySQL 权威 + 同事务 outbox）。

表：cg_capability / cg_agent_binding / cg_connector / cg_connector_tool / cg_outbox；schema 只来自网关自带的 Alembic
迁移（``services/capability-gateway/migrations``，version_table ``cg_alembic_version``，可与其他服务共用一个库）。
MySQL 下迁移包在 ``GET_LOCK('cg_migrate')`` 里，多副本并发启动只迁移一次。也兼容 SQLite（aiosqlite，测试用）。
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    select,
    text,
    update,
)
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from capability_gateway.catalog import Catalog, CatalogConflict, ConnectorTool, ConnectorVersion, OutboxEvent
from capability_gateway.registry import Binding, Capability

VERSION_TABLE = "cg_alembic_version"
MIGRATE_LOCK = "cg_migrate"
# 迁移目录：源码树中位于 services/capability-gateway/migrations；镜像内由 GATEWAY_MIGRATIONS_DIR 指向拷贝位置
MIGRATIONS_DIR = Path(os.environ.get("GATEWAY_MIGRATIONS_DIR") or Path(__file__).resolve().parents[2] / "migrations")
_ID = BigInteger().with_variant(Integer, "sqlite")  # SQLite 只有 INTEGER PRIMARY KEY 才自增


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # 存 naive UTC（MySQL DATETIME 不带时区）


class Base(DeclarativeBase):
    pass


class CapabilityRow(Base):
    __tablename__ = "cg_capability"
    __table_args__ = (UniqueConstraint("tenant", "name", name="uq_cg_capability_tenant_name"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default")
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(16), default="mcp_tool")
    description: Mapped[str] = mapped_column(Text)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    system: Mapped[str] = mapped_column(String(64), default="")
    owner: Mapped[str] = mapped_column(String(128), default="")
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    status: Mapped[str] = mapped_column(String(16), default="published", index=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="read")
    data_class: Mapped[str] = mapped_column(String(16), default="internal")
    supports_dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_idempotency_key: Mapped[bool] = mapped_column(Boolean, default=False)
    compensate_tool: Mapped[str | None] = mapped_column(String(128), nullable=True)
    connector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class BindingRow(Base):
    __tablename__ = "cg_agent_binding"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    capability_id: Mapped[str] = mapped_column(String(128), primary_key=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)  # core | discoverable
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ConnectorRow(Base):
    __tablename__ = "cg_connector"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(512))
    protocol: Mapped[str] = mapped_column(String(16), default="rest")
    auth_type: Mapped[str] = mapped_column(String(16), default="inherit")
    system: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="inactive")
    health: Mapped[str] = mapped_column(String(16), default="unknown")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activated_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_probe_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ConnectorToolRow(Base):
    __tablename__ = "cg_connector_tool"

    connector: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    errors: Mapped[list[str]] = mapped_column(JSON)


class OutboxRow(Base):
    __tablename__ = "cg_outbox"
    __table_args__ = (Index("ix_cg_outbox_created", "created_at"),)

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    aggregate: Mapped[str] = mapped_column(String(32), default="capability")
    aggregate_id: Mapped[str] = mapped_column(String(128))
    op: Mapped[str] = mapped_column(String(16), default="upsert")
    created_at: Mapped[float] = mapped_column(Float)


_CAP_FIELDS = tuple(f for f in Capability.model_fields if f != "row_version")


def _cap(row: CapabilityRow) -> Capability:
    return Capability.model_validate({f: getattr(row, f) for f in Capability.model_fields})


def _cv(row: ConnectorRow) -> ConnectorVersion:
    return ConnectorVersion.model_validate({f: getattr(row, f) for f in ConnectorVersion.model_fields})


def normalize_database_url(url: str) -> URL:
    """MySQL 未显式指定 charset 时追加 utf8mb4。"""
    u = make_url(url)
    if u.get_backend_name() == "mysql" and "charset" not in u.query:
        u = u.update_query_dict({"charset": "utf8mb4"})
    return u


def make_engine(url: str) -> AsyncEngine:
    u = normalize_database_url(url)
    kwargs: dict[str, Any] = {}
    if u.get_backend_name() == "mysql":
        kwargs.update(pool_pre_ping=True, pool_recycle=3600)
    return create_async_engine(u, **kwargs)


def alembic_config() -> Config:
    """指向网关迁移目录的 Alembic 配置（不读 alembic.ini，避免覆盖应用日志设置）。"""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def _upgrade_head(connection: Connection) -> None:
    cfg = alembic_config()
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


async def migrate(engine: AsyncEngine, lock_timeout_s: int = 60) -> None:
    """alembic upgrade head；MySQL 下以 GET_LOCK 串行化多副本。"""
    async with engine.connect() as conn:
        locked = conn.dialect.name == "mysql"
        if locked:
            got = (await conn.execute(text("SELECT GET_LOCK(:n, :t)"), {"n": MIGRATE_LOCK, "t": lock_timeout_s})).scalar()
            if got != 1:
                raise RuntimeError(f"等待迁移锁 {MIGRATE_LOCK} 超时或失败：{got!r}")
        try:
            await conn.run_sync(_upgrade_head)
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            if locked:
                await conn.execute(text("SELECT RELEASE_LOCK(:n)"), {"n": MIGRATE_LOCK})
                await conn.commit()


class SqlCatalog(Catalog):
    """SQLAlchemy（async）实现；能力 / 绑定变更与 outbox 在同一事务内提交。"""

    def __init__(self, url: str, *, migrate_on_init: bool = True) -> None:
        self.engine = make_engine(url)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.migrate_on_init = migrate_on_init

    async def init(self) -> None:
        if self.migrate_on_init:
            await migrate(self.engine)

    async def close(self) -> None:
        await self.engine.dispose()

    @staticmethod
    def _outbox(s: AsyncSession, cid: str, op: str = "upsert") -> None:
        s.add(OutboxRow(aggregate="capability", aggregate_id=cid, op=op, created_at=time.time()))

    # ---- 能力 ----
    async def get(self, cid: str) -> Capability | None:
        async with self.session() as s:
            row = await s.get(CapabilityRow, cid)
            return _cap(row) if row else None

    async def get_many(self, ids: list[str]) -> dict[str, Capability]:
        if not ids:
            return {}
        async with self.session() as s:
            rows = (await s.execute(select(CapabilityRow).where(CapabilityRow.id.in_(ids)))).scalars().all()
            return {r.id: _cap(r) for r in rows}

    async def list_all(self) -> list[Capability]:
        async with self.session() as s:
            rows = (await s.execute(select(CapabilityRow).order_by(CapabilityRow.id))).scalars().all()
            return [_cap(r) for r in rows]

    async def by_name(self, name: str, tenant: str | None = None) -> Capability | None:
        q = select(CapabilityRow).where(CapabilityRow.name == name, CapabilityRow.status == "published")
        if tenant is not None:
            q = q.where(CapabilityRow.tenant == tenant)
        async with self.session() as s:
            row = (await s.execute(q.order_by(CapabilityRow.id).limit(1))).scalars().first()
            return _cap(row) if row else None

    async def upsert(self, cap: Capability) -> Capability:
        try:
            async with self.session.begin() as s:
                row = await s.get(CapabilityRow, cap.id, with_for_update=True)
                values = {f: getattr(cap, f) for f in _CAP_FIELDS}
                if row is None:
                    row = CapabilityRow(**values, row_version=1)
                    s.add(row)
                else:
                    for k, v in values.items():
                        setattr(row, k, v)
                    row.row_version += 1
                self._outbox(s, cap.id)
                await s.flush()
                return _cap(row)
        except IntegrityError as e:
            raise CatalogConflict(f"capability name '{cap.name}' already used in tenant {cap.tenant}") from e

    # ---- 绑定 ----
    async def bindings(self, *, agent_id: str | None = None, capability_id: str | None = None) -> list[Binding]:
        q = select(BindingRow)
        if agent_id is not None:
            q = q.where(BindingRow.agent_id == agent_id)
        if capability_id is not None:
            q = q.where(BindingRow.capability_id == capability_id)
        async with self.session() as s:
            rows = (await s.execute(q.order_by(BindingRow.agent_id, BindingRow.capability_id, BindingRow.kind))).scalars().all()
            return [Binding(agent_id=r.agent_id, capability_id=r.capability_id, kind=r.kind) for r in rows]  # type: ignore[arg-type]

    async def _bump(self, s: AsyncSession, cid: str) -> None:
        await s.execute(update(CapabilityRow).where(CapabilityRow.id == cid).values(row_version=CapabilityRow.row_version + 1))
        self._outbox(s, cid)

    async def put_binding(self, b: Binding) -> None:
        async with self.session.begin() as s:
            if await s.get(CapabilityRow, b.capability_id) is None:
                raise KeyError(b.capability_id)
            if await s.get(BindingRow, (b.agent_id, b.capability_id, b.kind)) is not None:
                return
            s.add(BindingRow(agent_id=b.agent_id, capability_id=b.capability_id, kind=b.kind))
            await self._bump(s, b.capability_id)

    async def delete_binding(self, b: Binding) -> bool:
        async with self.session.begin() as s:
            row = await s.get(BindingRow, (b.agent_id, b.capability_id, b.kind))
            if row is None:
                return False
            await s.delete(row)
            await self._bump(s, b.capability_id)
            return True

    # ---- outbox ----
    async def outbox_fetch(self, limit: int = 100) -> list[OutboxEvent]:
        async with self.session() as s:
            rows = (await s.execute(select(OutboxRow).order_by(OutboxRow.id).limit(limit))).scalars().all()
            return [
                OutboxEvent(id=r.id, aggregate=r.aggregate, aggregate_id=r.aggregate_id, op=r.op, created_at=r.created_at)  # type: ignore[arg-type]
                for r in rows
            ]

    async def outbox_ack(self, ids: list[int]) -> None:
        if not ids:
            return
        async with self.session.begin() as s:
            await s.execute(delete(OutboxRow).where(OutboxRow.id.in_(ids)))

    # ---- 连接器 ----
    async def put_connector(self, cv: ConnectorVersion) -> ConnectorVersion:
        async with self.session.begin() as s:
            row = await s.get(ConnectorRow, (cv.name, cv.version))
            if row is None:
                row = ConnectorRow(**cv.model_dump())
                s.add(row)
            else:
                row.endpoint, row.protocol, row.auth_type, row.system = cv.endpoint, cv.protocol, cv.auth_type, cv.system
            await s.flush()
            return _cv(row)

    async def list_connectors(self, name: str | None = None) -> list[ConnectorVersion]:
        q = select(ConnectorRow)
        if name is not None:
            q = q.where(ConnectorRow.name == name)
        async with self.session() as s:
            rows = (await s.execute(q.order_by(ConnectorRow.name, ConnectorRow.version))).scalars().all()
            return [_cv(r) for r in rows]

    async def activate(self, name: str, version: str) -> ConnectorVersion:
        async with self.session.begin() as s:
            target = await s.get(ConnectorRow, (name, version))
            if target is None:
                raise KeyError(f"{name}@{version}")
            await s.execute(update(ConnectorRow).where(ConnectorRow.name == name).values(status="inactive"))
            target.status = "active"
            target.activated_manifest_hash = target.manifest_hash
            await s.flush()
            await s.refresh(target)
            return _cv(target)

    async def save_probe(self, cv: ConnectorVersion, tools: list[ConnectorTool] | None) -> None:
        async with self.session.begin() as s:
            row = await s.get(ConnectorRow, (cv.name, cv.version))
            if row is None:
                return
            row.health, row.consecutive_failures = cv.health, cv.consecutive_failures
            row.manifest_hash, row.last_probe_at = cv.manifest_hash, cv.last_probe_at
            row.activated_manifest_hash = cv.activated_manifest_hash
            row.last_error = cv.last_error[:512] if cv.last_error else None
            if tools is not None:
                await s.execute(
                    delete(ConnectorToolRow).where(ConnectorToolRow.connector == cv.name, ConnectorToolRow.version == cv.version)
                )
                for t in tools:
                    s.add(
                        ConnectorToolRow(
                            connector=t.connector, version=t.version, name=t.name, spec=t.spec, read_only=t.read_only, errors=t.errors
                        )
                    )

    async def connector_tools(self, name: str, version: str) -> list[ConnectorTool]:
        q = select(ConnectorToolRow).where(ConnectorToolRow.connector == name, ConnectorToolRow.version == version)
        async with self.session() as s:
            rows = (await s.execute(q.order_by(ConnectorToolRow.name))).scalars().all()
            return [
                ConnectorTool(connector=r.connector, version=r.version, name=r.name, spec=r.spec, read_only=r.read_only, errors=r.errors)
                for r in rows
            ]
