"""Fixture-only synthetic datasets sharing the Runtime authoritative subject ledger.

No production route, migration, model execution or publication qualification.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from typing_extensions import TypedDict

from runtime.api import auth, workbench
from runtime.api.auth import Principal, current_principal
from runtime.config import settings
from runtime.state import db

router = APIRouter(prefix="/v1/isolated-datasets")
ENABLED = object()
CATALOG_REVISION = "synthetic-catalog-1"
CATALOG = {
    "ticket-summary-readonly": {"success", "failure"},
    "sales-summary-readonly": {"success", "empty", "failure"},
    "order-metrics-readonly": {"success", "incomplete", "empty"},
}


class ProposalData(TypedDict):
    schema_version: Literal[1]
    catalog_revision: Literal["synthetic-catalog-1"]
    scenario_id: str
    case_ids: list[str]


class CommandData(TypedDict):
    attempt_id: str
    action: Literal["draft", "edit", "freeze"]
    agent_id: str
    version: str
    registration_id: str | None
    expected_revision: int
    proposal: ProposalData | None


class RegistrationData(TypedDict):
    id: str
    tenant: str
    agent_id: str
    version: str
    maintainer: str
    state: Literal["draft", "frozen"]
    revision: int
    proposal: ProposalData


class OperationEvidence(TypedDict):
    attempt_id: str
    scope_id: str
    fingerprint: str
    subject: str
    tenant: str
    action: Literal["draft", "edit", "freeze"]
    registration_id: str
    revision: int
    snapshot: RegistrationData


class Base(DeclarativeBase):
    pass


class Registration(Base):
    __tablename__ = "isolated_dataset_registration"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    agent: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(64))
    body: Mapped[RegistrationData] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("tenant", "agent", "version"),)


class Operation(Base):
    __tablename__ = "isolated_dataset_operation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command: Mapped[CommandData] = mapped_column(JSON)
    evidence: Mapped[OperationEvidence | None] = mapped_column(JSON, nullable=True)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    catalog_revision: Literal["synthetic-catalog-1"]
    scenario_id: str
    case_ids: list[str] = Field(min_length=1, max_length=3)

    @model_validator(mode="before")
    @classmethod
    def strict_schema(cls, value: object) -> object:
        if isinstance(value, dict) and type(value.get("schema_version", 1)) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.scenario_id not in CATALOG or len(set(self.case_ids)) != len(self.case_ids):
            raise ValueError("unknown or duplicate synthetic case")
        if not set(self.case_ids) <= CATALOG[self.scenario_id]:
            raise ValueError("unknown synthetic case")
        return self


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    action: Literal["draft", "edit", "freeze"]
    agent_id: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    registration_id: str | None = Field(default=None, pattern=r"^dataset_[a-f0-9]{20}$")
    expected_revision: int = Field(ge=0, strict=True)
    proposal: Proposal | None = None


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(ge=1)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def enabled(request: Request) -> None:
    if (
        getattr(request.app.state, "isolated_datasets", None) is not ENABLED
        or settings.runtime_env != "dev"
        or db.engine.url.get_backend_name() != "sqlite"
    ):
        raise HTTPException(503, "isolated registration disabled")


async def authorize(request: Request, principal: Principal, agent_id: str) -> None:
    workbench.user_scope(principal)
    bindings = getattr(request.app.state, "isolated_dataset_bindings", {})
    user = bindings.get((auth.settings.jwt_issuer, principal.tenant_id, principal.subject))
    if not isinstance(user, str) or not user:
        raise HTTPException(403, "exact test identity binding required")
    adapter = getattr(request.app.state, "isolated_dataset_authorize", None)
    if adapter is None:
        raise HTTPException(503, "isolated identity adapter unavailable")
    # Only the fixture injects this server-side adapter; never accept browser role headers.
    await adapter(principal.tenant_id, user, agent_id)


def result(row: db.WorkbenchAttempt, op: Operation) -> dict[str, object]:
    return {
        "scope_id": row.scope_id,
        "attempt_id": row.attempt_id,
        "fingerprint": row.fingerprint,
        "status": row.status,
        "revision": row.revision,
        "acknowledged": row.acknowledged,
        "command": {k: v for k, v in op.command.items() if k != "proposal"},
        "evidence": {k: v for k, v in op.evidence.items() if k != "snapshot"} if op.evidence else None,
        "rejection": row.rejection,
    }


async def registration(session: AsyncSession, identifier: str | None, tenant: str) -> Registration:
    row = await session.get(Registration, identifier, populate_existing=True)
    if row is None or row.tenant != tenant:
        raise HTTPException(404, "registration not found")
    return row


async def validate(session: AsyncSession, command: CommandData, principal: Principal, request: Request) -> Registration | None:
    await authorize(request, principal, command["agent_id"])
    proposal = command["proposal"]
    if command["action"] == "draft":
        if command["registration_id"] is not None or command["expected_revision"] != 0 or proposal is None:
            raise HTTPException(422, "draft binding invalid")
        exists = await session.scalar(
            select(Registration.id).where(
                Registration.tenant == principal.tenant_id,
                Registration.agent == command["agent_id"],
                Registration.version == command["version"],
            )
        )
        if exists:
            raise HTTPException(409, "version exists")
        return None
    row = await registration(session, command["registration_id"], principal.tenant_id)
    if row.agent != command["agent_id"] or row.version != command["version"]:
        raise HTTPException(404, "dataset not found")
    if row.body["maintainer"] != principal.subject:
        raise HTTPException(403, "original creator required")
    if row.body["revision"] != command["expected_revision"] or row.body["state"] != "draft":
        raise HTTPException(409, "dataset revision or state changed")
    if (command["action"] == "edit") != (proposal is not None):
        raise HTTPException(422, "proposal binding invalid")
    return row


@router.get("")
async def listing(request: Request, agent_id: str, principal: Principal = Depends(current_principal)) -> dict[str, object]:
    enabled(request)
    await authorize(request, principal, agent_id)
    async with db.SessionLocal() as session:
        rows = list(
            await session.scalars(
                select(Registration)
                .where(Registration.tenant == principal.tenant_id, Registration.agent == agent_id)
                .order_by(Registration.version)
                .limit(201)
            )
        )
        if len(rows) > 200:
            raise HTTPException(409, "isolated list limit exceeded")
        return {"identity": workbench.binding(principal), "items": [r.body for r in rows]}


@router.get("/ledger")
async def ledger(request: Request, principal: Principal = Depends(current_principal)) -> dict[str, object]:
    enabled(request)
    scope_id = workbench.user_scope(principal)
    async with db.SessionLocal() as session:
        scope = await session.get(db.WorkbenchScope, scope_id)
        row = await workbench.owned(session, scope.active_attempt, scope_id) if scope and scope.active_attempt else None
        op = await session.get(Operation, row.attempt_id) if row and row.action == "dataset_write" else None
        return {
            "scope_id": scope_id,
            "blocked_elsewhere": row is not None and op is None,
            "active": result(row, op) if row is not None and op is not None else None,
        }


@router.post("/prepare")
async def prepare(body: Command, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, object]:
    enabled(request)
    scope_id = workbench.user_scope(principal)
    command: CommandData = {
        "attempt_id": str(body.attempt_id),
        "action": body.action,
        "agent_id": body.agent_id,
        "version": body.version,
        "registration_id": body.registration_id,
        "expected_revision": body.expected_revision,
        "proposal": {
            "schema_version": body.proposal.schema_version,
            "catalog_revision": body.proposal.catalog_revision,
            "scenario_id": body.proposal.scenario_id,
            "case_ids": body.proposal.case_ids.copy(),
        }
        if body.proposal
        else None,
    }
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        previous = await session.get(db.WorkbenchAttempt, str(body.attempt_id))
        if previous:
            row = await workbench.owned(session, previous.attempt_id, scope_id)
            op = await session.get(Operation, row.attempt_id)
            if row.action != "dataset_write" or op is None or op.command != command:
                raise HTTPException(409, "attempt binding mismatch")
            await authorize(request, principal, command["agent_id"])
            return result(row, op)
        if scope.active_attempt:
            raise HTTPException(409, "WORKBENCH_LOCKED")
        await validate(session, command, principal, request)
        identifier = body.registration_id or "dataset_" + uuid4().hex[:20]
        row = db.WorkbenchAttempt(
            attempt_id=str(body.attempt_id),
            scope_id=scope_id,
            action="dataset_write",
            run_id=identifier,
            request_seq=body.expected_revision,
            fingerprint=digest({"scope": scope_id, "command": command, "registration": identifier}),
            status="PREPARED",
            revision=1,
            acknowledged=False,
        )
        op = Operation(id=row.attempt_id, command=command)
        session.add_all([row, op])
        scope.active_attempt = row.attempt_id
        session.add(
            db.Audit(
                actor=principal.subject,
                tenant_id=principal.tenant_id,
                auth_method=principal.auth_method,
                action="dataset.prepare",
                object=f"attempt:{row.attempt_id}",
                result="PREPARED",
                detail={"fingerprint": row.fingerprint},
            )
        )
        await session.commit()
        return result(row, op)


@router.post("/{attempt_id}/execute")
async def execute(
    attempt_id: UUID, body: Evidence, request: Request, principal: Principal = Depends(current_principal)
) -> dict[str, object]:
    enabled(request)
    scope_id = workbench.user_scope(principal)
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        row = await workbench.owned(session, str(attempt_id), scope_id)
        op = await session.get(Operation, row.attempt_id)
        if row.action != "dataset_write" or op is None or body.fingerprint != row.fingerprint:
            raise HTTPException(409, "attempt mismatch")
        if row.status in ("COMMITTED", "REJECTED_NO_EFFECT"):
            await authorize(request, principal, op.command["agent_id"])
            return result(row, op)
        if scope.active_attempt != row.attempt_id or row.status != "PREPARED" or row.revision != body.revision:
            raise HTTPException(409, "attempt revision changed")
        try:
            async with session.begin_nested():
                target = await validate(session, op.command, principal, request)
                if target is None:
                    proposal = op.command["proposal"]
                    if proposal is None:
                        raise HTTPException(422, "draft proposal unavailable")
                    value: RegistrationData = {
                        "id": row.run_id,
                        "tenant": principal.tenant_id,
                        "agent_id": op.command["agent_id"],
                        "version": op.command["version"],
                        "maintainer": principal.subject,
                        "state": "draft",
                        "revision": 1,
                        "proposal": proposal,
                    }
                    target = Registration(
                        id=row.run_id, tenant=principal.tenant_id, agent=op.command["agent_id"], version=op.command["version"], body=value
                    )
                    session.add(target)
                else:
                    value = target.body.copy()
                    value["revision"] += 1
                    if op.command["action"] == "edit":
                        proposal = op.command["proposal"]
                        if proposal is None:
                            raise HTTPException(422, "edit proposal unavailable")
                        value["proposal"] = proposal
                    else:
                        value["state"] = "frozen"
                    target.body = value
                await session.flush()
                op.evidence = {
                    "attempt_id": row.attempt_id,
                    "scope_id": scope_id,
                    "fingerprint": row.fingerprint,
                    "subject": principal.subject,
                    "tenant": principal.tenant_id,
                    "action": op.command["action"],
                    "registration_id": target.id,
                    "revision": value["revision"],
                    "snapshot": value,
                }
                row.status = "COMMITTED"
                row.evidence_seq = value["revision"]
        except HTTPException:
            row.status = "REJECTED_NO_EFFECT"
            row.rejection = "VALIDATION_REJECTED"
        row.revision += 1
        session.add(
            db.Audit(
                actor=principal.subject,
                tenant_id=principal.tenant_id,
                auth_method=principal.auth_method,
                action="dataset.execute",
                object=f"attempt:{row.attempt_id}",
                result=row.status,
                detail={"fingerprint": row.fingerprint, "revision": row.revision, "action": op.command["action"]},
            )
        )
        await session.commit()
        return result(row, op)


@router.post("/{attempt_id}/acknowledge")
async def acknowledge(
    attempt_id: UUID, body: Evidence, request: Request, principal: Principal = Depends(current_principal)
) -> dict[str, object]:
    enabled(request)
    scope_id = workbench.user_scope(principal)
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        row = await workbench.owned(session, str(attempt_id), scope_id)
        op = await session.get(Operation, row.attempt_id)
        if row.action != "dataset_write" or op is None or row.fingerprint != body.fingerprint or row.revision != body.revision:
            raise HTTPException(409, "receipt mismatch")
        if row.acknowledged:
            return result(row, op)
        if scope.active_attempt != row.attempt_id:
            raise HTTPException(409, "scope mismatch")
        evidence = op.evidence
        if row.status == "COMMITTED":
            expected = {
                "attempt_id": row.attempt_id,
                "scope_id": scope_id,
                "fingerprint": row.fingerprint,
                "subject": principal.subject,
                "tenant": principal.tenant_id,
                "action": op.command["action"],
                "registration_id": row.run_id,
                "revision": row.evidence_seq,
            }
            if evidence is None or any(evidence.get(k) != v for k, v in expected.items()):
                raise HTTPException(409, "terminal evidence unavailable")
        elif row.status != "REJECTED_NO_EFFECT" or row.rejection != "VALIDATION_REJECTED":
            raise HTTPException(409, "terminal receipt required")
        row.acknowledged = True
        row.revision += 1
        scope.active_attempt = None
        session.add(
            db.Audit(
                actor=principal.subject,
                tenant_id=principal.tenant_id,
                auth_method=principal.auth_method,
                action="dataset.acknowledge",
                object=f"attempt:{row.attempt_id}",
                result="ok",
                detail={"fingerprint": row.fingerprint, "revision": row.revision},
            )
        )
        await session.commit()
        return result(row, op)
