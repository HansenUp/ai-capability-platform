"""Messages API (HTTP + SSE) and Run API (A2A-named states, approval/input/cancel, event stream).
（Messages API：兼容通用对话形态的 HTTP/SSE 入口；Run API：以 A2A 任务状态命名，提供
审批/续跑/取消/事件流查询等围绕 Run 生命周期的操作。两组 API 共享同一条底层驱动
（runtime.loop.runner.drive），只是入口形态不同。）
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from ai_contract import Event, RunEventType, RunState, TextEvent, ThinkingEvent
from runtime.api import rollout
from runtime.api.auth import Principal, check_approver, current_principal
from runtime.api.views import ApprovalView, ErrorDetail, RunEventsView, RunStateView, RunView
from runtime.config import settings
from runtime.loop import active, runner
from runtime.loop.toolbroker import ARTIFACT_REF
from runtime.state.db import AgentVersion, Run, SessionLocal
from runtime.state.eventlog import EventLog

router = APIRouter(dependencies=[Depends(current_principal)])  # 全部 /v1 接口要求调用方身份（AUTH_REQUIRED=true 时即 JWT）


class MessagesRequest(BaseModel):
    """/v1/messages 的请求体：类似通用对话补全接口，但绑定到具体 agent_id/agent_version。"""

    agent_id: str  # 目标 Agent 标识
    agent_version: str | None = None  # 指定版本，缺省取该 agent 已发布的最新版本
    session_id: str | None = None  # 会话标识（用于多轮关联，当前实现未强制使用）
    messages: list[dict[str, Any]]  # 本轮输入消息
    # 附件：调用方自己此前 Run 产生的外置工件引用（artifact://<id>）；他人的工件一律 403（S4.8 附件隔离）
    attachments: list[str] = Field(default_factory=list, max_length=10)
    stream: bool = True  # 是否以 SSE 流式返回
    metadata: dict[str, Any] = Field(default_factory=dict)  # 附加元数据


async def _load_agent(
    agent_id: str, version: str | None, tenant_id: str, subject: str | None = None, *, allow_unpublished: bool = False
) -> tuple[AgentVersion, str]:
    """加载调用方租户内的 Agent 版本快照，返回（版本，选版原因）。

    - 指定 version：只能是已发布版本（pinned）——否则已回滚的版本、未过发布闸门的草稿都能被直接调用；
      续跑（resume）固定原 Run 的版本，传 allow_unpublished=True，不受该版本之后被回滚的影响；
    - 未指定：在已发布版本中按主体做哈希灰度选版（stable / canary / allowlist / only，见 runtime.api.rollout）。
    其他租户的 Agent 与不存在同样返回 404，不泄露存在性。"""
    async with SessionLocal() as s:
        q = select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.tenant_id == tenant_id)
        if version:
            q = q.where(AgentVersion.version == version)
            if not allow_unpublished:
                q = q.where(AgentVersion.status == "published")
            agent = (await s.execute(q)).scalars().first()
            reason = "pinned"
        else:
            published = (await s.execute(q.where(AgentVersion.status == "published"))).scalars().all()
            agent, reason = rollout.choose(published, subject or "")
    if agent is None:
        raise HTTPException(404, f"agent '{agent_id}' has no published version" + (f" '{version}'" if version else ""))
    return agent, reason


async def run_in_tenant(run_id: str, request: Request, principal: Principal = Depends(current_principal)) -> None:
    """Run 级租户隔离：Run 不存在或不属于调用方租户一律 404；同时把 Run 记为本次请求的审计对象。"""
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
    if run is None or run.tenant_id != principal.tenant_id:
        raise HTTPException(404, f"run '{run_id}' not found")
    request.state.audit_object = f"run:{run_id}"


ATTACHMENT_SCAN_RUNS = 200  # 查找附件来源时最多检查调用方最近的多少个 Run


async def _attachment_sources(refs: list[str], principal: Principal) -> list[tuple[str, str]]:
    """校验附件归属：每个 artifact:// 引用必须出自调用方本人（同租户）最近的某个 Run 的工作区，否则 403。
    返回（引用, 源文件路径）。"""
    async with SessionLocal() as s:
        q = (
            select(Run.run_id)
            .where(Run.subject == principal.subject, Run.tenant_id == principal.tenant_id)
            .order_by(Run.created_at.desc())
            .limit(ATTACHMENT_SCAN_RUNS)
        )
        own_runs = list((await s.execute(q)).scalars())
    return await asyncio.to_thread(_find_sources, refs, own_runs)


def _find_sources(refs: list[str], own_runs: list[str]) -> list[tuple[str, str]]:
    """在调用方自己的 Run 工作区中查找每个附件的源文件（文件系统操作，放在线程里执行）。"""
    out: list[tuple[str, str]] = []
    for ref in refs:
        m = ARTIFACT_REF.match(ref)
        candidates = (os.path.join(settings.workspace_dir, r, f"{m.group(1)}.txt") for r in own_runs) if m else iter(())
        src = next((p for p in candidates if os.path.isfile(p)), None)
        if src is None:
            raise HTTPException(403, f"attachment not accessible: {ref[:80]}")
        out.append((ref, src))
    return out


def _attach(run_id: str, sources: list[tuple[str, str]], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把附件复制进新 Run 的工作区（read_artifact 只能读本 Run 的工作区），并在最后一条用户消息后注明附件引用。"""
    if not sources:
        return messages
    dest = os.path.join(settings.workspace_dir, run_id)
    os.makedirs(dest, exist_ok=True)
    for _ref, src in sources:
        shutil.copyfile(src, os.path.join(dest, os.path.basename(src)))
    note = "附件（可用 read_artifact 读取）：" + "、".join(ref for ref, _ in sources)
    return [*messages, {"role": "user", "content": note}]


