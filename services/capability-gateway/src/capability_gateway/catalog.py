"""能力目录存储接口（D122）：能力、Agent 绑定、连接器（含版本与 manifest 工具）、outbox。

两个实现：``MemoryCatalog``（``CATALOG_BACKEND=memory``，单测 / 本机）与 ``db.SqlCatalog``（MySQL，权威）。
能力或其绑定的每次变更都在同一事务内写一条 outbox（aggregate=capability），由 search.Indexer 异步同步到 ES；
能力的 row_version 随之 +1，ES 以 external_gte 版本写入，多个 indexer 并发或乱序也不会用旧数据覆盖新数据。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, Field

from capability_gateway.registry import SEED, SEED_BINDINGS, Binding, Capability, seed_connectors


class CatalogConflict(Exception):
    """违反唯一约束（同一租户内工具名重复）。"""


class ConnectorVersion(BaseModel):
    """连接器（MCP Server / REST 工具服务）的一个版本；同名连接器同一时刻只有一个 active 版本。"""

    name: str = Field(min_length=1, max_length=128)
    version: str = Field(default="1", min_length=1, max_length=32)
    endpoint: str = Field(max_length=512)
    protocol: Literal["rest", "mcp"] = "rest"  # rest：GET /tools + POST /tools/{name}；mcp：streamable-http JSON-RPC
    auth_type: Literal["inherit", "service"] = "inherit"  # inherit：转发 OBO 令牌
    system: str = Field(default="", max_length=64)
    status: Literal["inactive", "active"] = "inactive"
    health: Literal["unknown", "up", "down"] = "unknown"
    consecutive_failures: int = 0
    manifest_hash: str | None = None  # 最近一次探活成功时的 manifest 哈希
    # 激活时的 manifest 哈希：/invoke 要求最近一次 manifest 与之一致（漂移即拒绝，须重新激活）；
    # 未经 activate 端点激活的版本（种子、迁移前的存量）在首次探活成功时记录（TOFU）
    activated_manifest_hash: str | None = None
    last_probe_at: float | None = None  # epoch 秒
    last_error: str | None = None


class ConnectorTool(BaseModel):
    """探活时从连接器 manifest 读到的一个工具及其写规范校验结果。"""

    connector: str
    version: str
    name: str
    spec: dict[str, Any] = Field(default_factory=dict)
    read_only: bool = True
    errors: list[str] = Field(default_factory=list)  # validate_tool 的结果；非空即不合规、不可发布


class OutboxEvent(BaseModel):
    id: int
    aggregate: str = "capability"
    aggregate_id: str
    op: Literal["upsert", "delete"] = "upsert"
    created_at: float = 0.0


def normalize_protocol(protocol: str) -> Literal["rest", "mcp"]:
    """兼容旧登记值：http / rest → rest；streamable-http / mcp → mcp。"""
    return "mcp" if protocol in ("mcp", "streamable-http") else "rest"


class Catalog(ABC):
    """目录存储接口（全部为协程，便于 MySQL 实现）。"""

    async def init(self) -> None:  # noqa: B027 —— 默认无需初始化
        """启动时调用（MySQL：执行迁移）。"""

    async def close(self) -> None:  # noqa: B027
        """关闭连接池。"""

    # ---- 能力 ----
    @abstractmethod
    async def get(self, cid: str) -> Capability | None: ...

    @abstractmethod
    async def get_many(self, ids: list[str]) -> dict[str, Capability]: ...

    @abstractmethod
    async def list_all(self) -> list[Capability]: ...

    async def list_published(self) -> list[Capability]:
        return [c for c in await self.list_all() if c.status == "published"]

    async def by_name(self, name: str, tenant: str | None = None) -> Capability | None:
        """按工具名取一条已发布能力（给定租户时只在该租户内查找）。"""
        return next((c for c in await self.list_published() if c.name == name and (tenant is None or c.tenant == tenant)), None)

    @abstractmethod
    async def upsert(self, cap: Capability) -> Capability:
        """新增或覆盖（row_version 由存储维护），同事务写 outbox；同租户工具名冲突抛 CatalogConflict。"""

    # ---- 绑定 ----
    @abstractmethod
    async def bindings(self, *, agent_id: str | None = None, capability_id: str | None = None) -> list[Binding]: ...

    @abstractmethod
    async def put_binding(self, b: Binding) -> None:
        """新增绑定（已存在时无操作）；能力不存在抛 KeyError。能力 row_version +1 并写 outbox。"""

    @abstractmethod
    async def delete_binding(self, b: Binding) -> bool: ...

    async def bound_ids(self, agent_id: str, kind: str) -> set[str]:
        """某 Agent（含 "*" 通配）在给定 kind 下绑定的能力 id。"""
        out: set[str] = set()
        for a in {agent_id, "*"}:
            out |= {b.capability_id for b in await self.bindings(agent_id=a) if b.kind == kind}
        return out

    # ---- outbox ----
    @abstractmethod
    async def outbox_fetch(self, limit: int = 100) -> list[OutboxEvent]: ...

    @abstractmethod
    async def outbox_ack(self, ids: list[int]) -> None: ...

    # ---- 连接器 ----
    @abstractmethod
    async def put_connector(self, cv: ConnectorVersion) -> ConnectorVersion:
        """新增或更新一个连接器版本的登记信息（endpoint / protocol / system / auth_type）；状态与健康度保留原值。"""

    @abstractmethod
    async def list_connectors(self, name: str | None = None) -> list[ConnectorVersion]: ...

    async def get_connector(self, name: str, version: str) -> ConnectorVersion | None:
        return next((c for c in await self.list_connectors(name) if c.version == version), None)

    async def active_connector(self, name: str) -> ConnectorVersion | None:
        return next((c for c in await self.list_connectors(name) if c.status == "active"), None)

    async def connector_for(self, cap: Capability) -> ConnectorVersion | None:
        """能力的承载连接器：优先按 cap.connector 名，未指定时按业务系统匹配 active 版本。"""
        if cap.connector:
            return await self.active_connector(cap.connector)
        return next((c for c in await self.list_connectors() if c.status == "active" and c.system == cap.system), None)

    @abstractmethod
    async def activate(self, name: str, version: str) -> ConnectorVersion:
        """切换 active 版本，并以该版本当前的 manifest 哈希作为激活基线（activated_manifest_hash）。"""

    @abstractmethod
    async def save_probe(self, cv: ConnectorVersion, tools: list[ConnectorTool] | None) -> None:
        """写回探活结果（health / 失败计数 / manifest 哈希 / 激活基线哈希 / 时间 / 错误）；tools 不为 None 时整体替换该版本的工具表。"""

    @abstractmethod
    async def connector_tools(self, name: str, version: str) -> list[ConnectorTool]: ...


class MemoryCatalog(Catalog):
    """内存实现（单测与本机开发）；默认装入开发种子。"""

    def __init__(self, items: list[Capability] | None = None, bindings: list[Binding] | None = None, *, seed: bool = True) -> None:
        self._caps: dict[str, Capability] = {}
        self._bindings: set[tuple[str, str, str]] = set()
        self._outbox: deque[OutboxEvent] = deque(maxlen=10000)
        self._seq = 0
        self._connectors: dict[tuple[str, str], ConnectorVersion] = {}
        self._tools: dict[tuple[str, str], list[ConnectorTool]] = {}
        for c in items if items is not None else (SEED if seed else []):
            self._store(c)
        for b in bindings if bindings is not None else (SEED_BINDINGS if seed else []):
            if b.capability_id in self._caps:
                self._bindings.add((b.agent_id, b.capability_id, b.kind))
        if seed:
            for s in seed_connectors():
                cv = ConnectorVersion(**s, status="active")
                self._connectors[(cv.name, cv.version)] = cv

    def _emit(self, cid: str, op: Literal["upsert", "delete"] = "upsert") -> None:
        self._seq += 1
        self._outbox.append(OutboxEvent(id=self._seq, aggregate_id=cid, op=op, created_at=time.time()))

    def _store(self, cap: Capability) -> Capability:
        for other in self._caps.values():
            if other.id != cap.id and other.tenant == cap.tenant and other.name == cap.name:
                raise CatalogConflict(f"capability name '{cap.name}' already used by {other.id} in tenant {cap.tenant}")
        old = self._caps.get(cap.id)
        stored = cap.model_copy(update={"row_version": (old.row_version if old else 0) + 1})
        self._caps[cap.id] = stored
        self._emit(cap.id)
        return stored

    async def get(self, cid: str) -> Capability | None:
        return self._caps.get(cid)

    async def get_many(self, ids: list[str]) -> dict[str, Capability]:
        return {i: self._caps[i] for i in ids if i in self._caps}

    async def list_all(self) -> list[Capability]:
        return list(self._caps.values())

    async def upsert(self, cap: Capability) -> Capability:
        return self._store(cap)

    async def bindings(self, *, agent_id: str | None = None, capability_id: str | None = None) -> list[Binding]:
        return [
            Binding(agent_id=a, capability_id=c, kind=k)  # type: ignore[arg-type]
            for a, c, k in sorted(self._bindings)
            if (agent_id is None or a == agent_id) and (capability_id is None or c == capability_id)
        ]

    def _bump(self, cid: str) -> None:
        cap = self._caps[cid]
        self._caps[cid] = cap.model_copy(update={"row_version": cap.row_version + 1})
        self._emit(cid)

    async def put_binding(self, b: Binding) -> None:
        if b.capability_id not in self._caps:
            raise KeyError(b.capability_id)
        key = (b.agent_id, b.capability_id, b.kind)
        if key not in self._bindings:
            self._bindings.add(key)
            self._bump(b.capability_id)

    async def delete_binding(self, b: Binding) -> bool:
        key = (b.agent_id, b.capability_id, b.kind)
        if key not in self._bindings:
            return False
        self._bindings.discard(key)
        if b.capability_id in self._caps:
            self._bump(b.capability_id)
        return True

    async def outbox_fetch(self, limit: int = 100) -> list[OutboxEvent]:
        return list(self._outbox)[:limit]

    async def outbox_ack(self, ids: list[int]) -> None:
        done = set(ids)
        self._outbox = deque((e for e in self._outbox if e.id not in done), maxlen=self._outbox.maxlen)

    async def put_connector(self, cv: ConnectorVersion) -> ConnectorVersion:
        old = self._connectors.get((cv.name, cv.version))
        if old is not None:
            cv = old.model_copy(update={"endpoint": cv.endpoint, "protocol": cv.protocol, "auth_type": cv.auth_type, "system": cv.system})
        self._connectors[(cv.name, cv.version)] = cv
        return cv

    async def list_connectors(self, name: str | None = None) -> list[ConnectorVersion]:
        return [c for (n, _), c in sorted(self._connectors.items()) if name is None or n == name]

    async def activate(self, name: str, version: str) -> ConnectorVersion:
        if (name, version) not in self._connectors:
            raise KeyError(f"{name}@{version}")
        for key, c in list(self._connectors.items()):
            if key[0] == name:
                active = key[1] == version
                update: dict[str, Any] = {"status": "active" if active else "inactive"}
                if active:
                    update["activated_manifest_hash"] = c.manifest_hash
                self._connectors[key] = c.model_copy(update=update)
        return self._connectors[(name, version)]

    async def save_probe(self, cv: ConnectorVersion, tools: list[ConnectorTool] | None) -> None:
        key = (cv.name, cv.version)
        if key not in self._connectors:
            return
        fields = ("health", "consecutive_failures", "manifest_hash", "activated_manifest_hash", "last_probe_at", "last_error")
        self._connectors[key] = self._connectors[key].model_copy(update={f: getattr(cv, f) for f in fields})
        if tools is not None:
            self._tools[key] = list(tools)

    async def connector_tools(self, name: str, version: str) -> list[ConnectorTool]:
        return list(self._tools.get((name, version), []))
