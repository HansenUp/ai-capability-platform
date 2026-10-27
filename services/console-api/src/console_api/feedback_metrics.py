"""单租户本机反馈只读审计：归属验证不升级为提交者来源证明。"""

import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import select

from console_api.auth import Operator, Role, require
from console_api.db import Agent, SessionLocal
from console_api.feedback_summary import Rating, summarize
from console_api.trace_metrics import MAX_PAGES, MAX_RESPONSE_BYTES, PAGE_SIZE, SourceUnavailable, attribute, get, timestamp

router = APIRouter(prefix="/metrics", tags=["Local feedback audit"])
_runtime_transport: httpx.AsyncBaseTransport | None = None
_langfuse_transport: httpx.AsyncBaseTransport | None = None


def local_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise SourceUnavailable
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise SourceUnavailable
    return value.rstrip("/")


def cohort(
    body: dict[str, Any], op: Operator, agent: str, since: datetime, until: datetime
) -> tuple[dict[str, datetime], dict[str, str], dict[str, int]]:
    if (
        body.get("scope") != "runtime_feedback_cohort_v1"
        or body.get("tenant_id") != op.tenant
        or body.get("agent_id") != agent
        or body.get("auth_method") != "user"
        or not isinstance(body.get("subject"), str)
        or not body["subject"].startswith("human:")
        or timestamp(body.get("since")) != since
        or timestamp(body.get("until")) != until
        or body.get("complete_scan") is not True
        or body.get("snapshot_guaranteed") is not False
    ):
        raise SourceUnavailable
    rows = body.get("runs")
    if not isinstance(rows, list) or len(rows) > 2000:
        raise SourceUnavailable
    completed, owners = {}, {}
    counts = {name: 0 for name in ("completed", "failed", "canceled", "pending")}
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("run_id"), str)
            or not row["run_id"]
            or row["run_id"] in owners
            or row.get("tenant_id") != op.tenant
            or row.get("agent_id") != agent
            or not isinstance(row.get("subject"), str)
            or not row["subject"]
            or not since <= timestamp(row.get("created_at")) < until
        ):
            raise SourceUnavailable
        state = row.get("state")
        if state not in {"completed", "failed", "canceled", "submitted", "working", "auth-required", "input-required"}:
            raise SourceUnavailable
        owners[row["run_id"]] = row["subject"]
        counts[state if state in {"completed", "failed", "canceled"} else "pending"] += 1
        if state == "completed":
            finished = timestamp(row.get("finished_at"))
            if finished < timestamp(row["created_at"]) or finished > timestamp(body.get("read_finished_at")):
                raise SourceUnavailable
            completed[row["run_id"]] = finished
    reported = body.get("counts")
    if (
        not isinstance(reported, dict)
        or any(type(value) is not int for value in reported.values())
        or reported != counts
        or timestamp(body.get("read_finished_at")) < timestamp(body.get("read_started_at"))
    ):
        raise SourceUnavailable
    return completed, owners, counts