def _sse(ev: Event) -> dict[str, str]:
    """将统一事件对象转换为 SSE 帧（event 字段为事件类型，data 字段为其 JSON 序列化）。"""
    return {"event": ev.type, "data": ev.model_dump_json()}


def _client_visible(ev: Event) -> bool:
    """思维链可见性策略：visibility=internal 的 thinking 事件只进事件日志，不下发给客户端；
    summary/full 原样下发。visibility 已由 runner 按 Agent 版本配置的 thinking_visibility 覆写。"""
    return not (isinstance(ev, ThinkingEvent) and ev.visibility == "internal")


@router.post("/v1/messages")
async def messages(req: MessagesRequest, request: Request, principal: Principal = Depends(current_principal)) -> Any:
    """对话式入口：创建 Run 并驱动一次执行；支持流式（SSE）与非流式（聚合后一次性返回）两种模式。"""
    agent, reason = await _load_agent(req.agent_id, req.agent_version, principal.tenant_id, principal.subject)
    sources = await _attachment_sources(req.attachments, principal) if req.attachments else []
    run_id = await runner.create_run(agent, principal.subject, req.session_id, actor=principal.actor, rollout=reason)
    request.state.audit_object = f"run:{run_id}"
    messages = _attach(run_id, sources, req.messages)
    gen = runner.drive(run_id, agent, principal.subject, messages, credential=principal.token)
    if req.stream:

        async def stream() -> AsyncIterator[dict[str, str]]:
            async for ev in gen:
                if _client_visible(ev):
                    yield _sse(ev)

        return EventSourceResponse(stream(), headers={"X-Run-Id": run_id})
    events = [ev async for ev in gen if _client_visible(ev)]  # 非流式：先消费完整个生成器，再聚合返回
    text = "".join(e.delta for e in events if isinstance(e, TextEvent))
    return {"run_id": run_id, "content": text, "events": [e.model_dump() for e in events]}


@router.get("/v1/agents")
async def list_my_agents(principal: Principal = Depends(current_principal)) -> list[dict[str, Any]]:
    """门户用：调用方租户内有已发布版本的 Agent，以及按灰度规则该调用方会用到的版本。
    （门户可见范围按 Console 的 visibility 过滤在 S4 接入。）"""
    async with SessionLocal() as s:
        q = select(AgentVersion).where(AgentVersion.tenant_id == principal.tenant_id, AgentVersion.status == "published")
        published = (await s.execute(q)).scalars().all()
    by_agent: dict[str, list[AgentVersion]] = {}
    for v in published:
        by_agent.setdefault(v.agent_id, []).append(v)
    out = []
    for agent_id in sorted(by_agent):
        chosen, reason = rollout.choose(by_agent[agent_id], principal.subject)
        if chosen is not None:
            out.append({"agent_id": agent_id, "version": chosen.version, "rollout": reason})
    return out


# ---------------- Run API ----------------


class RunCreate(BaseModel):
    """/v1/runs 的创建请求体：面向 A2A 风格的任务提交场景。"""

    agent_id: str  # 目标 Agent 标识
    agent_version: str | None = None  # 指定版本
    input: list[dict[str, Any]]  # 初始输入消息
    mode: str = "async"  # 执行模式（当前实现固定为后台异步执行）


