"""Fixture-only policy control plane sharing authoritative workbench locks.

No production routes, real identities, real sources or downloads.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, NotRequired, Self
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, String, UniqueConstraint, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from typing_extensions import TypedDict

from runtime.api import isolated_views as views
from runtime.api import workbench
from runtime.api.auth import Principal, current_principal
from runtime.config import settings
from runtime.state import db

router = APIRouter(prefix="/v1/isolated-policies")
ENABLED = object()
MAINTAINER = "data-policy-maintainer"
REVIEWER = "data-policy-reviewer"


class ProposalData(views.ProposalData):
    manifest_id: str
    recipient: Literal["alice", "bob"]


class ManifestSnapshot(TypedDict):
    id: str
    tenant: str
    view_id: str
    version: str
    generation: int | None
    digest: str
    source: dict[str, str]
    proposal: views.ProposalData


class RecipientBinding(TypedDict):
    issuer: str | None
    audience: str
    client_id: str
    tenant: str
    subject: Literal["alice", "bob"]
    runtime_subject: str


class PolicyHead(TypedDict):
    id: str
    generation: int | None
    revision: int
    state: views.RegistrationState
    digest: str


class CommandData(TypedDict):
    attempt_id: str
    action: views.Action
    registration_id: str | None
    expected_revision: int
    proposal: ProposalData | None
    source: dict[str, str]
    base_policy: NotRequired[PolicyHead | None]
    manifest: NotRequired[ManifestSnapshot]
    recipient_binding: NotRequired[RecipientBinding]


class RegistrationData(TypedDict):
    id: str
    tenant: str
    view_id: str
    version: str
    maintainer: str
    state: views.RegistrationState
    revision: int
    proposal: ProposalData
    source: dict[str, str]
    digest: str
    submission: int | None
    reviewers: dict[str, views.ReviewData]
    generation: int | None
    base_policy: PolicyHead | None
    manifest: ManifestSnapshot
    recipient_binding: RecipientBinding


class OperationEvidence(TypedDict):
    attempt_id: str
    scope_id: str
    fingerprint: str
    subject: str
    tenant: str
    action: views.Action
    registration_id: str
    revision: int
    snapshot: RegistrationData


class PolicyPair(TypedDict):
    manifest: views.RegistrationData
    policy: RegistrationData


class Base(DeclarativeBase):
    pass


class Registration(Base):
    __tablename__ = "isolated_read_policy"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(64))
    body: Mapped[RegistrationData] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("tenant", "version"),)


class Operation(Base):
    __tablename__ = "isolated_policy_operation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command: Mapped[CommandData] = mapped_column(JSON)
    evidence: Mapped[OperationEvidence | None] = mapped_column(JSON, nullable=True)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    manifest_id: str = Field(pattern=r"^view_[a-f0-9]{20}$")
    recipient: Literal["alice", "bob"]
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    columns: list[Literal["item", "secret"]] = Field(min_length=1, max_length=2)
    filter_item: bool = True
    limit: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.version == "v1" or len(set(self.columns)) != len(self.columns) or (self.filter_item and "item" not in self.columns):
            raise ValueError("invalid synthetic proposal")
        return self


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    action: Literal["draft", "edit", "submit", "approve", "reject", "withdraw", "publish", "revoke"]
    registration_id: str | None = Field(default=None, pattern=r"^policy_[a-f0-9]{20}$")
    expected_revision: int = Field(ge=0)
    proposal: Proposal | None = None


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(ge=1)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def enabled(request: Request) -> None:
    if (
        getattr(request.app.state, "isolated_policies", None) is not ENABLED
        or settings.runtime_env != "dev"
        or db.engine.url.get_backend_name() != "sqlite"
    ):
        raise HTTPException(503, "isolated registration disabled")


def manager(principal: Principal) -> None:
    workbench.user_scope(principal)
    if not principal.roles.intersection({MAINTAINER, REVIEWER}):
        raise HTTPException(403, "explicit data view role required")


def result(row: db.WorkbenchAttempt, op: Operation) -> dict[str, object]:
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


async def registration(session: AsyncSession, identifier: str | None, tenant: str) -> Registration:
    row = await session.get(Registration, identifier, populate_existing=True)
    if row is None or row.tenant != tenant:
        raise HTTPException(404, "registration not found")
    return row


def manifest_snapshot(value: views.RegistrationData) -> ManifestSnapshot:
    return {
        "id": value["id"],
        "tenant": value["tenant"],
        "view_id": value["view_id"],
        "version": value["version"],
        "generation": value["generation"],
        "digest": value["digest"],
        "source": value["source"],
        "proposal": value["proposal"],
    }


def recipient_binding(proposal: ProposalData, principal: Principal) -> RecipientBinding:
    # Explicit synthetic identity mapping, not conversion of browser-supplied subjects.
    from runtime.config import settings

    if principal.tenant_id != "default":
        raise HTTPException(403, "synthetic identity mapping unavailable")
    return {
        "issuer": settings.jwt_issuer,
        "audience": "data-hub",
        "client_id": "gateway",
        "tenant": "default",
        "subject": proposal["recipient"],
        "runtime_subject": {"alice": "human:alice", "bob": "human:bob"}[proposal["recipient"]],
    }


async def policy_head(session: AsyncSession, tenant: str) -> PolicyHead | None:
    rows = list(await session.scalars(select(Registration).where(Registration.tenant == tenant)))
    published = [r.body for r in rows if r.body.get("generation") is not None]
    if not published:
        return None
    latest = max(published, key=lambda value: value["generation"] or 0)
    return {
        "id": latest["id"],
        "generation": latest["generation"],
        "revision": latest["revision"],
        "state": latest["state"],
        "digest": latest["digest"],
    }


async def attach_binding(session: AsyncSession, command: CommandData, principal: Principal) -> None:
    if command["action"] not in ("draft", "edit") or command["proposal"] is None:
        return
    proposal = command["proposal"]
    row = await views.registration(session, proposal["manifest_id"], principal.tenant_id)
    if row.body["state"] != "active":
        raise HTTPException(409, "active manifest required")
    command["base_policy"] = await policy_head(session, principal.tenant_id)
    command["manifest"] = manifest_snapshot(row.body)
    command["recipient_binding"] = recipient_binding(proposal, principal)


async def validate(session: AsyncSession, command: CommandData, principal: Principal) -> Registration | None:
    manager(principal)
    action = command["action"]
    required = REVIEWER if action in ("approve", "reject") else MAINTAINER
    if required not in principal.roles:
        raise HTTPException(403, "action role required")
    proposal = command["proposal"]
    target = None
    if action == "draft":
        if command["registration_id"] is not None or command["expected_revision"] != 0 or proposal is None:
            raise HTTPException(422, "draft binding invalid")
        if await session.scalar(
            select(Registration.id).where(Registration.tenant == principal.tenant_id, Registration.version == proposal["version"])
        ):
            raise HTTPException(409, "policy version exists")
        manifest, recipient = command["manifest"], command["recipient_binding"]
    else:
        target = await registration(session, command["registration_id"], principal.tenant_id)
        value = target.body
        if value["revision"] != command["expected_revision"]:
            raise HTTPException(409, "stale policy revision")
        if action in ("approve", "reject"):
            if (
                principal.subject in (value["maintainer"], value["recipient_binding"]["runtime_subject"])
                or value["state"] != "submitted"
                or principal.subject in value["reviewers"]
            ):
                raise HTTPException(403, "independent non-beneficiary reviewer required")
        elif value["maintainer"] != principal.subject:
            raise HTTPException(403, "original policy maintainer required")
        states = {
            "edit": {"draft"},
            "submit": {"draft"},
            "withdraw": {"submitted", "rejected"},
            "publish": {"submitted"},
            "revoke": {"active"},
        }
        if action in states and value["state"] not in states[action]:
            raise HTTPException(409, "policy state changed")
        if action == "edit":
            if proposal is None or proposal["version"] != target.version:
                raise HTTPException(422, "same policy draft version required")
            manifest, recipient = command["manifest"], command["recipient_binding"]
        else:
            if proposal is not None:
                raise HTTPException(422, "proposal not allowed")
            proposal, manifest, recipient = value["proposal"], value["manifest"], value["recipient_binding"]
        if action == "publish" and (
            len(value["reviewers"]) < 2
            or any(r["digest"] != value["digest"] or r["submission"] != value["submission"] for r in value["reviewers"].values())
        ):
            raise HTTPException(409, "two matching approvals required")
    if action == "publish" and (target is None or target.body["base_policy"] != await policy_head(session, principal.tenant_id)):
        raise HTTPException(409, "policy head changed; withdraw, edit and reapprove")
    # Revocation/withdrawal remain possible after manifest replacement; never grant access.
    if action not in ("revoke", "withdraw", "reject"):
        current = await views.registration(session, manifest["id"], principal.tenant_id)
        if current.body["state"] != "active" or manifest_snapshot(current.body) != manifest:
            raise HTTPException(409, "manifest changed")
        if recipient != recipient_binding(proposal, principal):
            raise HTTPException(409, "identity mapping changed")
        allowed = manifest["proposal"]
        if (
            not set(proposal["columns"]) <= set(allowed["columns"])
            or proposal["limit"] > allowed["limit"]
            or (proposal["filter_item"] and not allowed["filter_item"])
        ):
            raise HTTPException(403, "policy must narrow reviewed manifest")
    return target


@router.get("")
async def listing(request: Request, principal: Principal = Depends(current_principal)) -> dict[str, object]:
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
async def ledger(request: Request, principal: Principal = Depends(current_principal)) -> dict[str, object]:
    enabled(request)
    scope_id = workbench.user_scope(principal)
    async with db.SessionLocal() as session:
        scope = await session.get(db.WorkbenchScope, scope_id)
        row = await workbench.owned(session, scope.active_attempt, scope_id) if scope and scope.active_attempt else None
        op = await session.get(Operation, row.attempt_id) if row and row.action == "policy_register" else None
        return {
            "scope_id": scope_id,
            "blocked_elsewhere": row is not None and op is None,
            "active": result(row, op) if row is not None and op is not None else None,
        }


@router.post("/prepare")
async def prepare(body: Command, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, object]:
    enabled(request)
    scope_id = workbench.user_scope(principal)
    source = getattr(request.app.state, "isolated_view_source", None)
    if not isinstance(source, dict) or not source.get("view_sha256") or not source.get("source_sha256"):
        raise HTTPException(503, "trusted synthetic source unavailable")
    command: CommandData = {
        "attempt_id": str(body.attempt_id),
        "action": body.action,
        "registration_id": body.registration_id,
        "expected_revision": body.expected_revision,
        "proposal": {
            "manifest_id": body.proposal.manifest_id,
            "recipient": body.proposal.recipient,
            "version": body.proposal.version,
            "columns": body.proposal.columns.copy(),
            "filter_item": body.proposal.filter_item,
            "limit": body.proposal.limit,
        }
        if body.proposal
        else None,
        "source": source,
    }
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        previous = await session.get(db.WorkbenchAttempt, str(body.attempt_id))
        if previous:
            row = await workbench.owned(session, previous.attempt_id, scope_id)
            op = await session.get(Operation, row.attempt_id)
            if (
                row.action != "policy_register"
                or op is None
                or {k: v for k, v in op.command.items() if k not in ("manifest", "recipient_binding", "base_policy")} != command
            ):
                raise HTTPException(409, "attempt binding mismatch")
            return result(row, op)
        if scope.active_attempt:
            raise HTTPException(409, "WORKBENCH_LOCKED")
        await attach_binding(session, command, principal)
        await validate(session, command, principal)
        identifier = body.registration_id or "policy_" + uuid4().hex[:20]
        row = db.WorkbenchAttempt(
            attempt_id=str(body.attempt_id),
            scope_id=scope_id,
            action="policy_register",
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
async def execute(
    attempt_id: UUID, body: Evidence, request: Request, principal: Principal = Depends(current_principal)
) -> dict[str, object]:
    enabled(request)
    scope_id = workbench.user_scope(principal)
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        row = await workbench.owned(session, str(attempt_id), scope_id)
        op = await session.get(Operation, row.attempt_id)
        if row.action != "policy_register" or op is None or body.fingerprint != row.fingerprint:
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
                    if proposal is None:
                        raise HTTPException(422, "draft proposal unavailable")
                    value: RegistrationData = {
                        "id": row.run_id,
                        "tenant": principal.tenant_id,
                        "view_id": "orders",
                        "version": proposal["version"],
                        "maintainer": principal.subject,
                        "state": "draft",
                        "revision": 1,
                        "proposal": proposal,
                        "source": op.command["source"],
                        "base_policy": op.command["base_policy"],
                        "manifest": op.command["manifest"],
                        "recipient_binding": op.command["recipient_binding"],
                        "digest": digest(
                            {
                                "proposal": proposal,
                                "manifest": op.command["manifest"],
                                "recipient": op.command["recipient_binding"],
                                "base_policy": op.command["base_policy"],
                            }
                        ),
                        "submission": None,
                        "reviewers": {},
                        "generation": None,
                    }
                    target = Registration(id=row.run_id, tenant=principal.tenant_id, version=proposal["version"], body=value)
                    session.add(target)
                else:
                    value = target.body.copy()
                    value["revision"] += 1
                    if action == "edit":
                        proposal = op.command["proposal"]
                        if proposal is None:
                            raise HTTPException(422, "edit proposal unavailable")
                        value.update(
                            {
                                "proposal": proposal,
                                "base_policy": op.command["base_policy"],
                                "manifest": op.command["manifest"],
                                "recipient_binding": op.command["recipient_binding"],
                                "digest": digest(
                                    {
                                        "proposal": op.command["proposal"],
                                        "manifest": op.command["manifest"],
                                        "recipient": op.command["recipient_binding"],
                                        "base_policy": op.command["base_policy"],
                                    }
                                ),
                                "reviewers": {},
                                "submission": None,
                            }
                        )
                    elif action == "submit":
                        value.update({"state": "submitted", "submission": value["revision"], "reviewers": {}})
                    elif action == "approve":
                        value["reviewers"] = {
                            **value["reviewers"],
                            principal.subject: {"digest": value["digest"], "submission": value["submission"]},
                        }
                    elif action in ("reject", "withdraw"):
                        value.update({"state": "rejected" if action == "reject" else "draft", "reviewers": {}, "submission": None})
                    elif action == "publish":
                        all_rows = list(await session.scalars(select(Registration).where(Registration.tenant == principal.tenant_id)))
                        generation = max([r.body.get("generation") or 0 for r in all_rows] + [0]) + 1
                        for other in all_rows:
                            if other.id != target.id and other.body["state"] == "active":
                                other.body = {**other.body, "state": "superseded", "revision": other.body["revision"] + 1}
                        value.update({"state": "active", "generation": generation})
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
                action="read_policy.execute",
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
        if row.action != "policy_register" or op is None or row.fingerprint != body.fingerprint or row.revision != body.revision:
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
                action="read_policy.acknowledge",
                object=f"attempt:{row.attempt_id}",
                result="ok",
                detail={"fingerprint": row.fingerprint, "revision": row.revision},
            )
        )
        await session.commit()
        return result(row, op)


async def current_pair(tenant: str) -> PolicyPair | None:
    """One transaction, no cached permits. None preserves only never-published v1 fixture."""
    async with db.SessionLocal() as session:
        await session.execute(text("BEGIN"))
        manifests = list(await session.scalars(select(views.Registration).where(views.Registration.tenant == tenant)))
        if not any(r.body.get("generation") is not None for r in manifests):
            return None
        active = [r.body for r in manifests if r.body["state"] == "active"]
        policies = list(await session.scalars(select(Registration).where(Registration.tenant == tenant)))
        permits = [r.body for r in policies if r.body["state"] == "active"]
        if len(active) != 1 or len(permits) != 1 or permits[0]["manifest"] != manifest_snapshot(active[0]):
            raise HTTPException(403, "current policy pair unavailable")
        return {"manifest": active[0], "policy": permits[0]}
