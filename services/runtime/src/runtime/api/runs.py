"""Run 列表与事件流（S7 §5，D114）。

- ``GET /v1/runs``：(created_at, run_id) 倒序键集分页、不透明游标；只返回调用方本人的 Run，平台管理员
  （platform-admin，开发态不检查角色）可看本租户全部并按 subject 过滤；审批人不在列表中（收件箱另行提供）。
- ``GET /v1/runs/{run_id}/stream``：SSE 尾随事件日志，读取权限同 D90（复用 messages.run_reader 与审批视图），
  Run 终态后以 done 收尾结束。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy import and_, or_, select
from sse_starlette.sse import EventSourceResponse

from ai_contract import RunState
from runtime.api.auth import Principal, current_principal, is_admin
from runtime.api.messages import RunAccess, _forbid, _frame, run_reader, run_view, tail_run
from runtime.api.views import ErrorDetail, RunListView
from runtime.state.db import Run, SessionLocal

router = APIRouter(dependencies=[Depends(current_principal)])

MAX_LIMIT = 100


def _encode_cursor(run: Run) -> str:
    raw = json.dumps({"c": run.created_at.isoformat(), "r": run.run_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """不透明游标 → (created_at, run_id)；格式不对 422（参数校验错误）。"""
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        created, run_id = datetime.fromisoformat(raw["c"]), raw["r"]
        if not isinstance(run_id, str):
            raise TypeError
    except (binascii.Error, ValueError, KeyError, TypeError) as e:
        # 与 FastAPI 参数校验同一形态（HTTPValidationError）
        raise RequestValidationError([{"type": "value_error", "loc": ("query", "cursor"), "msg": "invalid cursor", "input": cursor}]) from e
    return created, run_id


@router.get("/v1/runs", response_model=RunListView, responses={403: {"model": ErrorDetail}})
async def list_runs(
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    session_id: Annotated[str | None, Query(max_length=40)] = None,
    state: RunState | None = None,
    agent_id: Annotated[str | None, Query(max_length=64)] = None,
    subject: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> dict[str, Any]:
    """本人的 Run 列表（最新在前）。管理员默认看本租户全部，可用 subject 只看某人；非管理员只能看自己
    （subject 给成别人 403）。next_cursor 原样传回即取下一页，为 null 表示没有更多。"""
    admin = is_admin(principal)
    if not admin and subject is not None and subject != principal.subject:
        raise _forbid(request, "only an admin can list other subjects' runs")
    owner = subject if admin else principal.subject
    q = select(Run).where(Run.tenant_id == principal.tenant_id)
    q = q.where(or_(Run.agent_id != "f4-data-query", Run.subject == principal.subject))
    # Reserved F6.2 synthetic profile: even administrators cannot enumerate another owner's results.
    q = q.where(or_(Run.model_alias.notin_(["synthetic-sales", "synthetic-analytics"]), Run.subject == principal.subject))
    if owner is not None:
        q = q.where(Run.subject == owner)
    if session_id is not None:
        q = q.where(Run.session_id == session_id)
    if state is not None:
        q = q.where(Run.state == state.value)
    if agent_id is not None:
        q = q.where(Run.agent_id == agent_id)
    if cursor is not None:
        created, last_id = _decode_cursor(cursor)
        q = q.where(or_(Run.created_at < created, and_(Run.created_at == created, Run.run_id < last_id)))
    q = q.order_by(Run.created_at.desc(), Run.run_id.desc()).limit(limit + 1)
    async with SessionLocal() as s:
        rows = list((await s.execute(q)).scalars())
    page = rows[:limit]
    items = [run_view(r) | {"session_id": r.session_id} for r in page]
    return {"items": items, "next_cursor": _encode_cursor(page[-1]) if len(rows) > limit else None}


@router.get(
    "/v1/runs/{run_id}/stream",
    response_model=None,  # 200 为 SSE：事件日志行（event=类型，id=seq，data=RunEvent），末尾一条 done（data=统一 DoneEvent）
    responses={403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
)
async def stream_run(
    run_id: str,
    access: Annotated[RunAccess, Depends(run_reader)],
    since_seq: Annotated[int, Query(ge=0, le=2**53)] = 0,
    last_event_id: Annotated[str | None, Header(max_length=20)] = None,
) -> Any:
    """尾随 Run 的事件日志（S7，D108）：先补发 seq > since_seq 的事件，再随写入持续下发，Run 进入终态后下发 done 并结束。
    `/v1/messages` 的客户端在节点故障后可凭已收到的 seq（或 SSE 的 Last-Event-ID）续读接管者的执行。

    读取权限同 `GET /v1/runs/{run_id}/events`（D90）：发起人、已记录的审批人、管理员看完整日志；待审批期间尚未审批的
    有审批资格者只看审批视图，Run 离开 auth-required 后流结束。"""
    if last_event_id is not None and last_event_id.isdigit():
        since_seq = max(since_seq, min(int(last_event_id), 2**53))

    async def frames() -> AsyncIterator[dict[str, str]]:
        async for ev in tail_run(run_id, access, since_seq):
            frame = _frame(ev)
            if frame is not None:
                yield frame

    return EventSourceResponse(frames())
