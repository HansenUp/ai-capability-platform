"""Messages API (HTTP + SSE) and Run API (A2A-named states, approval/input/cancel, event stream).
（Messages API：兼容通用对话形态的 HTTP/SSE 入口；Run API：以 A2A 任务状态命名，提供
审批/续跑/取消/事件流查询等围绕 Run 生命周期的操作。两组 API 共享同一条底层驱动
（runtime.loop.runner.drive），只是入口形态不同。）
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from ai_contract import Event, RunEventType, RunState
from runtime.api.auth import Principal, current_principal
from runtime.loop import runner
from runtime.state.db import AgentVersion, Run, SessionLocal
from runtime.state.eventlog import EventLog

router = APIRouter()


class MessagesRequest(BaseModel):
    """/v1/messages 的请求体：类似通用对话补全接口，但绑定到具体 agent_id/agent_version。"""

    agent_id: str  # 目标 Agent 标识
    agent_version: str | None = None  # 指定版本，缺省取该 agent 已发布的最新版本
    session_id: str | None = None  # 会话标识（用于多轮关联，当前实现未强制使用）
    messages: list[dict[str, Any]]  # 本轮输入消息
    attachments: list[str] = Field(default_factory=list)  # 附件引用
    stream: bool = True  # 是否以 SSE 流式返回
    metadata: dict[str, Any] = Field(default_factory=dict)  # 附加元数据


async def _load_agent(agent_id: str, version: str | None) -> AgentVersion:
    """按 agent_id（可选指定 version）加载 Agent 版本快照；未指定版本时取已发布的最新版本。"""
    async with SessionLocal() as s:
        q = select(AgentVersion).where(AgentVersion.agent_id == agent_id)
        q = q.where(AgentVersion.version == version) if version else q.where(AgentVersion.status == "published")
        agent = (await s.execute(q.order_by(AgentVersion.id.desc()))).scalars().first()
    if agent is None:
        raise HTTPException(404, f"agent '{agent_id}' has no published version")
    return agent


def _sse(ev: Event) -> dict[str, str]:
    """将统一事件对象转换为 SSE 帧（event 字段为事件类型，data 字段为其 JSON 序列化）。"""
    return {"event": ev.type, "data": ev.model_dump_json()}


@router.post("/v1/messages")
async def messages(req: MessagesRequest, request: Request, principal: Principal = Depends(current_principal)) -> Any:
    """对话式入口：创建 Run 并驱动一次执行；支持流式（SSE）与非流式（聚合后一次性返回）两种模式。"""
    agent = await _load_agent(req.agent_id, req.agent_version)
    run_id = await runner.create_run(agent, principal.subject, req.session_id)
    gen = runner.drive(run_id, agent, principal.subject, req.messages)
    if req.stream:

        async def stream():
            async for ev in gen:
                yield _sse(ev)

        return EventSourceResponse(stream(), headers={"X-Run-Id": run_id})
    events = [ev async for ev in gen]  # 非流式：先消费完整个生成器，再聚合返回
    text = "".join(e.delta for e in events if e.type == "text")
    return {"run_id": run_id, "content": text, "events": [e.model_dump() for e in events]}


# ---------------- Run API ----------------


class RunCreate(BaseModel):
    """/v1/runs 的创建请求体：面向 A2A 风格的任务提交场景。"""

    agent_id: str  # 目标 Agent 标识
    agent_version: str | None = None  # 指定版本
    input: list[dict[str, Any]]  # 初始输入消息
    mode: str = "async"  # 执行模式（当前实现固定为后台异步执行）


@router.post("/v1/runs", status_code=202)
async def create_run(req: RunCreate, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """创建一个 Run 并立即返回 202（已接受）；实际驱动在后台任务中异步执行。"""
    agent = await _load_agent(req.agent_id, req.agent_version)
    run_id = await runner.create_run(agent, principal.subject, None)
    # In production a worker picks this up from Redis Streams; here we execute eagerly in background.
    # （生产环境应由 worker 从 Redis Streams 消费任务；此处为简化实现，直接在后台任务中即时执行。
    # Redis Streams 接入属于 S7 工作包。）
    import asyncio

    async def _bg() -> None:
        async for _ in runner.drive(run_id, agent, principal.subject, req.input):
            pass  # 后台驱动只需消费生成器使其执行完成，事件已在 drive 内部写入事件日志

    asyncio.create_task(_bg())
    return {"run_id": run_id, "state": RunState.SUBMITTED}


@router.get("/v1/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    """查询单个 Run 的当前状态与元信息。"""
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise HTTPException(404)
        return {
            "run_id": run.run_id,
            "state": run.state,
            "agent_id": run.agent_id,
            "agent_version": run.agent_version,
            "model_alias": run.model_alias,
            "subject": run.subject,
            "last_seq": run.last_seq,
            "created_at": run.created_at.isoformat(),
        }


@router.get("/v1/runs/{run_id}/events")
async def run_events(run_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    """按 seq 增量查询 Run 的事件日志（自 since_seq 之后的所有事件）。"""
    async with SessionLocal() as s:
        return [e.model_dump(mode="json") for e in await EventLog(s).list(run_id, since_seq)]


class ApprovalBody(BaseModel):
    """审批/拒绝工具调用请求体。

    params_hash 是必填项：审批附着于具体参数与资源，审批人必须回传卡片上展示的那一份参数指纹，
    否则无从判断"批的到底是哪一组参数"。
    """

    call_id: str  # 待审批的工具调用标识
    params_hash: str  # 审批时卡片上展示的参数指纹，必须与闸口决策事件中记录的一致
    ttl_seconds: int = 900  # 审批有效期（秒），超时后该批准失效，必须重新审批
    comment: str = ""  # 审批意见


@router.post("/v1/runs/{run_id}/approve")
async def approve(run_id: str, body: ApprovalBody, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """批准一次挂起中的高风险工具调用：仅在 Run 处于 auth-required 状态时允许，
    记录 TOOL_APPROVED 事件并把状态迁回 working，随后需调用 /resume 才能真正继续执行。

    批准会绑定"参数指纹 + 有效期"：参数一旦变化或审批超时，闸口不会放行，必须重新审批。"""
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        if run is None or run.state != RunState.AUTH_REQUIRED:
            raise HTTPException(409, "run is not awaiting approval")
        log = EventLog(s)
        decided = [
            e.payload.get("params_hash")
            for e in await log.list(run_id)
            if e.type == RunEventType.TOOL_DECISION and e.payload.get("call_id") == body.call_id
        ]
        if not decided or decided[-1] != body.params_hash:
            # 参数指纹对不上：说明待审批的参数已经变化（或审批人拿的是过期卡片），必须重新走审批。
            raise HTTPException(409, "params_hash does not match the pending call; re-approval required（参数已变化，需重新审批）")
        expires_at = (datetime.now(UTC) + timedelta(seconds=body.ttl_seconds)).isoformat()
        await log.append(
            run_id,
            RunEventType.TOOL_APPROVED,
            {
                "call_id": body.call_id,
                "approver": principal.subject,
                "comment": body.comment,
                "params_hash": body.params_hash,
                "expires_at": expires_at,
            },
        )
        await log.transition(run_id, RunState.WORKING, "approved")
        await s.commit()
    return {"run_id": run_id, "state": RunState.WORKING, "resume": f"/v1/runs/{run_id}/resume"}


@router.post("/v1/runs/{run_id}/reject")
async def reject(run_id: str, body: ApprovalBody, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """拒绝一次挂起中的高风险工具调用：记录 TOOL_REJECTED 事件并将 Run 直接置为 failed。"""
    async with SessionLocal() as s:
        log = EventLog(s)
        await log.append(
            run_id, RunEventType.TOOL_REJECTED, {"call_id": body.call_id, "approver": principal.subject, "comment": body.comment}
        )
        await log.transition(run_id, RunState.FAILED, "rejected")
        await s.commit()
    return {"run_id": run_id, "state": RunState.FAILED}


class ResumeBody(BaseModel):
    """续跑请求体：messages: the original initial messages (clients keep them; workers read from session store)
    （客户端需要回传最初的输入消息用于重建历史；生产环境中 worker 也可以改为从会话存储读取。）"""

    messages: list[dict[str, Any]]


@router.post("/v1/runs/{run_id}/resume")
async def resume(run_id: str, body: ResumeBody, principal: Principal = Depends(current_principal)) -> Any:
    """Continue a run after approval by replaying its event log (checkpoint via replay).
    （在审批通过后续跑一个 Run：通过重放该 Run 的事件日志实现 checkpoint 续跑，
    只允许对处于 working 状态的 Run 发起。）
    """
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise HTTPException(404)
        if run.state != RunState.WORKING:
            raise HTTPException(409, f"run state is {run.state}")
    agent = await _load_agent(run.agent_id, run.agent_version)

    async def stream():
        async for ev in runner.drive(run_id, agent, principal.subject, body.messages):
            yield _sse(ev)

    return EventSourceResponse(stream())


@router.post("/v1/runs/{run_id}/cancel")
async def cancel(run_id: str) -> dict[str, Any]:
    """取消一个 Run；若当前状态不允许迁移到 canceled（如已是终态），返回 409。"""
    async with SessionLocal() as s:
        log = EventLog(s)
        try:
            await log.transition(run_id, RunState.CANCELED, "user")
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        await s.commit()
    return {"run_id": run_id, "state": RunState.CANCELED}


def _dumps(o: Any) -> str:
    """带非 ASCII 直出（ensure_ascii=False）的 JSON 序列化辅助函数，避免中文被转义。"""
    return json.dumps(o, ensure_ascii=False)
