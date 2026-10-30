"""Fixture-only MCP 2025-06-18 JSON subset; explicit platform write protocol.

Shares E2 receipt identity, never creates a second execution or implicit ACK.
No external MCP/model connections, sessions, streaming, retries or cancellation.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from runtime.api import isolated_a2a as core
from runtime.api import isolated_discovery as discovery
from runtime.api import workbench
from runtime.api.auth import Principal, current_principal, verify_jwt
from runtime.state import db

PREFIX = "/v1/isolated-mcp"
VERSION = "2025-06-18"
PROFILE = "isolated-agent-receipts-v1"
ENABLED = object()
router = APIRouter(prefix=PREFIX)


class Attempt(core.Strict):
    attempt_id: str = core.Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


class Task(core.Strict):
    task_id: str


class Empty(core.Strict):
    pass


class Call(core.Strict):
    name: str
    arguments: dict[str, Any]


class RPC(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    jsonrpc: str
    id: str | int
    method: str
    params: dict[str, Any] = {}


SCHEMAS = {
    "agents_discover": Empty,
    "agent_prepare": core.Prepare,
    "agent_execute": core.Send,
    "attempt_recover": Attempt,
    "attempt_withdraw": core.Evidence,
    "attempt_ack": core.Evidence,
    "task_read": Task,
}


async def access(request: Request, principal: Principal = Depends(current_principal)):
    if (
        getattr(request.app.state, "isolated_mcp", None) is not ENABLED
        or getattr(request.app.state, "isolated_a2a", None) is not core.ENABLED
        or discovery.settings.runtime_env != "dev"
        or db.engine.url.get_backend_name() != "sqlite"
    ):
        raise HTTPException(503, "isolated MCP disabled")
    workbench.user_scope(principal)
    if "act" in await verify_jwt(principal.token, request):
        raise HTTPException(403, "direct user required")
    # Browser clients must be same-origin; native signed test clients may omit Origin.
    origin = request.headers.get("origin")
    if origin is not None and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "origin denied")
    if request.headers.get("X-Platform-MCP-Profile") != PROFILE:
        raise HTTPException(400, "explicit platform receipt profile required")
    return principal


@router.post("")
async def endpoint(request: Request, principal: Principal = Depends(access)):
    headers = {"Cache-Control": "no-store"}
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 131072:
            raise HTTPException(413, "request too large")
    if not all(t in request.headers.get("accept", "") for t in ("application/json", "text/event-stream")):
        raise HTTPException(406, "JSON and SSE acceptance required")
    try:
        notification = json.loads(raw)
        if notification == {"jsonrpc": "2.0", "method": "notifications/initialized"}:
            if request.headers.get("MCP-Protocol-Version") != VERSION:
                raise HTTPException(400, "protocol version required")
            return Response(status_code=202, headers=headers)
        message = RPC.model_validate_json(raw)
        if message.jsonrpc != "2.0":
            raise ValueError("version")
    except (ValidationError, ValueError):
        return Response(
            json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}),
            media_type="application/json",
            headers=headers,
        )

    def result(value):
        return Response(json.dumps({"jsonrpc": "2.0", "id": message.id, "result": value}), media_type="application/json", headers=headers)

    def error(code, text):
        return Response(
            json.dumps({"jsonrpc": "2.0", "id": message.id, "error": {"code": code, "message": text}}),
            media_type="application/json",
            headers=headers,
        )

    if message.method == "initialize":
        if message.params.get("protocolVersion") != VERSION:
            return error(-32602, "unsupported protocol version")
        return result(
            {
                "protocolVersion": VERSION,
                "capabilities": {"tools": {}, "experimental": {PROFILE: {}}},
                "serverInfo": {"name": "isolated-agent-receipts", "version": "1"},
                "instructions": ("Platform profile only: explicit prepare/execute/recover/withdraw/ACK. "
                                 "Never retry writes. Shares E2 receipts."),
            }
        )
    if request.headers.get("MCP-Protocol-Version") != VERSION:
        raise HTTPException(400, "protocol version required")
    if message.method == "tools/list":
        if message.params:
            return error(-32602, "pagination not supported")
        return result(
            {
                "tools": [
                    {
                        "name": name,
                        "description": "Explicit platform receipt operation; no automatic retries or ACK",
                        "inputSchema": model.model_json_schema(),
                    }
                    for name, model in SCHEMAS.items()
                ]
            }
        )
    if message.method != "tools/call":
        return error(-32601, "method not supported")
    try:
        call = Call.model_validate(message.params)
        if call.name not in SCHEMAS:
            return error(-32602, "unknown tool")
        body = SCHEMAS[call.name].model_validate(call.arguments)
    except ValidationError:
        return error(-32602, "invalid tool arguments")
    try:
        if call.name == "agents_discover":
            value = await discovery.discover(request, Response(), principal)
            if isinstance(value, BaseModel):
                value = value.model_dump(mode="json")
        elif call.name == "agent_prepare":
            value = await core.prepare(body, request, principal)
        elif call.name == "agent_execute":
            value = await core.send(body, request, principal)
        elif call.name == "attempt_recover":
            value = await core.recover(UUID(body.attempt_id), principal)
        elif call.name == "attempt_withdraw":
            value = await core.withdraw(UUID(body.attempt_id), body, principal)
        elif call.name == "attempt_ack":
            value = await core.acknowledge(body, principal)
        else:
            value = await core.get_task(body.task_id, principal)
    except HTTPException as exc:
        return result(
            {
                "isError": True,
                "content": [{"type": "text", "text": f"HTTP {exc.status_code}; recover original receipt; never retry writes"}],
            }
        )
    return result({"isError": False, "content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value})