@router.post("/v1/runs", status_code=202, response_model=RunStateView, responses={404: {"model": ErrorDetail}})
async def create_run(req: RunCreate, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """创建一个 Run 并立即返回 202（已接受）；实际驱动在后台任务中异步执行。"""
    agent, reason = await _load_agent(req.agent_id, req.agent_version, principal.tenant_id, principal.subject)
    run_id = await runner.create_run(agent, principal.subject, None, actor=principal.actor, rollout=reason)
    request.state.audit_object = f"run:{run_id}"
    # In production a worker picks this up from Redis Streams; here we execute eagerly in background.
    # （生产环境应由 worker 从 Redis Streams 消费任务；此处为简化实现，直接在后台任务中即时执行。
    # Redis Streams 接入属于 S7 工作包。）
    import asyncio

    async def _bg() -> None:
        async for _ in runner.drive(run_id, agent, principal.subject, req.input, credential=principal.token):
            pass  # 后台驱动只需消费生成器使其执行完成，事件已在 drive 内部写入事件日志

    asyncio.create_task(_bg())
    return {"run_id": run_id, "state": RunState.SUBMITTED}


@router.get("/v1/runs/{run_id}", response_model=RunView, responses={404: {"model": ErrorDetail}}, dependencies=[Depends(run_in_tenant)])
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


@router.get("/v1/runs/{run_id}/events", response_model=RunEventsView, dependencies=[Depends(run_in_tenant)])
async def run_events(run_id: str, since_seq: Annotated[int, Query(ge=0, le=2**53)] = 0) -> list[dict[str, Any]]:
    """按 seq 增量查询 Run 的事件日志（自 since_seq 之后的所有事件）。

    since_seq 限定在 [0, 2^53]：既覆盖任何现实的 seq，又保证 JS 客户端可精确表示，超界由 FastAPI 返回 422。"""
    async with SessionLocal() as s:
        return [e.model_dump() for e in await EventLog(s).list(run_id, since_seq)]


class ApprovalBody(BaseModel):
    """审批/拒绝工具调用请求体。

    params_hash 是必填项：审批附着于具体参数与资源，审批人必须回传卡片上展示的那一份参数指纹，
    否则无从判断"批的到底是哪一组参数"。
    """

    call_id: str  # 待审批的工具调用标识
    params_hash: str  # 审批时卡片上展示的参数指纹，必须与闸口决策事件中记录的一致
    ttl_seconds: int = 900  # 审批有效期（秒），超时后该批准失效，必须重新审批
    comment: str = ""  # 审批意见


async def _check_pending_approval(s: AsyncSession, run_id: str, body: ApprovalBody, request: Request, principal: Principal) -> EventLog:
    """审批 / 拒绝的共同前置校验：Run 存在（否则 404）、处于 auth-required，且回传的参数指纹
    与该调用最近一次闸口决策记录的一致（否则 409）。返回绑定到同一会话的 EventLog。"""
    run = await s.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"run '{run_id}' not found")
    if run.state != RunState.AUTH_REQUIRED:
        raise HTTPException(409, "run is not awaiting approval")
    check_approver(request, principal, run.subject)  # 强制鉴权时：只有人员能审批，且不能审批自己发起的 Run
    log = EventLog(s)
    decided = [
        e.payload.get("params_hash")
        for e in await log.list(run_id)
        if e.type == RunEventType.TOOL_DECISION and e.payload.get("call_id") == body.call_id
    ]
    if not decided or decided[-1] != body.params_hash:
        # 参数指纹对不上：说明待审批的参数已经变化（或审批人拿的是过期卡片），必须重新走审批。
        raise HTTPException(409, "params_hash does not match the pending call; re-approval required（参数已变化，需重新审批）")
    return log