async def collect(
    client: httpx.AsyncClient,
    tenant: str,
    agent: str,
    owners: dict[str, str],
    completed: dict[str, datetime],
    captured: dict[str, dict[str, Any]] | None = None,
) -> list[Rating]:
    scores: dict[str, str] = {}
    pages_seen: set[str] = set()
    expected = None
    traces: dict[str, dict[str, Any]] = {}
    ratings = []
    for page in range(1, MAX_PAGES + 1):
        body = await get(client, "/api/public/v2/scores", {"name": "user_feedback", "page": page, "limit": PAGE_SIZE})
        rows, meta = body.get("data"), body.get("meta")
        if not isinstance(rows, list) or not isinstance(meta, dict):
            raise SourceUnavailable
        total, pages = meta.get("totalItems"), meta.get("totalPages")
        if (
            type(total) is not int
            or type(pages) is not int
            or total < 0
            or not 0 <= pages <= MAX_PAGES
            or pages != (total + PAGE_SIZE - 1) // PAGE_SIZE
            or meta.get("page") != page
            or meta.get("limit") != PAGE_SIZE
            or len(rows) != min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
            or (expected is not None and expected != (total, pages))
        ):
            raise SourceUnavailable
        expected = total, pages
        page_hash = hashlib.sha256(json.dumps(rows, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if rows and page_hash in pages_seen:
            raise SourceUnavailable
        pages_seen.add(page_hash)
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise SourceUnavailable
            signature = hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if row["id"] in scores:
                if scores[row["id"]] != signature:
                    raise SourceUnavailable
                continue
            scores[row["id"]] = signature
            if row.get("name") != "user_feedback":
                continue
            metadata = row.get("metadata")
            if not isinstance(metadata, dict) or not isinstance(metadata.get("run_id"), str) or not metadata["run_id"]:
                raise SourceUnavailable
            trace_id = row.get("traceId")
            if not isinstance(trace_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", trace_id):
                raise SourceUnavailable
            if trace_id not in traces:
                traces[trace_id] = await get(client, f"/api/public/traces/{quote(trace_id, safe='')}")
            trace = traces[trace_id]
            run_id = attribute(trace, "run_id")
            trace_tenant = attribute(trace, "tenant_id")
            trace_agent = attribute(trace, "agent_id")
            trace_subject = attribute(trace, "subject")
            if (
                trace.get("id") != trace_id
                or not all(isinstance(value, str) and value for value in (run_id, trace_tenant, trace_agent, trace_subject))
                or trace.get("sessionId") != run_id
            ):
                raise SourceUnavailable
            if trace_tenant != tenant or trace_agent != agent or run_id not in completed:
                if metadata["run_id"] in completed:
                    raise SourceUnavailable
                continue
            if (
                metadata["run_id"] != run_id
                or trace_subject != owners[run_id]
                or metadata.get("subject") != owners[run_id]
                or any(key in metadata and metadata[key] != expected for key, expected in (("tenant_id", tenant), ("agent_id", agent)))
            ):
                raise SourceUnavailable
            revised = None
            try:
                revised = timestamp(row.get("updatedAt"))
                if revised > datetime.now(UTC):
                    revised = None
            except SourceUnavailable:
                pass
            if captured is not None:
                captured[row["id"]] = row
            ratings.append(Rating(row["id"], run_id, revised, row.get("value"), row.get("dataType", ""), False))
        if page >= pages:
            return ratings
    raise SourceUnavailable


async def read_cohort(client: httpx.AsyncClient, agent: str, since: datetime, until: datetime, *, v2: bool = False) -> dict[str, Any]:
    async with client.stream(
        "GET",
        "/v1/metrics/feedback-evidence" if v2 else "/v1/metrics/feedback-cohort",
        params={"agent_id": agent, "since": since.isoformat(), "until": until.isoformat()},
    ) as reply:
        if reply.status_code in (401, 403):
            raise HTTPException(reply.status_code, "Runtime feedback authorization denied")
        if reply.status_code != 200:
            raise SourceUnavailable
        raw = bytearray()
        async for chunk in reply.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SourceUnavailable
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise SourceUnavailable
    return body


async def _feedback_metrics(
    response: Response,
    since: datetime,
    until: datetime,
    agent_id: str = Query(min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
    op: Operator = Depends(require(Role.TENANT_ADMIN)),
    *,
    v2: bool = False,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if os.environ.get("CONSOLE_ENV", "dev") == "prod":
        raise HTTPException(503, "feedback audit is local only")
    if not authorization or not authorization.startswith("Bearer ") or not authorization[7:].strip():
        raise HTTPException(401, "verified user token required")
    if since.tzinfo is None or until.tzinfo is None or not timedelta(0) < until - since <= timedelta(days=7):
        raise HTTPException(422, "aware increasing range of at most seven days required")
    try:
        since, until = since.astimezone(UTC), until.astimezone(UTC)
        runtime = local_url(os.environ.get("RUNTIME_ADMIN_URL", "http://localhost:8080"))
        langfuse = local_url(os.environ.get("LANGFUSE_HOST", "http://localhost:3000"))
    except (SourceUnavailable, ValueError, OverflowError):
        raise HTTPException(503, "invalid local feedback configuration") from None
    started = datetime.now(UTC)
    try:
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(
                base_url=runtime,
                headers={"Authorization": authorization},
                timeout=5,
                trust_env=False,
                follow_redirects=False,
                transport=_runtime_transport,
            ) as client:
                body = await read_cohort(client, agent_id, since, until, v2=v2)
            if v2 and body.get("scope") != "runtime_feedback_evidence_v2":
                raise SourceUnavailable
            cohort_body = body | {"scope": "runtime_feedback_cohort_v1"} if v2 else body
            completed, owners, counts = cohort(cohort_body, op, agent_id, since, until)
            async with SessionLocal() as session:
                if await session.scalar(select(Agent.id).where(Agent.tenant_id == op.tenant, Agent.agent_id == agent_id)) is None:
                    raise HTTPException(404)
            public, secret = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
            if not public or not secret:
                raise SourceUnavailable
            async with httpx.AsyncClient(
                base_url=langfuse, auth=(public, secret), timeout=5, trust_env=False, follow_redirects=False, transport=_langfuse_transport
            ) as client:
                captured: dict[str, dict[str, Any]] = {}
                ratings = await collect(client, op.tenant, agent_id, owners, completed, captured if v2 else None)
            if v2:
                from console_api.feedback_v2 import summarize_v2

                summary = summarize_v2(completed, body.get("proofs"), captured)
                async with httpx.AsyncClient(
                    base_url=runtime,
                    headers={"Authorization": authorization},
                    timeout=5,
                    trust_env=False,
                    follow_redirects=False,
                    transport=_runtime_transport,
                ) as client:
                    after = await read_cohort(client, agent_id, since, until, v2=True)
                stable_keys = (
                    "scope",
                    "tenant_id",
                    "agent_id",
                    "subject",
                    "auth_method",
                    "since",
                    "until",
                    "complete_scan",
                    "snapshot_guaranteed",
                    "counts",
                    "runs",
                    "proofs",
                )
                if any(body.get(key) != after.get(key) for key in stable_keys):
                    raise SourceUnavailable
            else:
                summary = summarize(completed, ratings)
    except (SourceUnavailable, httpx.HTTPError, TimeoutError, ValueError, TypeError, OverflowError):
        raise HTTPException(503, "feedback audit unavailable or incomplete; no partial ratios returned") from None
    return {
        "scope": "local_feedback_aggregate_v2" if v2 else "local_feedback_audit_v1",
        "trust_mode": "runtime_observed_revision_v2" if v2 else "strict_readonly_unverified",
        "tenant_id": op.tenant,
        "agent_id": agent_id,
        "subject": body["subject"],
        "auth_method": "user",
        "since": since.isoformat(),
        "until": until.isoformat(),
        "read_started_at": started.isoformat(),
        "read_finished_at": datetime.now(UTC).isoformat(),
        "cohort_observed_at": body["read_finished_at"],
        "complete_scan": True,
        "snapshot_guaranteed": False,
        "historical_snapshot_supported": False,
        "counts": counts,
        "feedback": summary,
    }


@router.get("/feedback")
async def feedback_metrics(
    response: Response,
    since: datetime,
    until: datetime,
    agent_id: str = Query(min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
    op: Operator = Depends(require(Role.TENANT_ADMIN)),
) -> dict[str, Any]:
    return await _feedback_metrics(response, since, until, agent_id, authorization, op)


@router.get("/feedback-v2")
async def feedback_metrics_v2(
    response: Response,
    since: datetime,
    until: datetime,
    agent_id: str = Query(min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
    op: Operator = Depends(require(Role.TENANT_ADMIN)),
) -> dict[str, Any]:
    return await _feedback_metrics(response, since, until, agent_id, authorization, op, v2=True)
