"""Opt-in B3 read-only bridge for an isolated SQLite/ASGI harness.

Not mounted by Runtime and never used by production dispatch. The caller supplies
only persisted identifiers and signed credentials, not execution context. Gateway
remains the sole act signer. No retries, new approval semantics or write support.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime import net
from runtime.api.auth import principal_from_claims, verify_jwt
from runtime.state.db import AgentVersion, Run, RunEventRow


async def invoke_read(
    session: AsyncSession,
    *,
    run_id: str,
    call_id: str,
    user_token: str,
    service_token: str,
    gateway: httpx.ASGITransport,
) -> dict[str, Any]:
    """Execute one already-gated persisted read using in-process Gateway only.

    The test harness owns the temporary SQLite database and test JWKS transport.
    This is not an HTTP endpoint: browser supplied subject, Agent, input, approval
    or taint fields cannot be supplied. A fresh read is performed for each call.
    No snapshot guarantee is claimed across Runtime and Gateway policy checking.
    """
    if session.get_bind().dialect.name != "sqlite" or not isinstance(gateway, httpx.ASGITransport):
        raise ValueError("isolated SQLite and ASGI transport required")
    principal = principal_from_claims(await verify_jwt(user_token))
    if principal.auth_method != "user":
        raise HTTPException(403, "user identity required")
    row = (
        await session.execute(
            select(Run, AgentVersion)
            .join(
                AgentVersion,
                (AgentVersion.agent_id == Run.agent_id) & (AgentVersion.version == Run.agent_version),
            )
            .where(
                Run.run_id == run_id,
                Run.subject == principal.subject,
                Run.tenant_id == principal.tenant_id,
                AgentVersion.tenant_id == principal.tenant_id,
                AgentVersion.status == "published",
                Run.state == "working",
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(403, "read context unavailable")
    run, agent = row
    events = list(
        (
            await session.scalars(
                select(RunEventRow).where(RunEventRow.run_id == run_id).order_by(RunEventRow.seq).execution_options(populate_existing=True)
            )
        ).all()
    )
    selected = [event for event in events if event.payload.get("call_id") == call_id]
    requests = [event for event in selected if event.type == "tool.requested"]
    decisions = [event for event in selected if event.type == "tool.decision"]
    executions = [event for event in selected if event.type == "tool.executing"]
    if len(requests) != 1 or len(decisions) != 1 or len(executions) != 1:
        raise HTTPException(409, "ambiguous or ungated read")
    requested, decision, executing = requests[0], decisions[0], executions[0]
    payload = requested.payload
    tool = payload.get("tool")
    # Only the first pending read is supported by this isolation harness. No
    # previous tool output is silently treated as untainted input.
    if (
        payload.get("risk_level") != "read"
        or not isinstance(tool, str)
        or tool not in agent.config.get("tools", [])
        or decision.payload.get("decision") != "allow"
        or not requested.seq < decision.seq < executing.seq
        or selected[-1] is not executing
        or executing.seq != run.last_seq
        or any(event.type == "tool.result" for event in events)
        or executing.payload.get("tool") != tool
    ):
        raise HTTPException(409, "read is not eligible")
    parameters = payload.get("effective_input", payload.get("input"))
    if not isinstance(parameters, dict):
        raise HTTPException(409, "invalid persisted input")
    # Use the same canonical fingerprint as the existing Runtime gate.
    from runtime.guard.gate import params_hash

    fingerprint = params_hash(parameters)
    if any(event.payload.get("params_hash") != fingerprint for event in (decision, executing)):
        raise HTTPException(409, "persisted input binding mismatch")
    body = {
        "tool": tool,
        "input": parameters,
        "subject": run.subject,
        "agent_id": run.agent_id,
        "agent_version": run.agent_version,
        "agent_tenant": run.tenant_id,
        "run_id": run.run_id,
        "call_id": call_id,
        "approved": False,
        "dry_run": False,
        "taint": ["confidential"],
    }
    async with net.client(transport=gateway, base_url=net.settings.capability_gateway_url, follow_redirects=False) as client:
        response = await client.post(
            "/invoke",
            json=body,
            headers={
                "Authorization": f"Bearer {service_token}",
                "X-AI-User-Token": user_token,
            },
        )
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result
