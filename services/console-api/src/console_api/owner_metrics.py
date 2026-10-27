from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.security import HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse

from console_api import owner_auth, trace_metrics
from console_api.db import AgentVersion, SessionLocal


class OwnerRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                response = await original(request)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except RequestValidationError:
                response = JSONResponse({"detail": "invalid Owner query"}, status_code=422)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Authorization"
            return response

        return handle


router = APIRouter(
    prefix="/owner/agents",
    tags=["Local Owner read metrics"],
    route_class=OwnerRoute,
    dependencies=[Depends(HTTPBearer(auto_error=False, scheme_name="OwnerLocalUserToken"))],
)
_transport: httpx.MockTransport | None = None


async def read(request: Request, agent_id: str, window: tuple[datetime, datetime] | None = None) -> dict[str, Any]:
    started = owner_auth.now()
    allowed = {"since", "until"} if window is not None else set()
    if set(request.query_params) - allowed or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise HTTPException(400, "Owner query overrides are not accepted")
    if not 1 <= len(agent_id) <= 64 or any(character in agent_id for character in "/*?\\") or agent_id.strip() != agent_id:
        raise HTTPException(422, "invalid Owner Agent")
    try:
        async with asyncio.timeout(30):
            profile, owner = await owner_auth.authenticate_async(request)
            await owner_auth.assigned(owner, agent_id)
            if window is None:
                async with SessionLocal() as session:
                    rows = dict(
                        (
                            await session.execute(
                                select(AgentVersion.status, func.count(AgentVersion.id))
                                .where(AgentVersion.tenant_id == owner.tenant, AgentVersion.agent_id == agent_id)
                                .group_by(AgentVersion.status)
                            )
                        ).all()
                    )
                if set(rows) - {"draft", "published", "retired"}:
                    raise HTTPException(503, "Owner version state unavailable")
                result = {"versions": {status: rows.get(status, 0) for status in ("draft", "published", "retired")}}
                scope = "owner_agent_summary_v1"
            else:
                since, until = window
                if since.tzinfo is None or until.tzinfo is None:
                    raise HTTPException(422, "aware Owner time window required")
                try:
                    since, until = since.astimezone(UTC), until.astimezone(UTC)
                    valid = timedelta(0) < until - since <= timedelta(days=7) and until <= started
                except (ValueError, OverflowError) as exc:
                    raise HTTPException(422, "invalid Owner time window") from exc
                if not valid:
                    raise HTTPException(422, "invalid Owner time window")
                if not isinstance(_transport, httpx.MockTransport):
                    raise HTTPException(503, "Owner local substitute transport required")
                async with httpx.AsyncClient(
                    base_url="http://127.0.0.1:3000",
                    auth=("owner-test-public", "owner-test-secret"),
                    timeout=5,
                    follow_redirects=False,
                    trust_env=False,
                    transport=_transport,
                ) as client:
                    metrics = await trace_metrics.collect(client, owner.tenant, agent_id, since, until, half_open=True)
                result = {
                    "trace_calls": metrics["trace_calls"],
                    "generations": metrics["generations"],
                    "tokens": {key: metrics["tokens"][key] for key in ("total", "known_trace_subtotal", "traces_with_complete_usage")},
                    "cost_usd": {key: metrics["cost_usd"][key] for key in ("total", "known_trace_subtotal", "traces_with_complete_cost")},
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "interval": "half_open",
                    "complete_scan": True,
                    "run_metrics_available": False,
                    "source_trust": "metadata_consistency_only",
                }
                scope = "owner_agent_trace_metrics_v1"
            _, final_owner = await owner_auth.authenticate_async(request, profile)
            if owner != final_owner:
                raise HTTPException(403, "Owner identity binding changed")
            await owner_auth.assigned(final_owner, agent_id)
    except (trace_metrics.SourceUnavailable, httpx.HTTPError, SQLAlchemyError, TimeoutError, ValueError) as exc:
        raise HTTPException(503, "Owner metrics unavailable; no partial result returned") from exc
    return {
        "scope": scope,
        "schema_version": 1,
        "tenant_id": owner.tenant,
        "agent_id": agent_id,
        "read_started_at": started.isoformat(),
        "read_finished_at": owner_auth.now().isoformat(),
        "snapshot_guaranteed": False,
        "authorization_policy": "current_assignment_rechecked_v1",
        **result,
    }


@router.get("/{agent_id}/summary")
async def summary(request: Request, agent_id: str) -> dict[str, Any]:
    return await read(request, agent_id)


@router.get("/{agent_id}/trace-metrics")
async def traces(request: Request, agent_id: str, since: datetime, until: datetime) -> dict[str, Any]:
    return await read(request, agent_id, (since, until))
