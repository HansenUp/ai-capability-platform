"""既有本机 Langfuse v3 的有界只读指标；不把 Trace 当作 Run。"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from console_api.auth import Operator, Role, require
from console_api.db import Agent, SessionLocal

router = APIRouter(prefix="/metrics", tags=["Local Trace metrics"])
PAGE_SIZE = 100
MAX_PAGES = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_transport: httpx.AsyncBaseTransport | None = None


class SourceUnavailable(Exception):
    pass


def attribute(trace: dict[str, Any], key: str) -> Any:
    metadata = trace.get("metadata")
    if not isinstance(metadata, dict):
        return None
    attrs = metadata.get("attributes")
    nested = attrs.get(f"ai.{key}") if isinstance(attrs, dict) else None
    flat = metadata.get(key)
    if nested is not None and flat is not None and nested != flat:
        raise SourceUnavailable
    return nested if nested is not None else flat


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise SourceUnavailable
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as error:
        raise SourceUnavailable from error


def amount(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and 0 <= number <= Decimal("1e18") else None
    except InvalidOperation:
        return None


async def get(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with client.stream("GET", path, params=params) as response:
        if response.status_code != 200:
            raise SourceUnavailable
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SourceUnavailable
    try:
        import json

        body = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise SourceUnavailable from error
    if not isinstance(body, dict):
        raise SourceUnavailable
    return body


async def collect(
    client: httpx.AsyncClient, tenant: str, agent: str, since: datetime, until: datetime, *, half_open: bool = False
) -> dict[str, Any]:
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    expected: tuple[int, int] | None = None
    for page in range(1, MAX_PAGES + 1):
        body = await get(
            client,
            "/api/public/traces",
            {
                "name": f"run {agent}",
                "fromTimestamp": since.isoformat(),
                "toTimestamp": until.isoformat(),
                "orderBy": "timestamp.asc",
                "limit": PAGE_SIZE,
                "page": page,
            },
        )
        rows, meta = body.get("data"), body.get("meta")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or not isinstance(meta, dict):
            raise SourceUnavailable
        total, pages = meta.get("totalItems"), meta.get("totalPages")
        if isinstance(total, bool) or not isinstance(total, int) or isinstance(pages, bool) or not isinstance(pages, int):
            raise SourceUnavailable
        if total < 0 or not 0 <= pages <= MAX_PAGES or pages != (total + PAGE_SIZE - 1) // PAGE_SIZE:
            raise SourceUnavailable
        if meta.get("page") != page or meta.get("limit") != PAGE_SIZE or (expected is not None and expected != (total, pages)):
            raise SourceUnavailable
        expected = total, pages
        if len(rows) != min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE)):
            raise SourceUnavailable
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in seen:
                raise SourceUnavailable
            seen.add(row["id"])
            stamp = timestamp(row.get("timestamp"))
            if not since <= stamp <= until:
                raise SourceUnavailable
            if attribute(row, "tenant_id") == tenant and attribute(row, "agent_id") == agent and (not half_open or stamp < until):
                selected.append(row)
        if page >= pages:
            break
    else:
        raise SourceUnavailable

    semaphore = asyncio.Semaphore(4)

    async def detail(row: dict[str, Any]) -> tuple[int | None, Decimal | None, int]:
        async with semaphore:
            trace = await get(client, f"/api/public/traces/{quote(row['id'], safe='')}")
        if trace.get("id") != row["id"] or attribute(trace, "tenant_id") != tenant or attribute(trace, "agent_id") != agent:
            raise SourceUnavailable
        if timestamp(trace.get("timestamp")) != timestamp(row.get("timestamp")):
            raise SourceUnavailable
        observations = trace.get("observations")
        if not isinstance(observations, list):
            raise SourceUnavailable
        ids: set[str] = set()
        tokens: list[int | None] = []
        costs: list[Decimal | None] = []
        for observation in observations:
            if not isinstance(observation, dict) or not isinstance(observation.get("id"), str) or observation["id"] in ids:
                raise SourceUnavailable
            ids.add(observation["id"])
            if observation.get("traceId") != row["id"]:
                raise SourceUnavailable
            if observation.get("type") != "GENERATION":
                continue
            usage, details = observation.get("usage"), observation.get("usageDetails")
            token: int | None = None
            if isinstance(usage, dict) and usage.get("unit") == "TOKENS" and isinstance(details, dict):
                raw = usage.get("total")
                reported = details.get("total")
                if (
                    isinstance(raw, int)
                    and not isinstance(raw, bool)
                    and 0 <= raw <= 10**18
                    and isinstance(reported, int)
                    and not isinstance(reported, bool)
                    and reported == raw
                ):
                    token = raw
            tokens.append(token)
            cost = amount(observation.get("totalCost"))
            if (
                cost is None
                and isinstance(observation.get("costDetails"), dict)
                and any(amount(value) is not None for value in observation["costDetails"].values())
            ):
                cost = amount(observation.get("calculatedTotalCost"))
            costs.append(cost)
        return (
            sum(value for value in tokens if value is not None) if tokens and all(value is not None for value in tokens) else None,
            sum((value for value in costs if value is not None), Decimal(0))
            if costs and all(value is not None for value in costs)
            else None,
            len(tokens),
        )

    results = await asyncio.gather(*(detail(row) for row in selected), return_exceptions=True)
    values: list[tuple[int | None, Decimal | None, int]] = []
    for result in results:
        if isinstance(result, BaseException):
            raise SourceUnavailable from result
        values.append(result)
    count = len(values)
    token_values = [value[0] for value in values if value[0] is not None]
    cost_values = [value[1] for value in values if value[1] is not None]
    return {
        "trace_calls": count,
        "generations": sum(value[2] for value in values),
        "tokens": {
            "total": sum(token_values) if count and len(token_values) == count else None,
            "known_trace_subtotal": sum(token_values),
            "traces_with_complete_usage": len(token_values),
        },
        "cost_usd": {
            "total": str(sum(cost_values, Decimal(0))) if count and len(cost_values) == count else None,
            "known_trace_subtotal": str(sum(cost_values, Decimal(0))),
            "traces_with_complete_cost": len(cost_values),
        },
    }


@router.get("/traces")
async def trace_metrics(
    since: datetime, until: datetime, agent_id: str = Query(min_length=1, max_length=64), op: Operator = Depends(require(Role.TENANT_ADMIN))
) -> dict[str, Any]:
    if since.tzinfo is None or until.tzinfo is None or not timedelta(0) < until - since <= timedelta(days=7):
        raise HTTPException(422, "aware, increasing time range of at most seven days required")
    try:
        since, until = since.astimezone(UTC), until.astimezone(UTC)
    except (ValueError, OverflowError) as error:
        raise HTTPException(422, "time range cannot be represented in UTC") from error
    async with SessionLocal() as session:
        if await session.scalar(select(Agent.id).where(Agent.tenant_id == op.tenant, Agent.agent_id == agent_id)) is None:
            raise HTTPException(404)
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    try:
        parsed = urlsplit(host)
    except ValueError as error:
        raise HTTPException(503, "invalid local Langfuse configuration") from error
    if (
        os.environ.get("CONSOLE_ENV", "dev") == "prod"
        or parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HTTPException(503, "Trace metrics support only the existing local Langfuse v3 profile")
    public, secret = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not public or not secret:
        raise HTTPException(503, "Langfuse metrics credentials not configured")
    try:
        async with (
            asyncio.timeout(30),
            httpx.AsyncClient(
                base_url=host, auth=(public, secret), timeout=5, trust_env=False, follow_redirects=False, transport=_transport
            ) as client,
        ):
            metrics = await collect(client, op.tenant, agent_id, since, until)
    except (SourceUnavailable, httpx.HTTPError, TimeoutError, ValueError) as error:
        raise HTTPException(503, "Langfuse metrics unavailable or incomplete; no partial result returned") from error
    return {
        "scope": "local_langfuse_v3_trace_generations",
        "tenant_id": op.tenant,
        "agent_id": agent_id,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "run_metrics_available": False,
        "snapshot_guaranteed": False,
        "complete_scan": True,
        **metrics,
    }
