"""Run driver: creates the Run, assembles context, drives the harness turn loop, routes tool calls
through the gate, executes tools, appends every step to the event log and yields unified events.

Resume (checkpoint via replay): a Run whose log holds a TOOL_REQUESTED without a TOOL_RESULT is
continued by first completing that call (if approved), then handing the tool result back to the
harness — the harness is never asked to re-decide an already recorded call.

（Run 驱动器：创建 Run、组装上下文、驱动 harness 的模型轮次循环、把工具调用交给闸口判定、
执行工具、把每一步都追加写入事件日志，并把统一事件流 yield 给上层调用方（API 层）。

续跑（通过重放实现 checkpoint）：若某个 Run 的事件日志里存在一条 TOOL_REQUESTED 但还没有
对应的 TOOL_RESULT，说明上次执行在该工具调用处被挂起（如等待审批）；续跑时先补完这个挂起
调用（若已批准），再把工具结果交还给 harness 继续——harness 永远不会被要求对一个已经记录在
案的调用重新做决策，这保证了幂等与可重放性。）

本模块自身不执行任何工具：所有工具副作用统一经由 loop.toolbroker.handle_tool 产生。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from ai_contract import (
    CardEvent,
    CardType,
    DoneEvent,
    ErrorEvent,
    Event,
    HookPoint,
    InputRequiredEvent,
    RunEvent,
    RunEventType,
    RunState,
    SessionHandle,
    SessionSpec,
    TextEvent,
    ThinkingEvent,
    ThinkingVisibility,
    ToolResultEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)
from runtime.assemble.assembler import assemble
from runtime.config import settings
from runtime.context import budget as ctx
from runtime.guard.gate import GateContext
from runtime.guard.hooks import HookChain, HookConfigError, HookOutcome
from runtime.harness import registry
from runtime.loop import active
from runtime.loop.toolbroker import RunCtx, handle_tool, hmac_json, record_unknown_result, taint_of
from runtime.observe import tracing
from runtime.run import invariant
from runtime.state.db import AgentVersion, Run, SessionLocal
from runtime.state.eventlog import EventLog

MAX_TOOL_ROUNDS = 8  # 单次 drive 调用内允许的最大模型-工具往返轮数，超出则判为失败，避免死循环
# 只允许平台产出的事件类型：harness 若产出这些事件，runner 丢弃并记 HARNESS_FORGED_PLATFORM_EVENT。
PLATFORM_ONLY_EVENTS = (ToolResultEvent, CardEvent, InputRequiredEvent)


def thinking_visibility(agent: AgentVersion) -> ThinkingVisibility:
    """读取 Agent 版本配置中的 thinking_visibility；缺省或取值非法时按 internal 处理（失败关闭）。"""
    value = (agent.config or {}).get("thinking_visibility", "internal")
    return value if value in ("internal", "summary", "full") else "internal"


async def create_run(
    agent: AgentVersion,
    subject: str,
    session_id: str | None,
    actor: str | None = None,
    rollout: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    initial_messages: list[dict[str, Any]] | None = None,
) -> str:
    """创建一个新 Run：写入 t_run 行并追加 RUN_CREATED 事件（seq=1），返回新生成的 run_id。
    actor 为代为执行的 Agent / 应用（来自令牌的 act 声明或签发客户端），rollout 为选版原因（灰度），均随 RUN_CREATED 进事件日志。

    attachments 为本 Run 附件的元信息 `[{"artifact_id", "source_run_id", "data_class"}]`（D88）：原样写入 RUN_CREATED 的
    `attachments`，并据此写 `inherited_taint`——任一附件为 confidential（或分级缺失 / 无法识别，失败关闭）即 "confidential"，
    否则 None；该污点在本 Run 首次驱动与之后每次续跑时由 `_replayed_taint` 读取。默认 None 时不写这两个键（日志不变）。

    initial_messages 为首次驱动将使用的初始消息（含附件说明消息）：其指纹写入 RUN_CREATED 的 `initial_messages_hash`，
    把初始消息与事件日志绑定——续跑回传的初始消息必须与之一致（API 层 409，drive 兜底 INVARIANT_VIOLATION）。
    默认 None 时不写该键。"""
    run_id = "run_" + uuid.uuid4().hex[:20]
    created: dict[str, Any] = {
        "agent_id": agent.agent_id,
        "agent_version": agent.version,
        "model_alias": agent.model_alias,
        "subject": subject,
        "actor": actor,
        "rollout": rollout,
    }
    if attachments is not None:
        created["attachments"] = attachments
        created["inherited_taint"] = "confidential" if any(a.get("data_class") not in ("public", "internal") for a in attachments) else None
    if initial_messages is not None:
        created[invariant.INITIAL_MESSAGES_HASH] = invariant.message_hash(initial_messages)
    async with SessionLocal() as s:
        s.add(
            Run(
                run_id=run_id,
                tenant_id=agent.tenant_id,
                agent_id=agent.agent_id,
                agent_version=agent.version,
                model_alias=agent.model_alias,
                subject=subject,
                session_id=session_id,
            )
        )
        await s.flush()
        await EventLog(s).append(run_id, RunEventType.RUN_CREATED, created)
        await s.commit()
    return run_id


async def drive(
    run_id: str,
    agent: AgentVersion,
    subject: str,
    messages: list[dict[str, Any]],
    credential: str | None = None,
    credential_subject: str | None = None,
) -> AsyncIterator[Event]:
    """驱动一个 Run 的执行（首次执行或续跑均走此函数）：
    1) 组装上下文与工具集，构造 SessionSpec；
    2) 读取事件日志判断是应该 open_session 还是 resume（重放）；若存在未完成的挂起调用，先补完；
    3) 进入模型-工具往返循环，每轮先做不变量校验，再驱动一次 harness 轮次，按事件类型分流处理；
    4) 模型不再请求工具即视为完成；工具调用则交给 toolbroker.handle_tool 处理，挂起则提前返回；
    5) 超过 MAX_TOOL_ROUNDS 轮未结束则判为失败。

    观测：每次 drive 调用生成一条 OTel Trace（根 span `run {agent_id}`，退出时无论正常/异常都结束）。
    续跑策略：续跑**新建** Trace，并在根 span 属性 `ai.resumed_from_trace_id` 记录上一条 Trace；
    同一 Run 的各条 Trace 以 `langfuse.session.id=run_id` 归入 Langfuse 同一 Session。t_run.trace_id
    始终指向最近一次 drive 的 Trace，与本次下发的 DoneEvent.trace_id 一致。
    本包装层只给 DoneEvent 补 trace_id 并记录停止原因，事件的种类、顺序与消费语义与 _drive 完全一致。

    credential_subject 为 credential（执行工具所用令牌）所属主体（D87）。非 None 且与 Run 发起主体（subject）不同时，
    记入 TOOL_EXECUTING 事件与工具审计；默认 None 时行为与此前完全一致。D87 修订后续跑只允许发起人，经 API 传入的
    credential_subject 恒等于发起主体，因此不会写入；机制保留。
    """
    with tracing.run_span(run_id, agent, subject, agent.model_alias) as rs:
        async with aclosing(_drive(run_id, agent, subject, messages, rs, credential, credential_subject)) as events:
            async for ev in events:
                if isinstance(ev, DoneEvent):
                    ev = ev.model_copy(update={"trace_id": rs.trace_id})
                    tracing.record_stop(rs.span, ev.stop_reason)
                yield ev


async def _bind_trace(s: AsyncSession, run_id: str, rs: tracing.RunSpan) -> None:
    """在调用方事务内把本次 Trace 写入 t_run.trace_id；若此前已有不同的 trace_id（续跑），记到根 span 上。"""
    run = await s.get(Run, run_id)
    if run is None:
        return  # Run 不存在：交给随后的 EventLog 操作抛 RunNotFound
    if run.trace_id and run.trace_id != rs.trace_id:
        rs.span.set_attribute("ai.resumed_from_trace_id", run.trace_id)
    run.trace_id = rs.trace_id


async def _tool_requested_seq(run_id: str, call_id: str) -> int | None:
    """从事件日志查该调用 TOOL_REQUESTED 事件的 seq（工具 span 用于与事件日志对齐）。"""
    async with SessionLocal() as s:
        log = await EventLog(s).list(run_id)
    return next((e.seq for e in log if e.type == RunEventType.TOOL_REQUESTED and e.payload.get("call_id") == call_id), None)


async def _stop_if_canceled(run_id: str) -> bool:
    """在新会话里读取 Run 的当前状态：若已被取消 API 抢先置为 canceled（终态），追加 RUN_DONE(cancelled)
    并返回 True——调用方应停止循环、下发 done(cancelled) 后返回；终态不允许任何迁移，继续执行只会在
    下一次状态迁移时抛 ValueError。未被取消（或 Run 不存在，交给后续 EventLog 操作抛 RunNotFound）返回 False。"""
    async with SessionLocal() as s:
        run = await s.get(Run, run_id)
        if run is None or run.state != RunState.CANCELED:
            return False
        await EventLog(s).append(run_id, RunEventType.RUN_DONE, {"stop_reason": "cancelled"})
        await s.commit()
    return True


def _replayed_taint(prior: list[RunEvent], tools_by_name: dict[str, ToolSpec]) -> set[str]:
    """从事件日志重建本 Run 的数据分类污点（ADR-0010，D88/D89）：RUN_CREATED 的 inherited_taint（附件继承），
    加上每个 TOOL_RESULT 按 `taint_of(status, data_class)` 计出的污点（与工具代理实时传播同一口径：ok 与 error 都计）。
    数据分类优先取 TOOL_RESULT 自身记录的 data_class（含 post_tool hook 的升级）；旧事件没有该字段时，
    按其 TOOL_REQUESTED 的工具名回退到当前工具表。"""
    requested = {e.payload["call_id"]: e.payload.get("tool") for e in prior if e.type == RunEventType.TOOL_REQUESTED}
    taint: set[str] = set()
    for e in prior:
        if e.type == RunEventType.RUN_CREATED and e.payload.get("inherited_taint") == "confidential":
            taint.add("confidential")
        if e.type != RunEventType.TOOL_RESULT:
            continue
        data_class = e.payload.get("data_class")
        if data_class is None:
            tool = tools_by_name.get(requested.get(e.payload["call_id"]) or "")
            data_class = tool.data_class if tool else None
        taint |= taint_of(e.payload.get("status"), data_class)
    return taint


_TOOL_STATUSES = frozenset({"ok", "error", "denied", "pending_approval"})


def _tool_statuses(prior: list[RunEvent], history: list[dict[str, Any]]) -> dict[str, Any]:
    """TurnInput.tool_statuses（Contract V1.1）：本轮历史中各工具结果的处理状态，取自事件日志的 TOOL_RESULT。
    适配器据此把 denied / error 结果以错误形式交给模型（如 claude-sdk 的 tool_result.is_error）；无工具结果时为空。"""
    in_history = {m.get("call_id") for m in history if m.get("role") == "tool"}
    return {
        e.payload["call_id"]: e.payload["status"]
        for e in prior
        if e.type == RunEventType.TOOL_RESULT and e.payload.get("call_id") in in_history and e.payload.get("status") in _TOOL_STATUSES
    }


def _pre_model_payload(prior: list[RunEvent], agent: AgentVersion, messages_hash: str, rc: RunCtx) -> dict[str, Any]:
    """pre_model hook 的负载：轮次与累计用量都从事件日志计算（续跑前后一致），不含消息原文。"""
    usage = {"input_tokens": 0, "output_tokens": 0}
    for e in prior:
        if e.type == RunEventType.MODEL_RESPONSE:
            u = e.payload.get("usage") or {}
            usage["input_tokens"] += int(u.get("input_tokens") or 0)
            usage["output_tokens"] += int(u.get("output_tokens") or 0)
    return {
        "round": sum(1 for e in prior if e.type == RunEventType.MODEL_REQUEST) + 1,
        "model_alias": agent.model_alias,
        "n_messages": len(rc.history),
        "messages_hash": messages_hash,
        "taint": sorted(rc.gate.taint),
        "usage": usage | {"total_tokens": usage["input_tokens"] + usage["output_tokens"]},
    }


async def _fail(run_id: str, code: str, message: str, reason: str, **extra: Any) -> None:
    """记一条 RUN_ERROR 并把 Run 迁移到 failed（hook 拒绝 / 配置不合规等平台侧失败）。"""
    async with SessionLocal() as s:
        log = EventLog(s)
        await log.append(run_id, RunEventType.RUN_ERROR, {"code": code, "message": message} | extra)
        await log.transition(run_id, RunState.FAILED, reason)
        await s.commit()


async def _drive(
    run_id: str,
    agent: AgentVersion,
    subject: str,
    messages: list[dict[str, Any]],
    rs: tracing.RunSpan,
    credential: str | None = None,
    credential_subject: str | None = None,
) -> AsyncGenerator[Event, None]:
    """drive() 的实际执行体（步骤见 drive 的说明）；rs 为本次 drive 的根 span，模型轮次与工具调用的子 span 挂在其下。

    会话生命周期：harness 会话打开（或重放恢复）后登记到进程内活动 Run 注册表（loop.active），供取消 API
    中断；本次 drive 无论以何种方式结束（正常结束、挂起等待审批、出错、超过轮数上限、客户端断开），
    都在 finally 中注销并调用 harness.interrupt——drive 返回后不会再复用这个会话（续跑总是新开/重放会话），
    对 claude-sdk 而言这一步会结束后台 CLI 子进程，避免会话泄漏。
    """
    harness = registry.get(agent.harness)
    assembled = await assemble(agent, subject)
    spec = SessionSpec(
        run_id=run_id,
        agent_id=agent.agent_id,
        agent_version=agent.version,
        model_alias=agent.model_alias,
        system_prompt=assembled.system_prompt,
        subject=subject,
        tenant_id=agent.tenant_id,
        workspace_dir=f"{settings.workspace_dir}/{run_id}",
    )
    initial_messages = list(messages)
    visibility = thinking_visibility(agent)
    rc = RunCtx(
        credential=credential,  # 终端用户令牌：工具经能力网关执行时换取 OBO 令牌，只在内存中
        credential_subject=credential_subject,
        run_id=run_id,
        agent=agent,
        spec=spec,
        # 只暴露组装结果中的工具：未在 Agent 快照里声明的工具不得执行，
        # 否则 admin 发布时的能力等级闸门可被绕过（声明只读、执行期却拿到高风险写）。
        tools_by_name={t.name: t for t in assembled.tools},
        gate=GateContext(
            subject=subject,
            agent_id=agent.agent_id,
            tenant_id=agent.tenant_id,
            deny_tools=set((agent.config or {}).get("redlines", {}).get("deny_tools", [])),
        ),
        history=list(messages),
        workspace=spec.workspace_dir or ".",
    )

    # 驱动开始前 Run 已被取消（如后台 Run 刚创建即被取消）：不再打开会话，直接以 cancelled 收尾。
    if await _stop_if_canceled(run_id):
        yield DoneEvent(run_id=run_id, stop_reason="cancelled")
        return

    # Hooks（S5）：平台在每轮边界执行 pre_model / post_model，工具代理执行 pre_tool / post_tool。快照中的 hooks
    # 配置发布时已校验；运行期仍不合规（如绕过发布）则失败关闭，不在没有 hook 的情况下继续跑。
    try:
        chain = HookChain.for_agent(agent.config, visibility)
    except HookConfigError as exc:
        await _fail(run_id, "HOOK_CONFIG_INVALID", str(exc), "hook config")
        yield ErrorEvent(code="HOOK_CONFIG_INVALID", message=str(exc))
        yield DoneEvent(run_id=run_id, stop_reason="error")
        return
    rc.hooks = chain

    # ---- unknown tool result window ----
    # （结果未知窗口：日志里有某个 call_id 的 TOOL_EXECUTING，却没有对应的 TOOL_EXECUTED 与
    # TOOL_RESULT，说明工具已经开始执行但结果未落盘——它到底有没有产生副作用无从判断。
    # 这种情况下**禁止盲目重试**：必须转人工确认或与下游系统对账，因此把 Run 迁移到
    # input-required 并推一张对账卡片给前端，由人来决定补做还是跳过。）
    unknown: dict[str, Any] | None = None
    mismatch: str | None = None  # 初始消息与 RUN_CREATED 记录的指纹不一致
    async with SessionLocal() as s:
        log = EventLog(s)
        prior = await log.list(run_id)
        try:
            # 初始消息与事件日志绑定：API 层续跑已先行比对（不一致 409）；此处兜底直接调用 drive 的路径
            invariant.check_initial(prior, initial_messages)
        except invariant.InvariantViolation as exc:
            mismatch = str(exc)
        settled = {e.payload["call_id"] for e in prior if e.type in (RunEventType.TOOL_EXECUTED, RunEventType.TOOL_RESULT)}
        for e in prior:
            if e.type == RunEventType.TOOL_EXECUTING and e.payload["call_id"] not in settled:
                unknown = {
                    "call_id": e.payload["call_id"],
                    "tool": e.payload.get("tool"),
                    "operation_id": e.payload.get("operation_id"),
                }
        if unknown is not None and mismatch is None:
            await _bind_trace(s, run_id, rs)
            await record_unknown_result(log, run_id, unknown)
            await s.commit()
    if mismatch is not None:
        await _fail(run_id, "INVARIANT_VIOLATION", mismatch, "invariant")
        yield ErrorEvent(code="INVARIANT_VIOLATION", message=mismatch)
        yield DoneEvent(run_id=run_id, stop_reason="error")
        return
    if unknown is not None:
        yield CardEvent(card_type=CardType.RECONCILIATION_REQUIRED, payload=unknown)
        yield DoneEvent(run_id=run_id, stop_reason="awaiting")
        return

    session: SessionHandle | None = None  # 本次 drive 打开的 harness 会话；finally 据此注销并中断
    try:
        # ---- open or resume the session from the event log ----
        # （从事件日志判断本次是新开会话还是需要重放续跑；同时找出可能存在的挂起中工具调用。）
        pending_call: ToolUseEvent | None = None
        async with SessionLocal() as s:
            log = EventLog(s)
            prior = await log.list(run_id)
            # 审批绑定信息随重放一并恢复：闸口放行还要再校验参数指纹与有效期，光有 call_id 不够。
            rc.gate.approved_call_ids = {
                e.payload["call_id"]: {"params_hash": e.payload.get("params_hash"), "expires_at": e.payload.get("expires_at")}
                for e in prior
                if e.type == RunEventType.TOOL_APPROVED
            }
            # 污点随重放一并恢复（ADR-0010）：否则续跑后的模型轮次会把机密内容原文写进 Trace，
            # 外发类工具也不再因 confidential 污点被收紧为需审批。
            rc.gate.taint |= _replayed_taint(prior, rc.tools_by_name)
            resulted = {e.payload["call_id"] for e in prior if e.type == RunEventType.TOOL_RESULT}
            for e in prior:
                # 找到"已请求但尚无结果"的工具调用：这是上次执行被挂起（等待审批）的位置，
                # 续跑时需要先完成它，而不是让 harness 重新决策。
                if e.type == RunEventType.TOOL_REQUESTED and e.payload["call_id"] not in resulted:
                    # pre_tool 改写过入参时按 effective_input 续跑（审批指纹也是按它算的）；重放不重跑 hook。
                    effective = e.payload["effective_input"] if "effective_input" in e.payload else e.payload.get("input", {})
                    pending_call = ToolUseEvent(call_id=e.payload["call_id"], tool_name=e.payload["tool"], input=effective)
            if any(e.type in (RunEventType.TOOL_REQUESTED, RunEventType.MODEL_RESPONSE) for e in prior):
                # 事件日志中已有过模型响应或工具请求，说明这是续跑：先按日志重建消息历史，
                # 再让 harness 基于重放后的统一事件流重建会话（checkpoint via replay）。
                rc.history = invariant.rebuild_messages(prior, initial_messages)
                session = await harness.resume(spec, EventLog.project_unified_events(prior))
            else:
                session = await harness.open_session(spec)
            active.register(run_id, harness, session)
            await _bind_trace(s, run_id, rs)
            await log.transition(run_id, RunState.WORKING, "start")
            plan = ctx.assemble(
                {
                    "system": spec.system_prompt,
                    "skills": "\n".join(x.name for x in assembled.skills),
                    "tools": json.dumps([t.name for t in assembled.tools]),
                    "history": json.dumps(rc.history, ensure_ascii=False),
                }
            )
            await log.append(run_id, RunEventType.CONTEXT_ASSEMBLED, plan.report() | assembled.report)
            await s.commit()

        if pending_call is not None:  # complete the interrupted (now approved) call before asking the harness again
            # （先补完此前被打断（现已获批准）的工具调用，再重新向 harness 请求下一轮。
            # already_logged=True：TOOL_REQUESTED（含 reasoning）与历史中的 tool_use 消息均已由重放恢复，不重复写。）
            with tracing.tool_span(pending_call.tool_name, pending_call.call_id, parent=rs) as tspan:
                async for ev in handle_tool(rc, pending_call, already_logged=True):
                    tracing.record_tool_event(tspan, ev)
                    yield ev
                tracing.record_tool_requested_seq(tspan, await _tool_requested_seq(run_id, pending_call.call_id))
                tracing.record_hooks(tspan, rc.hook_records)
            if rc.suspended:
                return

        # 传给适配器的 HookSet 只含它声明的 internal_hook_points（平台看不到的点），由同一条 hook 链包装而成；
        # 适配器侧的 hook 结论随本轮 MODEL_RESPONSE 的 hooks 记录（source=adapter）。
        adapter_hook_records: list[dict[str, Any]] = []
        hooks = chain.hookset(harness.capabilities().internal_hook_points, adapter_hook_records)
        for _round in range(MAX_TOOL_ROUNDS):
            # 每轮开始前检查 Run 是否已被取消 API 抢先置为 canceled（例如上一轮工具执行期间被取消）。
            if await _stop_if_canceled(run_id):
                yield DoneEvent(run_id=run_id, stop_reason="cancelled")
                return
            # 数据分级矩阵（S4.9）：Run 已带 confidential 污点而模型不在机密数据允许清单内——不再把上下文发给该模型，
            # 以失败收尾（宁可中断，不让机密数据流向未授权的模型）。
            if "confidential" in rc.gate.taint and agent.model_alias not in settings.confidential_model_aliases:
                msg = f"model '{agent.model_alias}' is not authorized for confidential data"
                async with SessionLocal() as s:
                    log = EventLog(s)
                    await log.append(run_id, RunEventType.RUN_ERROR, {"code": "DATA_CLASS_MODEL_BLOCKED", "message": msg})
                    await log.transition(run_id, RunState.FAILED, "data_class")
                    await s.commit()
                yield ErrorEvent(code="DATA_CLASS_MODEL_BLOCKED", message=msg)
                yield DoneEvent(run_id=run_id, stop_reason="error")
                return
            pre_model: HookOutcome | None = None
            async with SessionLocal() as s:
                log = EventLog(s)
                prior_log = await log.list(run_id)
                try:
                    # 不变量校验：确认即将发给模型的 rc.history 与"从事件日志重建的历史"完全一致，
                    # 防止上下文被意外篡改或与事件日志脱节（保证可重放性/审计一致性）。
                    h = invariant.check(prior_log, initial_messages, rc.history)
                except invariant.InvariantViolation as e:
                    await log.append(run_id, RunEventType.RUN_ERROR, {"code": "INVARIANT_VIOLATION", "message": str(e)})
                    await log.transition(run_id, RunState.FAILED, "invariant")
                    await s.commit()
                    yield ErrorEvent(code="INVARIANT_VIOLATION", message=str(e))
                    yield DoneEvent(run_id=run_id, stop_reason="error")
                    return
                round_no = sum(1 for e in prior_log if e.type == RunEventType.MODEL_REQUEST) + 1
                request_payload: dict[str, Any] = {"model_alias": agent.model_alias, "messages_hash": h, "n_messages": len(rc.history)}
                if chain.has(HookPoint.PRE_MODEL):
                    # pre_model 在每轮边界由平台执行（对所有适配器）；结论随 MODEL_REQUEST 落日志，续跑不重跑。
                    pre_model = await chain.run(HookPoint.PRE_MODEL, _pre_model_payload(prior_log, agent, h, rc))
                    request_payload["hooks"] = pre_model.records
                model_request = await log.append(run_id, RunEventType.MODEL_REQUEST, request_payload)
                await s.commit()
            if pre_model is not None and pre_model.denied:
                msg = f"pre_model hook '{pre_model.denied_by}' denied: {pre_model.reason}"
                await _fail(run_id, "HOOK_DENIED", msg, "hook denied", hook=pre_model.denied_by, point=HookPoint.PRE_MODEL.value)
                tracing.record_hooks(rs.span, pre_model.records)
                yield ErrorEvent(code="HOOK_DENIED", message=msg)
                yield DoneEvent(run_id=run_id, stop_reason="error")
                return

            tool_call: ToolUseEvent | None = None
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            harness_stop: str = "end_turn"  # harness 本轮的停止原因（DoneEvent.stop_reason），决定 Run 终态
            harness_error: ErrorEvent | None = None  # harness 本轮产出的最后一个错误事件
            harness_exc: Exception | None = None  # 消费 run_turn 时抛出的非预期异常
            post_denied: HookOutcome | None = None  # post_model 拒绝：本轮输出不进历史，Run 判为失败
            prompt = tracing.last_user_text(rc.history)  # 本轮提示词取发给模型前历史里的最后一条用户消息
            # 配置了 post_model 时，本轮要下发给客户端的文本 / 思维 / 工具调用事件先按原顺序缓冲，post_model 通过后
            # （UsageEvent 处）再下发；拒绝则一律丢弃——否则被拒的文本已实时到达客户端。未配置时保持流式。
            held: list[Event] | None = [] if chain.has(HookPoint.POST_MODEL) else None
            with tracing.model_span(agent.model_alias, _round, parent=rs, model_request_seq=model_request.seq) as mspan:
                if pre_model is not None:
                    tracing.record_hooks(mspan, pre_model.records)
                # aclosing：无论正常结束、break、异常还是本生成器被关闭，都确定性地关闭 harness 的轮次生成器，
                # 不把清理留给 GC。协议声明的返回类型是 AsyncIterator，各适配器实际均为异步生成器（带 aclose）。
                turn = cast(
                    AsyncGenerator[Event, None],
                    harness.run_turn(
                        session,
                        TurnInput(messages=rc.history, tool_statuses=_tool_statuses(prior_log, rc.history)),
                        assembled.tools,
                        assembled.skills,
                        hooks,
                    ),
                )
                try:
                    async with aclosing(turn) as turn_events:
                        async for ev in turn_events:
                            if isinstance(ev, TextEvent):
                                text_parts.append(ev.delta)
                                if held is not None:
                                    held.append(ev)
                                else:
                                    yield ev
                            elif isinstance(ev, ThinkingEvent):
                                # 思维链一律随 MODEL_RESPONSE 进事件日志；可见性以 Agent 版本配置为准覆写，
                                # 是否下发给客户端由 API 层按 visibility 决定（internal 不下发）。
                                # summary（D4）：原始思维增量不下发，本轮结束时经 post_model 生成摘要后下发一条。
                                thinking_parts.append(ev.delta)
                                if visibility != "summary":
                                    shown = ev.model_copy(update={"visibility": visibility})
                                    if held is not None:
                                        held.append(shown)
                                    else:
                                        yield shown
                            elif isinstance(ev, ToolUseEvent):
                                tool_call = ev
                                if held is not None:
                                    held.append(ev)
                                else:
                                    yield ev
                            elif isinstance(ev, PLATFORM_ONLY_EVENTS):
                                # 工具结果、卡片、补充输入请求只能由平台（工具代理 / runner）产出：harness 产出的一律丢弃，
                                # 不转发客户端、不进模型历史，只记一条错误供审计，Run 继续执行。
                                async with SessionLocal() as s:
                                    await EventLog(s).append(
                                        run_id,
                                        RunEventType.RUN_ERROR,
                                        {
                                            "code": "HARNESS_FORGED_PLATFORM_EVENT",
                                            "message": f"harness emitted platform-only event '{ev.type}'; dropped",
                                            "event_type": ev.type.value,
                                        },
                                    )
                                    await s.commit()
                            elif isinstance(ev, UsageEvent):
                                # 收到用量事件视为本轮模型输出结束：落盘 MODEL_RESPONSE 事件，并把完整文本追加进历史。
                                payload: dict[str, Any] = {
                                    "text": "".join(text_parts),
                                    "usage": ev.model_dump(),
                                    "prompt_cache": tracing.prompt_cache_stats(ev, plan.stable_prefix_hash),
                                }
                                if thinking_parts:
                                    payload["thinking"] = "".join(thinking_parts)
                                summary: str | None = None
                                hook_records: list[dict[str, Any]] = []
                                if chain.has(HookPoint.POST_MODEL):
                                    # post_model：结论与思维链摘要随 MODEL_RESPONSE 落日志（重放读日志，不重跑）。
                                    post = await chain.run(
                                        HookPoint.POST_MODEL,
                                        {
                                            "round": round_no,
                                            "model_alias": agent.model_alias,
                                            "text": payload["text"],
                                            "thinking": payload.get("thinking", ""),
                                            "tool_call": tool_call.model_dump(mode="json") if tool_call else None,
                                            "usage": ev.model_dump(mode="json"),
                                            "taint": sorted(rc.gate.taint),
                                        },
                                    )
                                    hook_records += post.records
                                    if post.denied:
                                        post_denied = post
                                        if payload["text"]:
                                            # 本轮输出不进历史：文本不入可重建历史，事件日志只留带密钥的指纹（同 raw_output_hmac）
                                            payload["withheld_text_hmac"] = hmac_json(payload["text"])
                                            payload["text"] = ""
                                    elif isinstance(post.overrides.get("thinking_summary"), str) and post.overrides["thinking_summary"]:
                                        summary = post.overrides["thinking_summary"]
                                        payload["thinking_summary"] = summary
                                hook_records += adapter_hook_records
                                adapter_hook_records.clear()
                                if hook_records:
                                    payload["hooks"] = hook_records
                                    tracing.record_hooks(mspan, hook_records)
                                async with SessionLocal() as s:
                                    model_response = await EventLog(s).append(run_id, RunEventType.MODEL_RESPONSE, payload)
                                    await s.commit()
                                tracing.record_usage(mspan, ev, model_response.seq, plan.stable_prefix_hash)
                                if text_parts and post_denied is None:
                                    rc.history.append({"role": "assistant", "content": "".join(text_parts)})
                                if held:
                                    if post_denied is None:
                                        for held_ev in held:
                                            yield held_ev
                                    held.clear()
                                if summary is not None and visibility == "summary":
                                    yield ThinkingEvent(delta=summary, visibility="summary")
                                yield ev
                            elif isinstance(ev, ErrorEvent):
                                # harness 的错误（模型网关失败、SDK 异常等）：进事件日志供审计，并转发客户端；
                                # 是否把 Run 判为失败由随后的 DoneEvent.stop_reason 决定。
                                harness_error = ev
                                async with SessionLocal() as s:
                                    await EventLog(s).append(
                                        run_id, RunEventType.RUN_ERROR, {"code": ev.code, "message": ev.message, "retryable": ev.retryable}
                                    )
                                    await s.commit()
                                yield ev
                            elif isinstance(ev, DoneEvent):
                                harness_stop = ev.stop_reason
                                mspan.set_attribute("gen_ai.response.finish_reasons", [ev.stop_reason])
                                break
                            else:
                                yield ev
                except Exception as exc:
                    # harness 抛出非预期异常（而不是以 ErrorEvent + done(error) 收尾）：先记下，循环外统一判为失败，
                    # 否则 Run 会卡在 working。只捕获 Exception：GeneratorExit（客户端断开）与 CancelledError 照常上抛。
                    harness_exc = exc
                    tracing.mark_error(mspan, exc)
                # ADR-0010：Run 已带 confidential 污点时，模型轮次只记元数据，不记提示词/补全原文。
                tracing.record_model_content(mspan, prompt, "".join(text_parts), redact="confidential" in rc.gate.taint)

            if await _stop_if_canceled(run_id):
                # 本轮进行期间 Run 被取消 API 抢先置为 canceled（取消 API 同时中断了 harness）：
                # 终态不再迁移，直接以 cancelled 收尾，也不再执行本轮请求的工具。
                yield DoneEvent(run_id=run_id, stop_reason="cancelled")
                return
            if harness_exc is not None:
                message = f"{type(harness_exc).__name__}: {harness_exc}"
                async with SessionLocal() as s:
                    log = EventLog(s)
                    await log.append(run_id, RunEventType.RUN_ERROR, {"code": "HARNESS_EXCEPTION", "message": message})
                    await log.transition(run_id, RunState.FAILED, "harness exception")
                    await s.commit()
                yield ErrorEvent(code="HARNESS_EXCEPTION", message=message)
                yield DoneEvent(run_id=run_id, stop_reason="error")
                return
            if post_denied is not None:
                # post_model 拒绝：本轮文本未进历史、请求的工具不执行，Run 判为失败。
                msg = f"post_model hook '{post_denied.denied_by}' denied: {post_denied.reason}"
                await _fail(run_id, "HOOK_DENIED", msg, "hook denied", hook=post_denied.denied_by, point=HookPoint.POST_MODEL.value)
                yield ErrorEvent(code="HOOK_DENIED", message=msg)
                yield DoneEvent(run_id=run_id, stop_reason="error")
                return
            if harness_stop == "error" or (harness_error is not None and tool_call is None):
                # harness 以错误收尾（或报错且未请求工具）：Run 判为失败，不能落成 completed。
                code = harness_error.code if harness_error is not None else "HARNESS_ERROR"
                async with SessionLocal() as s:
                    log = EventLog(s)
                    if harness_error is None:
                        # 以 error 收尾却没产出任何 ErrorEvent：补记一条错误，保证失败的 Run 在事件日志里有据可查。
                        await log.append(
                            run_id,
                            RunEventType.RUN_ERROR,
                            {"code": "HARNESS_ERROR", "message": "harness stopped with error but emitted no error event"},
                        )
                    await log.transition(run_id, RunState.FAILED, code)
                    await s.commit()
                yield DoneEvent(run_id=run_id, stop_reason="error")
                return
            if harness_stop == "cancelled":
                # harness 被中断（interrupt）：Run 迁移到 canceled 终态。
                async with SessionLocal() as s:
                    log = EventLog(s)
                    await log.append(run_id, RunEventType.RUN_DONE, {"stop_reason": "cancelled"})
                    await log.transition(run_id, RunState.CANCELED, "interrupted")
                    await s.commit()
                yield DoneEvent(run_id=run_id, stop_reason="cancelled")
                return
            if tool_call is None:
                # 本轮模型没有请求任何工具：视为对话正常结束，Run 迁移到 completed。
                # 输出因长度上限被截断（max_tokens）时仍视为完成，但停止原因如实记录，便于客户端提示。
                stop: Literal["end_turn", "max_tokens"] = "max_tokens" if harness_stop == "max_tokens" else "end_turn"
                async with SessionLocal() as s:
                    log = EventLog(s)
                    await log.append(run_id, RunEventType.RUN_DONE, {"stop_reason": stop})
                    await log.transition(run_id, RunState.COMPLETED, stop)
                    await s.commit()
                yield DoneEvent(run_id=run_id, stop_reason=stop)
                return

            with tracing.tool_span(tool_call.tool_name, tool_call.call_id, parent=rs) as tspan:
                # 本轮的思维链随工具调用进入可重建历史（tool_use 消息的 reasoning 字段），供需要回传推理的模型使用。
                async for ev in handle_tool(rc, tool_call, reasoning="".join(thinking_parts) or None):
                    tracing.record_tool_event(tspan, ev)
                    yield ev
                tracing.record_tool_requested_seq(tspan, await _tool_requested_seq(run_id, tool_call.call_id))
                tracing.record_hooks(tspan, rc.hook_records)
            if rc.suspended:
                return

        if await _stop_if_canceled(run_id):
            yield DoneEvent(run_id=run_id, stop_reason="cancelled")
            return
        # 达到最大工具往返轮数仍未结束：判为失败，避免模型/工具无限循环拖垮资源。
        async with SessionLocal() as s:
            log = EventLog(s)
            await log.append(run_id, RunEventType.RUN_ERROR, {"code": "MAX_TOOL_ROUNDS", "message": "tool loop limit reached"})
            await log.transition(run_id, RunState.FAILED, "max_rounds")
            await s.commit()
        yield ErrorEvent(code="MAX_TOOL_ROUNDS", message="tool loop limit reached")
        yield DoneEvent(run_id=run_id, stop_reason="error")
    finally:
        if session is not None:
            active.unregister(run_id, session)
            # shield：调用方取消本任务（如 SSE 客户端断开）时，中断仍会执行完毕，不被一并取消。
            await asyncio.shield(active.interrupt_quietly(harness, session, run_id))
