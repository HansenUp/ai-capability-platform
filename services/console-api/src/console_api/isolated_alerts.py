"""Explicitly injected, disposable synthetic alert laboratory. No real sender or node input."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from console_api import owner_auth
from console_api.auth import Role

router = APIRouter(prefix="/isolated-alerts", tags=["isolated-alerts"])


def stamp():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self):
        self.profile = owner_auth._profile
        self.temporary = tempfile.TemporaryDirectory(prefix="f3-alerts-")
        self.path = Path(self.temporary.name) / "alerts.sqlite"
        self.notification_result = "simulated_received"
        with self.connection() as db:
            db.execute("CREATE TABLE alerts (tenant TEXT, id TEXT, body TEXT, PRIMARY KEY(tenant,id))")
            db.execute("CREATE TABLE operations (tenant TEXT, id TEXT, subject TEXT, body TEXT, PRIMARY KEY(tenant,id))")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=0)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.OperationalError as exc:
            db.rollback()
            raise HTTPException(409, "concurrent access; read current state, do not resend automatically") from exc
        finally:
            db.close()

    def close(self):
        self.temporary.cleanup()

    def observe(self, tenant, identifier, evidence, seq, signal):
        # Fixture-only evidence producer; deliberately has no HTTP ingestion endpoint.
        if signal not in {"abnormal", "recovered", "unknown"} or type(seq) is not int or seq < 1:
            raise ValueError("invalid synthetic signal")
        with self.connection() as db:
            row = db.execute("SELECT body FROM alerts WHERE tenant=? AND id=?", (tenant, identifier)).fetchone()
            if row:
                body = json.loads(row[0])
                if seq < body["source_seq"]:
                    raise ValueError("stale evidence")
                if seq == body["source_seq"]:
                    if (evidence, signal) != (body["evidence"], body["signal"]):
                        raise ValueError("conflicting evidence")
                    return
                if signal == "abnormal" and body["signal"] == "recovered":
                    raise ValueError("new abnormal cycle requires new incident id")
                body.update(evidence=evidence, source_seq=seq, signal=signal, revision=body["revision"] + 1, observed_at=stamp())
            else:
                body = dict(
                    id=identifier,
                    tenant_id=tenant,
                    node_id="synthetic-node",
                    rule="synthetic-node-health-v1",
                    evidence=evidence,
                    source_seq=seq,
                    signal=signal,
                    handling="unclaimed",
                    assignee=None,
                    revision=1,
                    observed_at=stamp(),
                    synthetic=True,
                    history=[],
                    notifications=[],
                )
            db.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?)", (tenant, identifier, json.dumps(body)))


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str = Field(min_length=36, max_length=36)
    revision: int = Field(ge=1)
    action: Literal["claim", "handle", "notify"]
    reason: Literal["investigated", "no_action_needed"] | None = None
    evidence: str | None = Field(default=None, max_length=128)


async def context(request):
    store = getattr(request.app.state, "isolated_alert_store", None)
    if os.environ.get("CONSOLE_ENV", "dev") != "dev" or not isinstance(store, Store):
        raise HTTPException(503, "isolated alerts disabled")
    profile, actor = await owner_auth.authenticate_async(request)
    if actor.role < Role.TENANT_ADMIN:
        raise HTTPException(403, "verified tenant administrator required")
    # Recheck after any asynchronous JWKS refresh. No await between this check and transaction.
    if profile is not store.profile:
        raise HTTPException(403, "isolated alert identity profile changed")
    return store, actor


def read_row(db, tenant, identifier):
    row = db.execute("SELECT body FROM alerts WHERE tenant=? AND id=?", (tenant, identifier)).fetchone()
    if row is None:
        raise HTTPException(404, "alert not found in current scope")
    return json.loads(row[0])


@router.get("")
async def listing(request: Request):
    store, actor = await context(request)
    with store.connection() as db:
        rows = db.execute("SELECT body FROM alerts WHERE tenant=? ORDER BY id LIMIT 201", (actor.tenant,)).fetchall()
        if len(rows) > 200:
            raise HTTPException(409, "isolated list limit exceeded")
        return {"tenant_id": actor.tenant, "subject": actor.subject, "synthetic": True, "items": [json.loads(row[0]) for row in rows]}


@router.get("/operations/{operation_id}")
async def receipt(operation_id: UUID, request: Request):
    store, actor = await context(request)
    with store.connection() as db:
        row = db.execute(
            "SELECT body FROM operations WHERE tenant=? AND id=? AND subject=?", (actor.tenant, str(operation_id), actor.subject)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "no receipt; absence does not prove no write")
        return json.loads(row[0])


@router.get("/{identifier}")
async def detail(identifier: str, request: Request):
    store, actor = await context(request)
    with store.connection() as db:
        return read_row(db, actor.tenant, identifier)


@router.post("/{identifier}/actions")
async def mutate(identifier: str, body: Mutation, request: Request):
    store, actor = await context(request)
    try:
        if str(UUID(body.operation_id)) != body.operation_id:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(422, "canonical operation UUID required") from exc
    with store.connection() as db:
        row = read_row(db, actor.tenant, identifier)
        if db.execute("SELECT 1 FROM operations WHERE tenant=? AND id=?", (actor.tenant, body.operation_id)).fetchone():
            raise HTTPException(409, "operation already recorded; read receipt without resending")
        if body.revision != row["revision"]:
            raise HTTPException(409, "revision changed; read current detail")
        if body.action == "claim":
            if row["handling"] != "unclaimed" or body.reason is not None or body.evidence is not None:
                raise HTTPException(409, "not available for self claim")
            row.update(handling="claimed", assignee=actor.subject)
        else:
            if row["assignee"] != actor.subject:
                raise HTTPException(403, "only original claimant may handle or simulate notification")
            if body.action == "handle":
                if row["handling"] != "claimed" or body.reason is None or body.evidence != row["evidence"]:
                    raise HTTPException(409, "handling requires current evidence and reason")
                row["handling"] = "handled"
            else:
                if body.reason is not None or body.evidence is not None:
                    raise HTTPException(422, "notification contains no external payload")
                row["notifications"].append(
                    {
                        "operation_id": body.operation_id,
                        "status": store.notification_result,
                        "at": stamp(),
                        "channel": "in-memory-synthetic",
                        "subject": actor.subject,
                    }
                )
        row["revision"] += 1
        row["history"].append(
            {
                "operation_id": body.operation_id,
                "action": body.action,
                "subject": actor.subject,
                "at": stamp(),
                "reason": body.reason,
                "evidence": body.evidence,
                "revision": row["revision"],
            }
        )
        result = {
            "tenant_id": actor.tenant,
            "subject": actor.subject,
            "alert_id": identifier,
            "request": body.model_dump(),
            "status": "committed",
            "revision": row["revision"],
            "synthetic": True,
        }
        db.execute("UPDATE alerts SET body=? WHERE tenant=? AND id=?", (json.dumps(row), actor.tenant, identifier))
        db.execute("INSERT INTO operations VALUES (?,?,?,?)", (actor.tenant, body.operation_id, actor.subject, json.dumps(result)))
        return result
