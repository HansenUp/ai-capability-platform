"""Skill 商店的持久化模型与仓储（cg_skill / cg_skill_version / cg_skill_event）。

Skill 名全局唯一并归属首次上传者的租户；版本 ``(name, version)`` 不可变（同名同版本再次上传 409）。
状态更新以 row_version 做比较并交换（并发更新 409）。内存实现用于单测与 CATALOG_BACKEND=memory，MySQL 实现见 db.SqlSkillRepo。
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["submitted", "scanned", "scan_failed", "evaluated", "eval_failed", "approved", "listed", "deprecated", "revoked"]
# 状态机（D140）：submitted→scanned→evaluated→approved→listed→deprecated/revoked；scan_failed、eval_failed、revoked 为终态
TRANSITIONS: dict[str, frozenset[str]] = {
    "submitted": frozenset({"scanned", "scan_failed"}),
    "scanned": frozenset({"evaluated", "eval_failed", "scan_failed", "revoked"}),
    "evaluated": frozenset({"approved", "scan_failed", "revoked"}),
    "approved": frozenset({"listed", "scan_failed", "revoked"}),
    "listed": frozenset({"deprecated", "revoked"}),
    "deprecated": frozenset({"revoked"}),
    "scan_failed": frozenset(),
    "eval_failed": frozenset(),
    "revoked": frozenset(),
}


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class SkillConflict(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SkillVersion(BaseModel):
    name: str
    version: str
    tenant: str
    status: Status
    manifest: dict[str, Any]  # 上架前为草稿（无 reviewed_by / signed_at / eval），listed 后为已签名清单
    manifest_sha256: str | None = None
    source_key: str
    source_sha256: str
    package_key: str | None = None
    package_sha256: str | None = None
    scan_report: dict[str, Any] | None = None
    rules_version: str | None = None
    eval_run_ref: str | None = None
    eval_pass_rate: float | None = None
    eval_items: int | None = None  # 评测条数
    submitted_by: str
    reviewed_by: str | None = None
    signature: dict[str, Any] | None = None
    kid: str | None = None
    listed_at: datetime | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None
    row_version: int = 1
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class SkillEvent(BaseModel):
    name: str
    version: str
    event: str  # submitted / scanned / scan_failed / evaluated / eval_failed / approved / listed / revoked / rescanned ...
    actor: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=now)


class SkillRepo(ABC):
    @abstractmethod
    async def create(self, rec: SkillVersion, event: SkillEvent) -> None:
        """登记新版本：名称已属其他租户 → SkillConflict(name_taken)；版本已存在 → SkillConflict(version_exists)。"""

    @abstractmethod
    async def get(self, name: str, version: str) -> SkillVersion | None: ...

    @abstractmethod
    async def save(self, rec: SkillVersion, events: list[SkillEvent]) -> SkillVersion:
        """按 rec.row_version 比较并交换后写入（row_version+1）；失败 SkillConflict(concurrent_update)。"""

    @abstractmethod
    async def list(self, *, statuses: list[str] | None = None, tenant: str | None = None) -> list[SkillVersion]: ...

    @abstractmethod
    async def events(self, name: str, version: str) -> builtins.list[SkillEvent]: ...


class MemorySkillRepo(SkillRepo):
    def __init__(self) -> None:
        self.owners: dict[str, str] = {}
        self.versions: dict[tuple[str, str], SkillVersion] = {}
        self.log: list[SkillEvent] = []

    async def create(self, rec: SkillVersion, event: SkillEvent) -> None:
        owner = self.owners.get(rec.name)
        if owner is not None and owner != rec.tenant:
            raise SkillConflict("name_taken", f"skill name '{rec.name}' belongs to another tenant")
        if (rec.name, rec.version) in self.versions:
            raise SkillConflict("version_exists", f"{rec.name}@{rec.version} already exists (versions are immutable)")
        self.owners[rec.name] = rec.tenant
        self.versions[(rec.name, rec.version)] = rec.model_copy(deep=True)
        self.log.append(event)

    async def get(self, name: str, version: str) -> SkillVersion | None:
        rec = self.versions.get((name, version))
        return rec.model_copy(deep=True) if rec else None

    async def save(self, rec: SkillVersion, events: list[SkillEvent]) -> SkillVersion:
        cur = self.versions.get((rec.name, rec.version))
        if cur is None or cur.row_version != rec.row_version:
            raise SkillConflict("concurrent_update", f"{rec.name}@{rec.version} was modified concurrently")
        new = rec.model_copy(deep=True, update={"row_version": rec.row_version + 1, "updated_at": now()})
        self.versions[(rec.name, rec.version)] = new
        self.log += events
        return new.model_copy(deep=True)

    async def list(self, *, statuses: list[str] | None = None, tenant: str | None = None) -> list[SkillVersion]:
        out = [
            r.model_copy(deep=True)
            for r in self.versions.values()
            if (statuses is None or r.status in statuses) and (tenant is None or r.tenant == tenant)
        ]
        return sorted(out, key=lambda r: (r.name, r.version))

    async def events(self, name: str, version: str) -> builtins.list[SkillEvent]:
        return [e for e in self.log if e.name == name and e.version == version]
