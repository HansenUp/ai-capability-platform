"""平台工具的 MCP 代理（Streamable HTTP，JSON 响应模式）：ACP agent 唯一可见的工具来源（D94）。

经 ACP ``session/new.mcpServers`` 以 HTTP 方式注入（名为 ``platform``，带每会话随机 bearer）。只实现工具子集：
``initialize`` / ``notifications/*`` / ``tools/list`` / ``tools/call`` / ``ping``；其余方法（含 2026 草案的
``server/discover``）回 -32601，客户端据此回退到 ``initialize``（goose 1.54 / dsh 0.2 实测）。GET（服务端推送流）
回 405：本代理不主动推送。

``tools/call`` 不执行任何业务：交给宿主（桥）认领 call_id、上报 ToolUseEvent 并挂起，直到平台在下一 turn 帧里
回灌同 call_id 的 tool 结果，再把结果文本作为 MCP 结果返回。客户端发来 ``notifications/cancelled`` 时放弃等待。

工具名（D92 命名规则，沿用 S5 claude-sdk 的思路）：平台名 → MCP 名 ``wire_tool_name``（只含 ``[A-Za-z0-9_-]``，
超长截断加哈希），反向一律查本会话的映射表 ``ToolNames``；两个平台名撞到同一 MCP 名时拒绝（失败关闭）。
agent 给模型看的名字再由 profile 加前缀（goose ``platform__<wire>``，dsh ``mcp__platform__<wire>``）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Iterable
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ai_contract import ToolSpec

from .jsonrpc import as_object

MCP_SERVER_NAME = "platform"  # 注入给 agent 的 MCP server 名
MAX_WIRE_NAME = 48  # MCP 工具名上限（给 agent 的前缀留出余量，模型侧函数名上限 64）
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


class ToolNameConflictError(ValueError):
    """两个平台工具名映射到同一个 MCP 工具名。"""


def wire_tool_name(name: str) -> str:
    """平台工具名 → MCP 工具名（不可逆；反向查 ToolNames）。"""
    safe = _UNSAFE.sub("_", name)
    if len(safe) > MAX_WIRE_NAME:
        safe = f"{safe[: MAX_WIRE_NAME - 9]}_{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"
    return safe


class ToolNames:
    """一组平台工具的双向名字映射：平台名 ↔ MCP 名。"""

    def __init__(self, names: Iterable[str] = ()) -> None:
        self.to_platform: dict[str, str] = {}
        self.to_wire: dict[str, str] = {}
        for name in names:
            self.add(name)

    def add(self, name: str) -> None:
        wire = wire_tool_name(name)
        if self.to_platform.setdefault(wire, name) != name:
            raise ToolNameConflictError(f"platform tools {self.to_platform[wire]!r} and {name!r} both map to MCP tool name {wire!r}")
        self.to_wire[name] = wire

    def platform(self, wire: str) -> str | None:
        return self.to_platform.get(wire)

    def wire(self, name: str) -> str:
        return self.to_wire.get(name) or wire_tool_name(name)


def mcp_tool(spec: ToolSpec) -> dict[str, Any]:
    """ToolSpec → MCP 工具声明（inputSchema 补齐 type/properties）。"""
    schema = {"type": "object", "properties": {}, **(spec.input_schema or {})}
    return {"name": wire_tool_name(spec.name), "description": spec.description, "inputSchema": schema}


def tool_result_text(content: Any) -> str:
    """平台工具结果 → 交给 agent 的文本：字符串原样，None 为空串，其余 JSON。"""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, default=str)


class ToolCallRejected(Exception):
    """宿主拒绝了这次 tools/call（未被模型请求、非平台工具、会话已中断等）；消息回给 agent。"""


class ProxyHost(Protocol):
    """工具代理的宿主（桥里的会话状态）。"""

    token: str

    def mcp_tools(self) -> list[ToolSpec]:
        """当前可见的平台工具。"""
        ...

    async def call_tool(self, wire_name: str, arguments: dict[str, Any], meta_call_id: str | None) -> tuple[str, bool]:
        """认领 call_id、上报 ToolUseEvent 并挂起等待平台结果；返回 (结果文本, is_error)。拒绝时抛 ToolCallRejected。"""
        ...


def _bearer_ok(request: Request, token: str) -> bool:
    got = request.headers.get("authorization", "")
    return hmac.compare_digest(got.encode(), f"Bearer {token}".encode())


class McpToolProxy:
    """Starlette 端点：/mcp。每个桥进程只服务一个 agent 会话。"""

    def __init__(self, host: ProxyHost) -> None:
        self.host = host
        self.session_id = uuid.uuid4().hex
        self._cancels: dict[str, asyncio.Event] = {}  # tools/call 的 JSON-RPC id → 取消信号

    async def endpoint(self, request: Request) -> Response:
        if not _bearer_ok(request, self.host.token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.method == "DELETE":
            return Response(status_code=200)
        if request.method != "POST":
            return Response(status_code=405)
        try:
            msg = await request.json()
        except ValueError:
            return _rpc_error(None, -32700, "parse error", status=400)
        if isinstance(msg, list):  # 不支持 JSON-RPC batch（MCP 2025-06 起已移除）
            return _rpc_error(None, -32600, "batch requests are not supported", status=400)
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            return Response(status_code=202)  # 客户端发来的响应 / 无效消息：无须应答
        method, params = msg["method"], as_object(msg.get("params"))
        if "id" not in msg:
            if method == "notifications/cancelled":
                ev = self._cancels.get(str(params.get("requestId")))
                if ev is not None:
                    ev.set()
            return Response(status_code=202)
        rid = msg["id"]
        if method == "initialize":
            requested = str(params.get("protocolVersion", ""))
            result = {
                "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": MCP_SERVER_NAME, "version": "1"},
            }
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result}, headers={"mcp-session-id": self.session_id})
        if method == "ping":
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {}})
        if method == "tools/list":
            tools = [mcp_tool(t) for t in self.host.mcp_tools()]
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
        if method == "tools/call":
            return await self._call(rid, params)
        return _rpc_error(rid, -32601, f"method not found: {method}")

    async def _call(self, rid: Any, params: dict[str, Any]) -> Response:
        name = str(params.get("name", ""))
        arguments = as_object(params.get("arguments"))
        meta = as_object(params.get("_meta"))
        meta_id = meta.get("agent-tool-call-request-id")  # goose 1.54 带模型侧 tool_call id；dsh 不带
        cancel = self._cancels[str(rid)] = asyncio.Event()
        call = asyncio.ensure_future(self.host.call_tool(name, arguments, str(meta_id) if meta_id else None))
        waiter = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({call, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not call.done():
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
                return _rpc_error(rid, -32800, "request cancelled")
            try:
                text, is_error = call.result()
            except ToolCallRejected as exc:
                return _rpc_error(rid, -32602, str(exc))
            except asyncio.CancelledError:
                return _rpc_error(rid, -32800, "request cancelled")
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})
        finally:
            waiter.cancel()
            self._cancels.pop(str(rid), None)


def _rpc_error(rid: Any, code: int, message: str, *, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}, status_code=status)
