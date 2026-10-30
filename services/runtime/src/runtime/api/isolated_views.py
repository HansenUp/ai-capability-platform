"""Fixture-injected registration control plane; shares authoritative workbench subject locks.

No production route, migration, real source, read_data grant or download issuance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, String, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from runtime.api import workbench
from runtime.api.auth import Principal, current_principal
from runtime.config import settings
from runtime.state import db

router = APIRouter(prefix="/v1/isolated-views")
ENABLED = object()
MAINTAINER = "data-view-maintainer"
REVIEWER = "data-view-reviewer"


class Base(DeclarativeBase):
    pass


class Registration(Base):
    __tablename__ = "isolated_view_registration"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("tenant", "version"),)


class Operation(Base):
    __tablename__ = "isolated_view_operation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    columns: list[Literal["item", "secret"]] = Field(min_length=1, max_length=2)
    filter_item: bool = True
    limit: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def valid(self):
        if self.version == "v1" or len(set(self.columns)) != len(self.columns) or (self.filter_item and "item" not in self.columns):
            raise ValueError("invalid synthetic proposal")
        return self


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    action: Literal["draft", "edit", "submit", "approve", "reject", "withdraw", "publish", "revoke"]
    registration_id: str | None = Field(default=None, pattern=r"^view_[a-f0-9]{20}$")
    expected_revision: int = Field(ge=0)
    proposal: Proposal | None = None


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(ge=1)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def enabled(request):
    if (
        getattr(request.app.state, "isolated_views", None) is not ENABLED
        or settings.runtime_env != "dev"
        or db.engine.url.get_backend_name() != "sqlite"
    ):
        raise HTTPException(503, "isolated registration disabled")


def manager(principal):
    workbench.user_scope(principal)
    if not principal.roles.intersection({MAINTAINER, REVIEWER}):
        raise HTTPException(403, "explicit data view role required")


def result(row, op):
    return {
        "scope_id": row.scope_id,
        "attempt_id": row.attempt_id,
        "fingerprint": row.fingerprint,
        "status": row.status,
        "revision": row.revision,
        "acknowledged": row.acknowledged,
        "command": op.command,
        "evidence": op.evidence,
        "rejection": row.rejection,
    }


async def registration(session, identifier, tenant):
    row = await session.get(Registration, identifier, populate_existing=True)
    if row is None or row.tenant != tenant:
        raise HTTPException(404, "registration not found")
    return row


async def validate(session, command, principal):
    manager(principal)
    action = command["action"]
    required = REVIEWER if action in ("approve", "reject") else MAINTAINER
    if required not in principal.roles:
        raise HTTPException(403, "action role required")
    proposal = command["proposal"]
    if action == "draft":
        if command["registration_id"] is not None or command["expected_revision"] != 0 or proposal is None:
            raise HTTPException(422, "draft binding invalid")
        if await session.scalar(
            select(Registration.id).where(Registration.tenant == principal.tenant_id, Registration.version == proposal["version"])
        ):
            raise HTTPException(409, "version exists")
        return None
    row = await registration(session, command["registration_id"], principal.tenant_id)
    value = row.body
    if value["revision"] != command["expected_revision"]:
        raise HTTPException(409, "stale registration revision")
    if action in ("approve", "reject"):
        if value["maintainer"] == principal.subject or value["state"] != "submitted" or principal.subject in value["reviewers"]:
            raise HTTPException(403, "independent current reviewer required")
    elif value["maintainer"] != principal.subject:
        raise HTTPException(403, "original maintainer required")
    states = {"edit": {"draft"}, "submit": {"draft"}, "withdraw": {"submitted", "rejected"}, "publish": {"submitted"}, "revoke": {"active"}}
    if action in states and value["state"] not in states[action]:
        raise HTTPException(409, "registration state changed")
    if action == "edit":
        if proposal is None or proposal["version"] != row.version:
            raise HTTPException(422, "edit requires same draft version")
    elif proposal is not None:
        raise HTTPException(422, "action cannot supply proposal")
    if action == "publish" and (
        len(value["reviewers"]) < 2
        or any(r["digest"] != value["digest"] or r["submission"] != value["submission"] for r in value["reviewers"].values())
    ):
        raise HTTPException(409, "two matching independent approvals required")
    return row


@router.get("")
async def listing(request: Request, principal: Principal = Depends(current_principal)):
    enabled(request)
    manager(principal)
    async with db.SessionLocal() as session:
        rows = list(
            await session.scalars(
                select(Registration).where(Registration.tenant == principal.tenant_id).order_by(Registration.id).limit(201)
            )
        )
        if len(rows) > 200:
            raise HTTPException(409, "isolated list limit exceeded")
        return {
            "identity": workbench.binding(principal),
            "roles": sorted(principal.roles.intersection({MAINTAINER, REVIEWER})),
            "items": [r.body for r in rows],
        }


@router.get("/ledger")
async def ledger(request: Request, principal: Principal = Depends(current_principal)):
    enabled(request)
    scope_id = workbench.user_scope(principal)
    async with db.SessionLocal() as session:
        scope = await session.get(db.WorkbenchScope, scope_id)
        row = await workbench.owned(session, scope.active_attempt, scope_id) if scope and scope.active_attempt else None
        op = await session.get(Operation, row.attempt_id) if row and row.action == "view_register" else None
        return {"scope_id": scope_id, "blocked_elsewhere": row is not None and op is None, "active": result(row, op) if op else None}


@router.post("/prepare")
async def prepare(body: Command, request: Request, principal: Principal = Depends(current_principal)):
    enabled(request)
    scope_id = workbench.user_scope(principal)
    command = body.model_dump(mode="json")
    source = getattr(request.app.state, "isolated_view_source", None)
    if not isinstance(source, dict) or not source.get("view_sha256") or not source.get("source_sha256"):
        raise HTTPException(503, "trusted synthetic source unavailable")
    command["source"] = source
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        previous = await session.get(db.WorkbenchAttempt, str(body.attempt_id))
        if previous:
            row = await workbench.owned(session, previous.attempt_id, scope_id)
            op = await session.get(Operation, row.attempt_id)
            if row.action != "view_register" or op is None or op.command != command:
                raise HTTPException(409, "attempt binding mismatch")
            return result(row, op)
        if scope.active_attempt:
            raise HTTPException(409, "WORKBENCH_LOCKED")
        await validate(session, command, principal)
        identifier = body.registration_id or "view_" + uuid4().hex[:20]
        row = db.WorkbenchAttempt(
            attempt_id=str(body.attempt_id),
            scope_id=scope_id,
            action="view_register",
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
        await session.commit()
        return result(row, op)


@router.post("/{attempt_id}/execute")
async def execute(attempt_id: UUID, body: Evidence, request: Request, principal: Principal = Depends(current_principal)):
    enabled(request)
    scope_id = workbench.user_scope(principal)
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        row = await workbench.owned(session, str(attempt_id), scope_id)
        op = await session.get(Operation, row.attempt_id)
        if row.action != "view_register" or op is None or body.fingerprint != row.fingerprint:
            raise HTTPException(409, "attempt mismatch")
        if row.status in ("COMMITTED", "REJECTED_NO_EFFECT"):
            return result(row, op)
        if scope.active_attempt != row.attempt_id or row.status != "PREPARED" or row.revision != body.revision:
            raise HTTPException(409, "attempt revision changed")
        try:
            async with session.begin_nested():
                if op.command["source"] != getattr(request.app.state, "isolated_view_source", None):
                    raise HTTPException(409, "source changed")
                target = await validate(session, op.command, principal)
                action = op.command["action"]
                if target is None:
                    proposal = op.command["proposal"]
                    value = {
                        "id": row.run_id,
                        "tenant": principal.tenant_id,
                        "view_id": "orders",
                        "version": proposal["version"],
                        "maintainer": principal.subject,
                        "state": "draft",
                        "revision": 1,
                        "proposal": proposal,
                        "source": op.command["source"],
                        "digest": digest({"proposal": proposal, "source": op.command["source"]}),
                        "submission": None,
                        "reviewers": {},
                        "generation": None,
                    }
                    target = Registration(id=row.run_id, tenant=principal.tenant_id, version=proposal["version"], body=value)
                    session.add(target)
                else:
                    value = dict(target.body)
                    value["revision"] += 1
                    if action == "edit":
                        value.update(
                            proposal=op.command["proposal"],
                            digest=digest({"proposal": op.command["proposal"], "source": value["source"]}),
                            reviewers={},
                            submission=None,
                        )
                    elif action == "submit":
                        value.update(state="submitted", submission=value["revision"], reviewers={})
                    elif action == "approve":
                        value["reviewers"] = {
                            **value["reviewers"],
                            principal.subject: {"digest": value["digest"], "submission": value["submission"]},
                        }
                    elif action in ("reject", "withdraw"):
                        value.update(state="rejected" if action == "reject" else "draft", reviewers={}, submission=None)
                    elif action == "publish":
                        all_rows = list(await session.scalars(select(Registration).where(Registration.tenant == principal.tenant_id)))
                        generation = max([r.body.get("generation") or 0 for r in all_rows] + [0]) + 1
                        for other in all_rows:
                            if other.id != target.id and other.body["state"] == "active":
                                other.body = {**other.body, "state": "superseded", "revision": other.body["revision"] + 1}
                        value.update(state="active", generation=generation)
                    elif action == "revoke":
                        value["state"] = "revoked"
                    target.body = value
                await session.flush()
                op.evidence = {
                    "attempt_id": row.attempt_id,
                    "scope_id": scope_id,
                    "fingerprint": row.fingerprint,
                    "subject": principal.subject,
                    "tenant": principal.tenant_id,
                    "action": action,
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
                action="view_registration.execute",
                object=f"attempt:{row.attempt_id}",
                result=row.status,
                detail={"fingerprint": row.fingerprint, "revision": row.revision, "action": op.command["action"]},
            )
        )
        await session.commit()
        return result(row, op)


@router.post("/{attempt_id}/acknowledge")
async def acknowledge(attempt_id: UUID, body: Evidence, request: Request, principal: Principal = Depends(current_principal)):
    enabled(request)
    scope_id = workbench.user_scope(principal)
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        row = await workbench.owned(session, str(attempt_id), scope_id)
        op = await session.get(Operation, row.attempt_id)
        if row.action != "view_register" or op is None or row.fingerprint != body.fingerprint or row.revision != body.revision:
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
                action="view_registration.acknowledge",
                object=f"attempt:{row.attempt_id}",
                result="ok",
                detail={"fingerprint": row.fingerprint, "revision": row.revision},
            )
        )
        await session.commit()
        return result(row, op)