@router.post(
    "/v1/runs/{run_id}/approve",
    response_model=ApprovalView,
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
    dependencies=[Depends(run_in_tenant)],
)
async def approve(run_id: str, body: ApprovalBody, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """批准一次挂起中的高风险工具调用：仅在 Run 处于 auth-required 状态时允许，
    记录 TOOL_APPROVED 事件并把状态迁回 working，随后需调用 /resume 才能真正继续执行。

    批准会绑定"参数指纹 + 有效期"：参数一旦变化或审批超时，闸口不会放行，必须重新审批。"""
    async with SessionLocal() as s:
        log = await _check_pending_approval(s, run_id, body, request, principal)
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


@router.post(
    "/v1/runs/{run_id}/reject",
    response_model=RunStateView,
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
    dependencies=[Depends(run_in_tenant)],
)
async def reject(run_id: str, body: ApprovalBody, request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """拒绝一次挂起中的高风险工具调用：记录 TOOL_REJECTED 事件并将 Run 直接置为 failed。
    与 approve 同样要求 Run 处于 auth-required 且参数指纹匹配，否则 409；Run 不存在 404。"""
    async with SessionLocal() as s:
        log = await _check_pending_approval(s, run_id, body, request, principal)
        await log.append(
            run_id,
            RunEventType.TOOL_REJECTED,
            {"call_id": body.call_id, "approver": principal.subject, "comment": body.comment, "params_hash": body.params_hash},
        )
        await log.transition(run_id, RunState.FAILED, "rejected")
        await s.commit()
    return {"run_id": run_id, "state": RunState.FAILED}


class ResumeBody(BaseModel):
    """续跑请求体：messages: the original initial messages (clients keep them; workers read from session store)
    （客户端需要回传最初的输入消息用于重建历史；生产环境中 worker 也可以改为从会话存储读取。）"""

    messages: list[dict[str, Any]]


@router.post(
    "/v1/runs/{run_id}/resume",
    response_model=None,  # 200 为 SSE 统一事件流（data 帧 schema 见 x-sse-event-schema），不做 JSON 响应建模
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
    dependencies=[Depends(run_in_tenant)],
)
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
    agent, _ = await _load_agent(run.agent_id, run.agent_version, principal.tenant_id, allow_unpublished=True)  # 续跑固定原版本

    async def stream() -> AsyncIterator[dict[str, str]]:
        # 续跑仍以 Run 发起人的身份执行（闸口判定、Trace 归属不随续跑者变化）；续跑者本人记在请求审计里
        async for ev in runner.drive(run_id, agent, run.subject, body.messages, credential=principal.token):
            if _client_visible(ev):
                yield _sse(ev)

    return EventSourceResponse(stream())


@router.post(
    "/v1/runs/{run_id}/cancel",
    response_model=RunStateView,
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
    dependencies=[Depends(run_in_tenant)],
)
async def cancel(run_id: str) -> dict[str, Any]:
    """取消一个 Run；Run 不存在返回 404（RunNotFound 由应用级异常处理器映射），
    当前状态不允许迁移到 canceled（如已是终态）返回 409。"""
    async with SessionLocal() as s:
        log = EventLog(s)
        try:
            await log.transition(run_id, RunState.CANCELED, "user")
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        await s.commit()
    # 迁移提交后，若该 Run 正在本进程驱动（登记在 loop.active），再中断其 harness 会话：阻塞中的模型轮次
    # 尽快收尾，runner 随后发现 Run 已是 canceled，以 done(cancelled) 结束流而不再做任何状态迁移。
    running = active.get(run_id)
    if running is not None:
        harness, session = running
        await active.interrupt_quietly(harness, session, run_id)
    return {"run_id": run_id, "state": RunState.CANCELED}


_observe_transport: httpx.AsyncBaseTransport | None = None  # 测试注入：观测适配器请求经该 transport 处理


def set_observe_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    global _observe_transport
    _observe_transport = transport


class FeedbackBody(BaseModel):
    """对一次 Run 的用户反馈：点赞 +1 / 点踩 -1，可附说明或修正后的正确答案（可沉淀为评测样本）。"""

    score: float = Field(ge=-1, le=1)
    comment: str = Field(default="", max_length=2000)
    correction: str | None = Field(default=None, max_length=8000)


@router.post(
    "/v1/runs/{run_id}/feedback",
    responses={403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 502: {"model": ErrorDetail}},
    dependencies=[Depends(run_in_tenant)],
)
async def feedback(run_id: str, body: FeedbackBody, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """反馈（S3.5）：只有 Run 发起人能反馈；trace_id 由 Runtime 从 t_run 取，调用方无法把评分挂到任意 Trace 上。
    经观测适配器写成 Langfuse score（user_feedback），挂在该 Run 最近一次执行的 Trace 上。"""
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
    assert run is not None  # run_in_tenant 已保证存在
    if run.subject != principal.subject:
        raise HTTPException(403, "only the run initiator can give feedback")
    if not run.trace_id:
        raise HTTPException(409, "run has no trace yet")
    payload = {"trace_id": run.trace_id, "run_id": run_id, "subject": principal.subject} | body.model_dump()
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=_observe_transport) as c:
            r = await c.post(f"{settings.observe_adapter_url.rstrip('/')}/feedback", json=payload)
        forwarded = r.status_code < 300 and bool(r.json().get("forwarded"))
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, f"observe adapter unavailable: {type(e).__name__}") from e
    if not forwarded:
        raise HTTPException(502, "feedback was not forwarded to Langfuse")
    return {"run_id": run_id, "trace_id": run.trace_id, "forwarded": True}


def _dumps(o: Any) -> str:
    """带非 ASCII 直出（ensure_ascii=False）的 JSON 序列化辅助函数，避免中文被转义。"""
    return json.dumps(o, ensure_ascii=False)
