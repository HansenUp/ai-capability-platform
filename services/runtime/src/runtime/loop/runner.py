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

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from ai_contract import (
    CardEvent,
    CardType,
    DoneEvent,
    ErrorEvent,
    Event,
    HookSet,
    InputRequiredEvent,
    RunEventType,
    RunState,
    SessionSpec,
    TextEvent,
    ThinkingEvent,
    ThinkingVisibility,
    ToolResultEvent,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)
from runtime.assemble.assembler import assemble
from runtime.config import settings
from runtime.context import budget as ctx
from runtime.guard.gate import GateContext
from runtime.harness import registry
from runtime.loop.toolbroker import RunCtx, handle_tool, record_unknown_result
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


async def create_run(agent: AgentVersion, subject: str, session_id: str | None) -> str:
    """创建一个新 Run：写入 t_run 行并追加 RUN_CREATED 事件（seq=1），返回新生成的 run_id。"""
    run_id = "run_" + uuid.uuid4().hex[:20]
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
        await EventLog(s).append(
            run_id,
            RunEventType.RUN_CREATED,
            {"agent_id": agent.agent_id, "agent_version": agent.version, "model_alias": agent.model_alias, "subject": subject},
        )
        await s.commit()
    return run_id


async def drive(run_id: str, agent: AgentVersion, subject: str, messages: list[dict[str, Any]]) -> AsyncIterator[Event]:
    """驱动一个 Run 的执行（首次执行或续跑均走此函数）：
    1) 组装上下文与工具集，构造 SessionSpec；
    2) 读取事件日志判断是应该 open_session 还是 resume（重放）；若存在未完成的挂起调用，先补完；
    3) 进入模型-工具往返循环，每轮先做不变量校验，再驱动一次 harness 轮次，按事件类型分流处理；
    4) 模型不再请求工具即视为完成；工具调用则交给 toolbroker.handle_tool 处理，挂起则提前返回；
    5) 超过 MAX_TOOL_ROUNDS 轮未结束则判为失败。
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
    rc = RunCtx(
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

    # ---- unknown tool result window ----
    # （结果未知窗口：日志里有某个 call_id 的 TOOL_EXECUTING，却没有对应的 TOOL_EXECUTED 与
    # TOOL_RESULT，说明工具已经开始执行但结果未落盘——它到底有没有产生副作用无从判断。
    # 这种情况下**禁止盲目重试**：必须转人工确认或与下游系统对账，因此把 Run 迁移到
    # input-required 并推一张对账卡片给前端，由人来决定补做还是跳过。）
    unknown: dict[str, Any] | None = None
    async with SessionLocal() as s:
        log = EventLog(s)
        prior = await log.list(run_id)
        settled = {e.payload["call_id"] for e in prior if e.type in (RunEventType.TOOL_EXECUTED, RunEventType.TOOL_RESULT)}
        for e in prior:
            if e.type == RunEventType.TOOL_EXECUTING and e.payload["call_id"] not in settled:
                unknown = {
                    "call_id": e.payload["call_id"],
                    "tool": e.payload.get("tool"),
                    "operation_id": e.payload.get("operation_id"),
                }
        if unknown is not None:
            await record_unknown_result(log, run_id, unknown)
            await s.commit()
    if unknown is not None:
        yield CardEvent(card_type=CardType.RECONCILIATION_REQUIRED, payload=unknown)
        yield DoneEvent(run_id=run_id, stop_reason="awaiting")
        return

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
        resulted = {e.payload["call_id"] for e in prior if e.type == RunEventType.TOOL_RESULT}
        for e in prior:
            # 找到"已请求但尚无结果"的工具调用：这是上次执行被挂起（等待审批）的位置，
            # 续跑时需要先完成它，而不是让 harness 重新决策。
            if e.type == RunEventType.TOOL_REQUESTED and e.payload["call_id"] not in resulted:
                pending_call = ToolUseEvent(call_id=e.payload["call_id"], tool_name=e.payload["tool"], input=e.payload.get("input", {}))
        if any(e.type in (RunEventType.TOOL_REQUESTED, RunEventType.MODEL_RESPONSE) for e in prior):
            # 事件日志中已有过模型响应或工具请求，说明这是续跑：先按日志重建消息历史，
            # 再让 harness 基于重放后的统一事件流重建会话（checkpoint via replay）。
            rc.history = invariant.rebuild_messages(prior, initial_messages)
            session = await harness.resume(spec, EventLog.project_unified_events(prior))
        else:
            session = await harness.open_session(spec)
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
        # （先补完此前被打断（现已获批准）的工具调用，再重新向 harness 请求下一轮。）
        async for ev in handle_tool(rc, pending_call, already_logged=True):
            yield ev
        if rc.suspended:
            return

    hooks = HookSet()
    for _round in range(MAX_TOOL_ROUNDS):
        async with SessionLocal() as s:
            log = EventLog(s)
            try:
                # 不变量校验：确认即将发给模型的 rc.history 与"从事件日志重建的历史"完全一致，
                # 防止上下文被意外篡改或与事件日志脱节（保证可重放性/审计一致性）。
                h = invariant.check(await log.list(run_id), initial_messages, rc.history)
            except invariant.InvariantViolation as e:
                await log.append(run_id, RunEventType.RUN_ERROR, {"code": "INVARIANT_VIOLATION", "message": str(e)})
                await log.transition(run_id, RunState.FAILED, "invariant")
                await s.commit()
                yield ErrorEvent(code="INVARIANT_VIOLATION", message=str(e))
                yield DoneEvent(run_id=run_id, stop_reason="error")
                return
            await log.append(
                run_id, RunEventType.MODEL_REQUEST, {"model_alias": agent.model_alias, "messages_hash": h, "n_messages": len(rc.history)}
            )
            await s.commit()

        tool_call: ToolUseEvent | None = None
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        async for ev in harness.run_turn(session, TurnInput(messages=rc.history), assembled.tools, assembled.skills, hooks):
            if isinstance(ev, TextEvent):
                text_parts.append(ev.delta)
                yield ev
            elif isinstance(ev, ThinkingEvent):
                # 思维链一律随 MODEL_RESPONSE 进事件日志；可见性以 Agent 版本配置为准覆写，
                # 是否下发给客户端由 API 层按 visibility 决定（internal 不下发）。
                thinking_parts.append(ev.delta)
                yield ev.model_copy(update={"visibility": thinking_visibility(agent)})
            elif isinstance(ev, ToolUseEvent):
                tool_call = ev
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
                payload: dict[str, Any] = {"text": "".join(text_parts), "usage": ev.model_dump()}
                if thinking_parts:
                    payload["thinking"] = "".join(thinking_parts)
                async with SessionLocal() as s:
                    await EventLog(s).append(run_id, RunEventType.MODEL_RESPONSE, payload)
                    await s.commit()
                if text_parts:
                    rc.history.append({"role": "assistant", "content": "".join(text_parts)})
                yield ev
            elif isinstance(ev, DoneEvent):
                break
            else:
                yield ev

        if tool_call is None:
            # 本轮模型没有请求任何工具：视为对话正常结束，Run 迁移到 completed。
            async with SessionLocal() as s:
                log = EventLog(s)
                await log.append(run_id, RunEventType.RUN_DONE, {"stop_reason": "end_turn"})
                await log.transition(run_id, RunState.COMPLETED, "end_turn")
                await s.commit()
            yield DoneEvent(run_id=run_id, stop_reason="end_turn")
            return

        async for ev in handle_tool(rc, tool_call):
            yield ev
        if rc.suspended:
            return

    # 达到最大工具往返轮数仍未结束：判为失败，避免模型/工具无限循环拖垮资源。
    async with SessionLocal() as s:
        log = EventLog(s)
        await log.append(run_id, RunEventType.RUN_ERROR, {"code": "MAX_TOOL_ROUNDS", "message": "tool loop limit reached"})
        await log.transition(run_id, RunState.FAILED, "max_rounds")
        await s.commit()
    yield ErrorEvent(code="MAX_TOOL_ROUNDS", message="tool loop limit reached")
    yield DoneEvent(run_id=run_id, stop_reason="error")
