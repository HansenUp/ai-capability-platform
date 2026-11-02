"""Fixture-only evaluation ledger; never mounted by the normal Runtime app.

Non-durable children are created once under the parent subject lock. Crashes leave
EXECUTING untouched: reads and repeated execute requests cannot resume work.
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, String, UniqueConstraint, and_, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from runtime.api import isolated_datasets as datasets
from runtime.api import isolated_evaluation_rules as rules
from runtime.api import messages, workbench, workbench_create
from runtime.api.auth import Principal, current_principal, verify_jwt
from runtime.loop import runner
from runtime.state import db
from runtime.state.eventlog import EventLog

router = APIRouter(prefix="/v1/isolated-evaluations")
ENABLED = object()
ACTION = "synthetic_eval"


class Batch(datasets.Base):
    __tablename__ = "isolated_evaluation_batch"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command: Mapped[dict] = mapped_column(JSON)
    binding: Mapped[dict] = mapped_column(JSON)
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Child(datasets.Base):
    __tablename__ = "isolated_evaluation_child"
    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), unique=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    __table_args__ = (UniqueConstraint("batch_id", "case_id"),)


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    dataset_id: str = Field(pattern=r"^dataset_[a-f0-9]{20}$")
    agent_id: str = Field(min_length=1, max_length=64)
    agent_version: str = Field(min_length=1, max_length=64)


def enabled(request):
    datasets.enabled(request)
    if getattr(request.app.state, "isolated_evaluations", None) is not ENABLED:
        raise HTTPException(503, "isolated evaluation disabled")


async def authorized(request, principal, command):
    await datasets.authorize(request, principal, command["agent_id"])
    claims = await verify_jwt(principal.token, request)
    if "act" in claims:
        raise HTTPException(403, "direct test user required")
    grants = getattr(request.app.state, "isolated_evaluation_grants", frozenset())
    if (principal.tenant_id, principal.subject, command["agent_id"], command["agent_version"]) not in grants:
        raise HTTPException(403, "exact evaluation execution grant required")


async def bindings(session, request, principal, command):
    await authorized(request, principal, command)
    dataset = await datasets.registration(session, command["dataset_id"], principal.tenant_id)
    if dataset.agent != command["agent_id"] or dataset.body.get("state") != "frozen":
        raise HTTPException(409, "frozen Agent dataset required")
    proposal = datasets.Proposal.model_validate(dataset.body["proposal"])
    agent, reason = await messages._load_agent(
        command["agent_id"], command["agent_version"], principal.tenant_id, principal.subject
    )
    if agent.harness != "mock" or agent.config != {"tools": [rules.TOOLS[proposal.scenario_id]]}:
        raise HTTPException(409, "isolated mock snapshot and exact tool configuration required")
    profile = getattr(request.app.state, "isolated_evaluation_profile", None)
    if profile is None or proposal.scenario_id not in profile.messages:
        raise HTTPException(503, "synthetic execution profile unavailable")
    binding = {
        "tenant": principal.tenant_id, "subject": principal.subject,
        "dataset_id": dataset.id, "dataset_version": dataset.version,
        "dataset_revision": dataset.body["revision"], "dataset_digest": datasets.digest(dataset.body),
        "agent_id": agent.agent_id, "agent_version": agent.version,
        "agent_snapshot": workbench_create.snapshot(agent, reason),
        "catalog_revision": proposal.catalog_revision, "evaluator_version": rules.VERSION,
        "scenario": proposal.scenario_id, "cases": proposal.case_ids,
        "profile_digest": profile.digest,
    }
    return binding, agent, profile


async def view(session, row, batch):
    children = list(await session.scalars(select(Child).where(Child.batch_id == row.attempt_id).order_by(Child.case_id)))
    return {
        "attempt_id": row.attempt_id, "scope_id": row.scope_id, "fingerprint": row.fingerprint,
        "revision": row.revision, "status": row.status, "acknowledged": row.acknowledged,
        "binding": batch.binding, "receipt": batch.receipt, "rejection": row.rejection,
        "children": [{"case_id": c.case_id, "run_id": c.run_id, "evidence": c.evidence} for c in children],
        "source": "isolated-synthetic", "publish_eligible": False,
    }


async def owned(session, principal, attempt_id, evidence=None):
    row = await workbench.owned(session, str(attempt_id), workbench.user_scope(principal))
    batch = await session.get(Batch, row.attempt_id)
    if row.action != ACTION or batch is None or (evidence and evidence.fingerprint != row.fingerprint):
        raise HTTPException(409, "evaluation attempt binding mismatch")
    return row, batch


def audit(session, principal, row, action):
    session.add(db.Audit(actor=principal.subject, tenant_id=principal.tenant_id, auth_method=principal.auth_method,
                         action=action, object=f"attempt:{row.attempt_id}", result=row.status,
                         detail={"revision": row.revision, "fingerprint": row.fingerprint}))


@router.get("/ledger")
async def ledger(request: Request, principal: Principal = Depends(current_principal)):
    enabled(request)
    scope_id = workbench.user_scope(principal)
    async with db.SessionLocal() as session:
        scope = await session.get(db.WorkbenchScope, scope_id)
        if not scope or not scope.active_attempt:
            return {"active": None, "blocked_elsewhere": False}
        row = await workbench.owned(session, scope.active_attempt, scope_id)
        if row.action != ACTION:
            return {"active": None, "blocked_elsewhere": True}
        row, batch = await owned(session, principal, row.attempt_id)
        return {"active": await view(session, row, batch), "blocked_elsewhere": False}


async def history_authorized(request, principal, agent_id):
    enabled(request)
    workbench.user_scope(principal)
    claims = await verify_jwt(principal.token, request)
    if "act" in claims:
        raise HTTPException(403, "direct test user required")
    await datasets.authorize(request, principal, agent_id)


async def history_owned(session, principal, attempt_id, agent_id):
    row, batch = await owned(session, principal, attempt_id)
    if (batch.binding.get("tenant") != principal.tenant_id
            or batch.binding.get("subject") != principal.subject
            or batch.binding.get("agent_id") != agent_id):
        raise HTTPException(404, "evaluation history not found")
    return row, batch


@router.get("/history")
async def history(
    request: Request, agent_id: str = Query(min_length=1, max_length=64),
    limit: int = Query(default=20, ge=1, le=50), before: UUID | None = None,
    status: Literal["PREPARED", "EXECUTING", "COMMITTED", "REJECTED_NO_EFFECT"] | None = None,
    principal: Principal = Depends(current_principal),
):
    await history_authorized(request, principal, agent_id)
    async with db.SessionLocal() as session:
        query = select(db.WorkbenchAttempt, Batch).join(Batch, Batch.id == db.WorkbenchAttempt.attempt_id).where(
            db.WorkbenchAttempt.scope_id == workbench.user_scope(principal),
            db.WorkbenchAttempt.action == ACTION,
            Batch.binding["tenant"].as_string() == principal.tenant_id,
            Batch.binding["subject"].as_string() == principal.subject,
            Batch.binding["agent_id"].as_string() == agent_id,
        )
        if status:
            query = query.where(db.WorkbenchAttempt.status == status)
        if before:
            anchor, _ = await history_owned(session, principal, before, agent_id)
            query = query.where(or_(
                db.WorkbenchAttempt.created_at < anchor.created_at,
                and_(db.WorkbenchAttempt.created_at == anchor.created_at,
                     db.WorkbenchAttempt.attempt_id < anchor.attempt_id),
            ))
        rows = list((await session.execute(query.order_by(
            db.WorkbenchAttempt.created_at.desc(), db.WorkbenchAttempt.attempt_id.desc()
        ).limit(limit + 1))).all())
        items = [{
            "attempt_id": row.attempt_id, "created_at": row.created_at.isoformat(),
            "status": row.status, "acknowledged": row.acknowledged,
            "agent_version": batch.binding["agent_version"],
            "dataset_version": batch.binding["dataset_version"],
        } for row, batch in rows[:limit]]
        await history_authorized(request, principal, agent_id)
        return {
            "scope_id": workbench.user_scope(principal), "agent_id": agent_id, "items": items,
            "next_cursor": items[-1]["attempt_id"] if len(rows) > limit else None,
            "snapshot_guaranteed": False, "source": "isolated-synthetic", "publish_eligible": False,
        }


@router.get("/history/compare")
async def compare_history(
    request: Request, left: UUID, right: UUID, agent_id: str = Query(min_length=1, max_length=64),
    principal: Principal = Depends(current_principal),
):
    await history_authorized(request, principal, agent_id)
    if left == right:
        raise HTTPException(422, "two distinct evaluations required")
    async with db.SessionLocal() as session:
        a, b = [await history_owned(session, principal, key, agent_id) for key in (left, right)]
        values = [await view(session, *pair) for pair in (a, b)]
        await history_authorized(request, principal, agent_id)
        return {"scope_id": workbench.user_scope(principal), "agent_id": agent_id,
                "left": values[0], "right": values[1], "comparison": rules.comparison(*values)}


@router.get("/history/{attempt_id}")
async def history_detail(
    attempt_id: UUID, request: Request, agent_id: str = Query(min_length=1, max_length=64),
    principal: Principal = Depends(current_principal),
):
    await history_authorized(request, principal, agent_id)
    async with db.SessionLocal() as session:
        pair = await history_owned(session, principal, attempt_id, agent_id)
        result = await view(session, *pair)
        await history_authorized(request, principal, agent_id)
        return result


@router.post("/prepare")
async def prepare(body: Prepare, request: Request, principal: Principal = Depends(current_principal)):
    enabled(request)
    scope_id = workbench.user_scope(principal)
    command = body.model_dump(mode="json")
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        previous = await session.get(db.WorkbenchAttempt, str(body.attempt_id))
        if previous:
            row, batch = await owned(session, principal, body.attempt_id)
            await authorized(request, principal, command)
            if batch.command != command:
                raise HTTPException(409, "attempt command mismatch")
            return await view(session, row, batch)
        if scope.active_attempt:
            raise HTTPException(409, "WORKBENCH_LOCKED")
        binding, _, _ = await bindings(session, request, principal, command)
        row = db.WorkbenchAttempt(
            attempt_id=str(body.attempt_id), scope_id=scope_id, action=ACTION,
            run_id="eval_" + uuid4().hex[:20], request_seq=0,
            fingerprint=datasets.digest({"scope": scope_id, "command": command, "binding": binding}),
            status="PREPARED", revision=1, acknowledged=False,
        )
        batch = Batch(id=row.attempt_id, command=command, binding=binding)
        session.add_all([row, batch])
        scope.active_attempt = row.attempt_id
        audit(session, principal, row, "evaluation.prepare")
        await session.commit()
        return await view(session, row, batch)


def terminal_receipt(row, batch, result):
    return {"attempt_id": row.attempt_id, "scope_id": row.scope_id, "action": ACTION,
            "fingerprint": row.fingerprint, "revision": row.revision,
            "binding_digest": datasets.digest(batch.binding), "result": result}


async def completed_result(session, batch):
    children = list(await session.scalars(select(Child).where(Child.batch_id == batch.id).order_by(Child.case_id)))
    if {c.case_id for c in children} != set(batch.binding["cases"]) or any(c.evidence is None for c in children):
        raise HTTPException(409, "batch evidence incomplete")
    for child in children:
        run = await session.get(db.Run, child.run_id, populate_existing=True)
        events = await EventLog(session).list(child.run_id)
        if (run is None or run.state not in ("completed", "failed", "canceled")
                or run.state != child.evidence["state"] or run.last_seq != child.evidence["last_seq"]
                or run.tenant_id != batch.binding["tenant"] or run.subject != batch.binding["subject"]
                or run.agent_id != batch.binding["agent_id"] or run.agent_version != batch.binding["agent_version"]
                or datasets.digest([e.model_dump(mode="json") for e in events]) != child.evidence["events_digest"]):
            raise HTTPException(409, "child evidence changed")
    result = rules.aggregate(batch.binding["cases"], [
        rules.Verdict(c.case_id, c.evidence["judgement"], tuple(c.evidence["reasons"])) for c in children
    ])
    return result | {"children_digest": datasets.digest([
        {"case_id": c.case_id, "run_id": c.run_id, "evidence": c.evidence} for c in children
    ])}


@router.post("/{attempt_id}/execute")
async def execute(attempt_id: UUID, body: datasets.Evidence, request: Request, principal: Principal = Depends(current_principal)):
    enabled(request)
    scope_id = workbench.user_scope(principal)
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        row, batch = await owned(session, principal, attempt_id, body)
        if row.status != "PREPARED":
            await authorized(request, principal, batch.command)
            # A repeat can only read. In particular EXECUTING never resumes.
            return await view(session, row, batch)
        if scope.active_attempt != row.attempt_id or row.revision != body.revision:
            raise HTTPException(409, "attempt revision changed")
        try:
            binding, agent, profile = await bindings(session, request, principal, batch.command)
            if binding != batch.binding:
                raise HTTPException(409, "evaluation snapshot changed")
        except HTTPException:
            row.status = "REJECTED_NO_EFFECT"
            row.rejection = "ADMISSION_REJECTED"
            row.revision += 1
            batch.receipt = terminal_receipt(row, batch, {"rejection": row.rejection})
            audit(session, principal, row, "evaluation.reject")
            await session.commit()
            return await view(session, row, batch)
        row.status = "EXECUTING"
        row.revision += 1
        audit(session, principal, row, "evaluation.start")
        await session.commit()
    # No background job or retry is scheduled. Cancellation/crash keeps EXECUTING.
    for case_id in binding["cases"]:
        async with db.SessionLocal() as session:
            scope = await workbench.lock_scope(session, scope_id)
            row, batch = await owned(session, principal, attempt_id, body)
            current, agent, profile = await bindings(session, request, principal, batch.command)
            if current != binding or scope.active_attempt != row.attempt_id or row.status != "EXECUTING":
                raise HTTPException(409, "evaluation context changed; read-only recovery required")
            if await session.get(Child, (row.attempt_id, case_id)):
                raise HTTPException(409, "child execution already registered")
            text = profile.messages[binding["scenario"]][case_id]
            run_id = "run_" + uuid4().hex[:20]
            child_binding = datasets.digest({"parent": row.fingerprint, "case": case_id, "text": text})
            await runner.persist_run(
                session, agent, principal.subject, None, actor=principal.actor, rollout="isolated-evaluation",
                initial_messages=[{"role": "user", "content": text}], durable=False, run_id=run_id,
                attempt_id=row.attempt_id, value_hmac=child_binding, snapshot_hash=binding["agent_snapshot"],
            )
            session.add(Child(batch_id=row.attempt_id, case_id=case_id, run_id=run_id))
            await session.commit()
        await profile.execute(run_id, agent, principal, text)
        async with db.SessionLocal() as session:
            await workbench.lock_scope(session, scope_id)
            child = await session.get(Child, (str(attempt_id), case_id))
            run = await session.get(db.Run, run_id)
            events = await EventLog(session).list(run_id)
            if child is None or run is None or run.state not in ("completed", "failed", "canceled"):
                raise HTTPException(409, "child terminal evidence missing; remains unresolved")
            expected = {"agent_id": binding["agent_id"], "agent_version": binding["agent_version"],
                        "subject": principal.subject, "workbench_attempt_id": str(attempt_id),
                        "workbench_value_hmac": child_binding, "workbench_snapshot_hash": binding["agent_snapshot"]}
            if (run.tenant_id != principal.tenant_id or run.subject != principal.subject
                    or run.agent_id != binding["agent_id"] or run.agent_version != binding["agent_version"]
                    or not events or any(events[0].payload.get(k) != v for k, v in expected.items())):
                raise HTTPException(409, "child evidence binding mismatch")
            verdict = rules.evaluate(binding["scenario"], case_id, run.state, [e.model_dump(mode="json") for e in events])
            child.evidence = {"state": run.state, "judgement": verdict.judgement, "reasons": list(verdict.reasons),
                              "last_seq": run.last_seq, "events_digest": datasets.digest([e.model_dump(mode="json") for e in events])}
            await session.commit()
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        row, batch = await owned(session, principal, attempt_id, body)
        if scope.active_attempt != row.attempt_id or row.status != "EXECUTING":
            raise HTTPException(409, "batch state changed")
        result = await completed_result(session, batch)
        row.status = "COMMITTED"
        row.revision += 1
        batch.receipt = terminal_receipt(row, batch, result)
        audit(session, principal, row, "evaluation.complete")
        await session.commit()
        return await view(session, row, batch)


@router.post("/{attempt_id}/acknowledge")
async def acknowledge(attempt_id: UUID, body: datasets.Evidence, request: Request,
                      principal: Principal = Depends(current_principal)):
    enabled(request)
    scope_id = workbench.user_scope(principal)
    async with db.SessionLocal() as session:
        scope = await workbench.lock_scope(session, scope_id)
        row, batch = await owned(session, principal, attempt_id, body)
        if row.revision != body.revision:
            raise HTTPException(409, "receipt revision mismatch")
        if row.acknowledged:
            return await view(session, row, batch)
        if scope.active_attempt != row.attempt_id or row.status not in ("COMMITTED", "REJECTED_NO_EFFECT"):
            raise HTTPException(409, "terminal receipt required")
        if not batch.receipt or batch.receipt != terminal_receipt(row, batch, batch.receipt.get("result")):
            raise HTTPException(409, "terminal receipt binding mismatch")
        if row.status == "COMMITTED":
            if await completed_result(session, batch) != batch.receipt["result"]:
                raise HTTPException(409, "result evidence changed")
        elif (batch.receipt["result"] != {"rejection": "ADMISSION_REJECTED"}
              or row.rejection != "ADMISSION_REJECTED"
              or await session.scalar(select(Child.run_id).where(Child.batch_id == row.attempt_id).limit(1)) is not None):
            raise HTTPException(409, "no-effect evidence conflicts")
        row.acknowledged = True
        row.revision += 1
        scope.active_attempt = None
        audit(session, principal, row, "evaluation.acknowledge")
        await session.commit()
        return await view(session, row, batch)
