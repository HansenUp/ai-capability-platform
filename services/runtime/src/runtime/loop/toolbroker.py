"""工具代理（tool broker）：平台侧唯一允许产生工具副作用的入口。

本模块把"闸口判定 → 审批挂起 → 执行 → 结果外置 → 写事件日志"这一整条链路收敛到
`handle_tool` 一个公开入口。无论副作用是由平台自己的轮次循环（loop.runner）触发，还是
将来由 harness 通过回调（例如 PreToolUse 钩子回灌）触发，都**只能**经过这个入口——
任何绕过它直接调用 `execute_tool` 的路径都会丢失闸口判定与审计记录，是不被允许的。

`execute_tool` 之所以同样放在本模块，是为了避免 runner 与本模块互相导入形成循环依赖，
同时也让"真正执行工具"这件事和它的唯一合法调用方处在同一个文件里，便于审计。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ai_contract import (
    CardEvent,
    CardType,
    DoneEvent,
    Event,
    RiskLevel,
    RunEventType,
    RunState,
    SessionSpec,
    ToolResultEvent,
    ToolSpec,
    ToolUseEvent,
)
from runtime.config import settings
from runtime.context import budget as ctx
from runtime.guard.gate import GateContext, evaluate, params_hash
from runtime.state.db import AgentVersion, SessionLocal
from runtime.state.eventlog import EventLog


async def execute_tool(tool: ToolSpec, tool_input: dict[str, Any], dry_run: bool = False) -> Any:
    """Dev executor. Production dispatches to MCP servers via the capability gateway with an OBO token.
    （开发环境的工具执行器：直接在进程内模拟结果。生产环境应通过能力网关，携带 OBO（On-Behalf-Of）
    令牌把调用分发给真实的 MCP 服务器执行——这部分分发逻辑属于 S6 工作包实现。）
    """
    if tool.name == "echo":
        return {"echo": tool_input}
    if tool.name == "ticket.search":
        return {"tickets": [{"id": f"T-{i}", "title": f"示例工单 {i}", "status": "open"} for i in range(1, 6)]}
    if tool.name == "ticket.close_batch":
        ids = tool_input.get("ids", [])
        if dry_run:
            # dry_run 用于高风险写操作的"影响预览"卡片：只返回将要产生的影响，不实际执行。
            return {"impact_preview": {"tickets": len(ids), "notifications": len(ids)}}
        return {"closed": ids}
    return {"error": f"tool {tool.name} not available in dev executor"}


async def record_unknown_result(log: EventLog, run_id: str, unknown: dict[str, Any], cause: str | None = None) -> None:
    """结果未知路径的落日志部分：记 UNKNOWN_TOOL_RESULT 错误并把 Run 迁移到 input-required（调用方负责提交）。

    unknown 至少含 call_id / tool / operation_id，原样作为对账卡片的 payload。禁止盲目重试：
    是否补做由人工对账决定。
    """
    payload: dict[str, Any] = {
        "code": "UNKNOWN_TOOL_RESULT",
        "message": f"tool {unknown['tool']} result unknown; manual reconciliation required",
    } | unknown
    if cause:
        payload["cause"] = cause
    await log.append(run_id, RunEventType.RUN_ERROR, payload)
    await log.transition(run_id, RunState.INPUT_REQUIRED, "unknown tool result")


@dataclass
class RunCtx:
    """drive() 一次执行过程中的可变上下文，在 runner 与本模块之间传递，避免参数层层透传。"""

    run_id: str
    agent: AgentVersion
    spec: SessionSpec
    tools_by_name: dict[str, ToolSpec]  # 按名称索引的可用工具集合，便于按 tool_name 快速查找
    gate: GateContext  # 闸口判定所需的累积状态（污点、红线、已批准调用等）
    history: list[dict[str, Any]]  # 当前模型可见的消息历史
    suspended: bool = False  # 本轮是否因等待审批而挂起（挂起时 drive 应立即返回）
    workspace: str = field(default=".")  # 本次 Run 的工作区目录，用于工具结果外置


async def handle_tool(rc: RunCtx, call: ToolUseEvent, already_logged: bool = False, reasoning: str | None = None) -> AsyncIterator[Event]:
    """Gate → (approval) → execute → offload → log. Sets rc.suspended when the run must wait.
    （处理单次工具调用的完整流程：闸口判定 →（如需）等待审批 → 执行 → 结果外置 → 写入事件日志。
    当 Run 必须等待人工干预（如审批）时，将 rc.suspended 置 True，调用方据此提前返回。

    already_logged=True 用于续跑场景：该调用此前已经写过 TOOL_REQUESTED 事件（且已获批准），
    这里只需补完后续的判定与执行，不再重复记录 TOOL_REQUESTED，避免事件日志重复。

    reasoning：产生该工具调用的那一轮模型的思维链文本（没有则为 None）。非空时写入历史中 tool_use 消息的
    顶层 "reasoning" 字段与 TOOL_REQUESTED 事件，使其可从事件日志重建（需要回传推理内容的模型据此续接）。）
    """
    tool = rc.tools_by_name.get(call.tool_name)
    phash = params_hash(call.input)  # 本次调用参数的规范化指纹，贯穿审批绑定与写前记录
    if not already_logged:
        tool_use_msg: dict[str, Any] = {
            "role": "assistant",
            "tool_use": {"call_id": call.call_id, "name": call.tool_name, "input": call.input},
        }
        if reasoning:
            tool_use_msg["reasoning"] = reasoning
        rc.history.append(tool_use_msg)
    async with SessionLocal() as s:
        log = EventLog(s)
        if not already_logged:
            requested: dict[str, Any] = {
                "call_id": call.call_id,
                "tool": call.tool_name,
                "input": call.input,
                "risk_level": tool.risk_level if tool else "unknown",
            }
            if reasoning:
                requested["reasoning"] = reasoning
            await log.append(rc.run_id, RunEventType.TOOL_REQUESTED, requested)
        if tool is None:
            # 模型请求了一个未在本次组装工具集中出现的工具名：直接记为错误结果，不进入闸口判定。
            result: Any = {"error": "unknown tool"}
            await log.append(rc.run_id, RunEventType.TOOL_RESULT, {"call_id": call.call_id, "status": "error", "output": result})
            await s.commit()
            rc.history.append({"role": "tool", "call_id": call.call_id, "content": result})
            yield ToolResultEvent(call_id=call.call_id, status="error", output=result)
            return
        gate = await evaluate(rc.gate, tool, call.input, call.call_id)
        await log.append(
            rc.run_id,
            RunEventType.TOOL_DECISION,
            {
                "call_id": call.call_id,
                "decision": gate.decision,
                "reason": gate.reason,
                "policy_decision_id": gate.policy_decision_id,
                "sandbox_level": gate.sandbox_level,
                # 审批附着于具体参数：把参数指纹一并记入决策事件，审批接口据此校验批的是哪一组参数。
                "params_hash": phash,
            },
        )
        if gate.decision == "deny":
            await log.append(rc.run_id, RunEventType.TOOL_RESULT, {"call_id": call.call_id, "status": "denied", "output": gate.reason})
            await s.commit()
            rc.history.append({"role": "tool", "call_id": call.call_id, "content": gate.reason})
            yield ToolResultEvent(call_id=call.call_id, status="denied", output=gate.reason)
            return
        if gate.decision == "require_approval":
            # 需要人工审批：若工具支持 dry-run，先生成影响预览；随后把 Run 状态迁移到
            # auth-required 并挂起，等待 /approve 调用后再通过 /resume 续跑完成本次调用。
            preview = await execute_tool(tool, call.input, dry_run=True) if tool.supports_dry_run else {}
            await log.transition(rc.run_id, RunState.AUTH_REQUIRED, f"tool {tool.name} needs approval")
            await s.commit()
            rc.suspended = True
            yield CardEvent(
                card_type=CardType.IMPACT_PREVIEW,
                # params_hash 必须推给前端：审批接口要求回传同一份参数指纹，参数变化则审批无效。
                payload={"call_id": call.call_id, "tool": tool.name, "preview": preview, "run_id": rc.run_id, "params_hash": phash},
            )
            yield DoneEvent(run_id=rc.run_id, stop_reason="awaiting")
            return
        if gate.idempotency_key and await log.has_idempotency_key(gate.idempotency_key):
            # 幂等键命中：说明该写操作此前已经真正执行过（如续跑重放场景），直接返回去重标记，
            # 不再重复执行，避免重放导致的重复副作用。
            result = {"deduplicated": True}
        else:
            # 写前记录（write-ahead）：先落 TOOL_EXECUTING 并单独提交，再真正执行。
            # 这样"已执行但未落日志"的结果未知窗口被消除——崩溃后日志里一定留有执行意图，
            # 重放时能识别出结果未知并转人工对账，而不是盲目重跑一遍产生重复副作用。
            await log.append(
                rc.run_id,
                RunEventType.TOOL_EXECUTING,
                {"call_id": call.call_id, "tool": tool.name, "operation_id": gate.idempotency_key, "params_hash": phash},
            )
            await s.commit()
            try:
                result = await execute_tool(tool, call.input)
            except Exception as exc:
                if tool.risk_level == RiskLevel.READ:
                    raise
                # 写操作执行抛异常：副作用是否已发生无从判断，当场走结果未知路径——
                # 记错误、迁移到 input-required、推对账卡片，SSE 流以 done 正常收尾，不自动重试。
                unknown = {"call_id": call.call_id, "tool": tool.name, "operation_id": gate.idempotency_key}
                await record_unknown_result(log, rc.run_id, unknown, cause=type(exc).__name__)
                await s.commit()
                rc.suspended = True
                yield CardEvent(card_type=CardType.RECONCILIATION_REQUIRED, payload=unknown)
                yield DoneEvent(run_id=rc.run_id, stop_reason="awaiting")
                return
            await log.append(
                rc.run_id, RunEventType.TOOL_EXECUTED, {"call_id": call.call_id, "tool": tool.name}, idempotency_key=gate.idempotency_key
            )
        if tool.data_class == "confidential":
            rc.gate.taint.add("confidential")  # 污点传播：本次 run 后续的外发类工具调用会因此被收紧为需审批
        summary, ref = ctx.offload_if_large(result, rc.workspace, settings.tool_result_offload_tokens)
        # 结果事件带上工具的数据分类：续跑时据此重建污点，不依赖届时的工具表（工具定义可能已变化）。
        await log.append(
            rc.run_id,
            RunEventType.TOOL_RESULT,
            {"call_id": call.call_id, "status": "ok", "output": summary, "output_ref": ref, "data_class": tool.data_class},
        )
        await s.commit()
    rc.history.append({"role": "tool", "call_id": call.call_id, "content": ref or summary})
    yield ToolResultEvent(call_id=call.call_id, status="ok", output=summary, output_ref=ref)
