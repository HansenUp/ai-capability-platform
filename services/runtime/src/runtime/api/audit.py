"""请求级审计中间件（S2.3）：每个 /v1、/admin 请求在响应结束后写一行 t_audit。

用纯 ASGI 中间件而不是 BaseHTTPMiddleware：后者会缓冲 / 包装流式响应，SSE 场景下行为不可控。
响应结束（流式响应为整条流结束或客户端断开）后才落审计，因此能拿到最终状态码、耗时，
以及 Run 的 trace_id。

身份来自 ``request.state.principal``（由 ``api.auth.current_principal`` 写入）；认证失败时为 anonymous，
失败原因取 ``request.state.auth_error``。审计对象优先取路由处理函数写入的 ``request.state.audit_object``
（如 ``run:<run_id>``），否则由路径参数拼出（如 ``agent:<agent_id>@<version>``），再否则为请求路径。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from runtime.observe import audit
from runtime.state.db import Run, SessionLocal

log = logging.getLogger(__name__)

AUDITED_PREFIXES = ("/v1/", "/admin")  # 只审计业务与管理接口；健康检查、OpenAPI 文档不审计
PATH_LIMIT = 256  # detail 中路径与认证失败原因的截断长度：未认证请求可控这些内容，防止撑大审计表


def result_of(status: int) -> str:
    """HTTP 状态码 → 审计结论。"""
    if status < 400:
        return "ok"
    if status in (401, 403):
        return "denied"
    if status < 500:
        return "rejected"
    return "error"


def _object_of(scope: Scope, state: dict[str, Any]) -> str:
    """审计对象：处理函数显式写入的优先，其次由路径参数推导，最后退回请求路径。"""
    explicit = state.get("audit_object")
    if explicit:
        return str(explicit)
    params: dict[str, Any] = scope.get("path_params") or {}
    if "run_id" in params:
        return f"run:{params['run_id']}"
    if "agent_id" in params:
        version = params.get("version")
        return f"agent:{params['agent_id']}" + (f"@{version}" if version else "")
    return str(scope.get("path", ""))


def _action_of(scope: Scope) -> str:
    """审计动作：<METHOD> <路由模板>（低基数，便于聚合）；未匹配到路由时用原始路径。"""
    route = scope.get("route")
    path = getattr(route, "path", None) or scope.get("path", "")
    return f"{scope.get('method', '')} {path}"


async def _run_trace_id(obj: str) -> str | None:
    """对象是 Run 时取其最近一次执行的 trace_id，便于从审计行跳到 Langfuse Trace。"""
    if not obj.startswith("run:"):
        return None
    async with SessionLocal() as s:
        return (await s.execute(select(Run.trace_id).where(Run.run_id == obj.removeprefix("run:")))).scalar_one_or_none()


class AuditMiddleware:
    """纯 ASGI 审计中间件。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(AUDITED_PREFIXES):
            await self.app(scope, receive, send)
            return
        state: dict[str, Any] = scope.setdefault("state", {})  # 与 Request.state 共用同一个 dict
        status = 500  # 应用未发出响应头就抛异常时按 500 记
        started = time.monotonic()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            await self._record(scope, state, status, started)

    async def _record(self, scope: Scope, state: dict[str, Any], status: int, started: float) -> None:
        """落一行请求审计；任何失败只记 ERROR 日志（响应已发出，无法再改变结果）。"""
        try:
            obj = _object_of(scope, state)
            client = scope.get("client")
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
            detail: dict[str, Any] = {
                "method": scope.get("method"),
                "path": str(scope.get("path", ""))[:PATH_LIMIT],
                "status_code": status,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "client_ip": client[0] if client else None,
            }
            if headers.get("x-request-id"):
                detail["request_id"] = headers["x-request-id"][:128]
            if state.get("auth_error"):
                detail["auth_error"] = str(state["auth_error"])[:PATH_LIMIT]
            principal = state.get("principal")
            row = audit.entry(principal, _action_of(scope), obj, result_of(status), detail=detail, trace_id=await _run_trace_id(obj))
            await audit.record(row)
        except Exception:  # noqa: BLE001
            log.exception("request audit failed: %s %s", scope.get("method"), scope.get("path"))
