"""统一审计事件（S2.3）：写 t_audit，并镜像为一条 OTel span（经 Collector 进入 Langfuse）。

审计行由三处产生：
- 每次 API 请求（``runtime.api.audit.AuditMiddleware``）：action 为 ``<METHOD> <路由模板>``；
- 每次工具闸口判定（``loop.toolbroker``）：action 为 ``tool.decision``，result 为 allow / deny / require_approval；
- 管理写操作（``api.admin``）：action 为 ``agent.create`` / ``agent.publish``，与业务写入同一事务提交。

Langfuse 镜像：请求级与管理写审计每行一个独立 span（名字 ``audit {action}``），``langfuse.user.id`` 为操作者；
对象是 Run 时 ``langfuse.session.id=run_id``，与该 Run 的执行 Trace 归入同一会话。tool.decision 不单独镜像，
判定结论已记在执行 Trace 的 ``execute_tool`` span 上。
span 只带身份与结论等元数据，不带请求体。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from runtime.observe.tracing import get_tracer
from runtime.state.db import Audit, SessionLocal

if TYPE_CHECKING:
    from runtime.api.auth import Principal

log = logging.getLogger(__name__)

ANONYMOUS = "anonymous"  # 未通过认证的请求的操作者
_LIMITS = {"actor": 128, "act": 128, "auth_method": 24, "tenant_id": 64, "action": 64, "object": 128, "result": 16}


def _clip(field: str, value: str | None) -> str | None:
    """按列宽截断，避免 MySQL 严格模式下超长写入失败导致审计丢失。"""
    return value[: _LIMITS[field]] if value is not None else None


def entry(
    principal: Principal | None,
    action: str,
    object_: str,
    result: str,
    *,
    detail: dict[str, Any] | None = None,
    trace_id: str | None = None,
    actor: str | None = None,
    act: str | None = None,
    tenant_id: str | None = None,
) -> Audit:
    """构造一行审计（未提交）。身份默认取自 principal，显式传入的 actor / act / tenant_id 优先。"""
    return Audit(
        actor=_clip("actor", actor or (principal.subject if principal else ANONYMOUS)),
        act=_clip("act", act if act is not None else (principal.actor if principal else None)),
        auth_method=_clip("auth_method", principal.auth_method if principal else "none"),
        tenant_id=_clip("tenant_id", tenant_id or (principal.tenant_id if principal else None)),
        action=_clip("action", action),
        object=_clip("object", object_),
        result=_clip("result", result),
        trace_id=trace_id,
        detail=detail or {},
    )


def mirror(row: Audit) -> None:
    """把一行审计镜像为 OTel span；失败只记日志，不影响业务。"""
    try:
        name = f"audit {row.action}"
        attrs: dict[str, Any] = {
            "ai.audit.actor": row.actor,
            "ai.audit.auth_method": row.auth_method or "",
            "ai.audit.tenant_id": row.tenant_id or "",
            "ai.audit.action": row.action,
            "ai.audit.object": row.object,
            "ai.audit.result": row.result,
            "langfuse.user.id": row.actor,
            "langfuse.trace.name": name,
        }
        if row.act:
            attrs["ai.audit.act"] = row.act
        if row.trace_id:
            attrs["ai.audit.run_trace_id"] = row.trace_id
        status = (row.detail or {}).get("status_code")
        if isinstance(status, int):
            attrs["http.response.status_code"] = status
        if row.object.startswith("run:"):
            attrs["langfuse.session.id"] = row.object.removeprefix("run:")
        get_tracer().start_span(name, kind=trace.SpanKind.INTERNAL, attributes=attrs).end()
    except Exception:  # noqa: BLE001 —— 镜像是旁路，任何异常都不得影响主流程
        log.exception("audit mirror failed")


async def record(row: Audit) -> None:
    """独立事务写入一行审计并镜像。写库失败记 ERROR 日志后上抛，由调用方决定是否吞掉。"""
    async with SessionLocal() as s:
        s.add(row)
        await s.commit()
    mirror(row)
