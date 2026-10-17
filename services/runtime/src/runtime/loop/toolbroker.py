"""工具代理（tool broker）：平台侧唯一允许产生工具副作用的入口。

本模块把"闸口判定 → 审批挂起 → 执行 → 结果外置 → 写事件日志"这一整条链路收敛到
`handle_tool` 一个公开入口。无论副作用是由平台自己的轮次循环（loop.runner）触发，还是
将来由 harness 通过回调（例如 PreToolUse 钩子回灌）触发，都**只能**经过这个入口——
任何绕过它直接调用 `execute_tool` 的路径都会丢失闸口判定与审计记录，是不被允许的。

`execute_tool` 之所以同样放在本模块，是为了避免 runner 与本模块互相导入形成循环依赖，
同时也让"真正执行工具"这件事和它的唯一合法调用方处在同一个文件里，便于审计。
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

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
from runtime.observe import audit
from runtime.state.db import AgentVersion, SessionLocal
from runtime.state.eventlog import EventLog

LOCAL_ONLY_TOOLS = frozenset({"echo"})  # 只在进程内执行、不经能力网关的开发工具（元工具 read_artifact 等另行处理）


class GatewayToolError(Exception):
    """能力网关层面的失败（未登记、令牌换取失败、MCP 不可达）：工具调用无法到达业务系统。"""


async def execute_tool(
    tool: ToolSpec,
    tool_input: dict[str, Any],
    dry_run: bool = False,
    rc: RunCtx | None = None,
    call_id: str | None = None,
    idempotency_key: str | None = None,
) -> Any:
    """执行一次已经闸口放行的工具调用。

    - TOOL_DISPATCH=gateway 且工具来自能力目录（有 capability_id）：经能力网关 `/invoke` 分发到 MCP Server，
      携带终端用户令牌（网关换取 OBO 令牌）；业务系统返回的 4xx（无权限 / 不存在 / 参数错误）作为工具错误结果
      `{"error": ..., "status_code": n}` 交还模型；网关自身失败抛 GatewayToolError。
      写类工具（非 dry_run）遇业务系统 5xx 或缺少状态码：副作用是否已发生无从判断，同样抛 GatewayToolError，
      由 handle_tool 走结果未知 / 对账路径（D61）；只读工具的任何失败仍作为工具错误结果交还模型。
    - 其他情况：进程内开发执行器（测试与无网关的本机调试）。
    """
    if tool.name == "read_artifact":
        return read_artifact(rc.workspace if rc else ".", str(tool_input.get("ref", "")))
    if settings.tool_dispatch == "gateway" and tool.capability_id and tool.name not in LOCAL_ONLY_TOOLS:
        return await _dispatch_via_gateway(tool, tool_input, dry_run, rc, call_id, idempotency_key)
    return await _dev_execute(tool, tool_input, dry_run)


async def _dispatch_via_gateway(
    tool: ToolSpec, tool_input: dict[str, Any], dry_run: bool, rc: RunCtx | None, call_id: str | None, idempotency_key: str | None
) -> Any:
    body = {
        "tool": tool.name,
        "input": tool_input,
        "subject": rc.gate.subject if rc else "",
        "agent_id": rc.agent.agent_id if rc else "",
        "agent_version": rc.agent.version if rc else "",
        "run_id": rc.run_id if rc else None,
        "call_id": call_id,
        "idempotency_key": idempotency_key,
        "dry_run": dry_run,
    }
    headers = {"Authorization": f"Bearer {rc.credential}"} if rc and rc.credential else {}
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_s, transport=_gateway_transport) as c:
            r = await c.post(f"{settings.capability_gateway_url.rstrip('/')}/invoke", json=body, headers=headers)
    except httpx.HTTPError as e:
        raise GatewayToolError(f"capability gateway unreachable: {type(e).__name__}") from e
    if r.status_code != 200:
        raise GatewayToolError(f"capability gateway returned HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if data.get("ok"):
        return data.get("output")
    status = data.get("status_code")
    if tool.risk_level != RiskLevel.READ and not dry_run and not (isinstance(status, int) and status < 500):
        # 写操作在业务系统侧 5xx（或状态未知）：可能已部分执行，不能当成确定失败交还模型继续跑
        raise GatewayToolError(f"business system returned {status if status is not None else 'no status'} for write tool; result unknown")
    output = data.get("output")
    detail = output.get("detail") if isinstance(output, dict) else output
    return {"error": detail or "tool call failed", "status_code": status}


# 外置结果引用：只接受 32 位十六进制 id（模型有时省略 artifact:// 前缀，同样接受），杜绝路径穿越
ARTIFACT_REF = re.compile(r"^(?:artifact://)?([0-9a-f]{32})$")


def read_artifact(workspace: str, ref: str) -> Any:
    """read_artifact 元工具（S4.8 附件隔离）：只能读取当前 Run 工作区内的外置结果 / 附件，
    其他 Run（包括同一用户的其他 Run）的工件一律视为不存在。"""
    m = ARTIFACT_REF.match(ref)
    path = os.path.join(workspace, f"{m.group(1)}.txt") if m else None
    if path is None or not os.path.isfile(path):
        return {"error": "artifact not found in this run"}
    with open(path, encoding="utf-8") as f:
        return {"ref": ref, "content": f.read()}


_gateway_transport: httpx.AsyncBaseTransport | None = None  # 测试注入


def set_gateway_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    global _gateway_transport
    _gateway_transport = transport


async def _dev_execute(tool: ToolSpec, tool_input: dict[str, Any], dry_run: bool = False) -> Any:
    """进程内开发执行器：模拟结果，不访问任何业务系统。"""
    if tool.name == "echo":
        return {"echo": tool_input}
    if tool.name == "ticket.search":
        return {"tickets": [{"id": f"T-{i}", "title": f"示例工单 {i}", "status": "open"} for i in range(1, 6)]}
    if tool.name == "ticket.get":
        return {"id": tool_input.get("id"), "title": "示例工单", "status": "closed", "records": []}
    if tool.name == "data.query":
        return {"rows": [{"customer": "某客户", "contract_value": 1200000}]}
    if tool.name == "knowledge.search":
        return {"hits": []}
    if tool.name == "mail.send":
        return {"sent": True}
    if tool.name == "ticket.close_batch":
        ids = tool_input.get("ids", [])
        if dry_run:
            # dry_run 用于高风险写操作的"影响预览"卡片：只返回将要产生的影响，不实际执行。
            return {"impact_preview": {"tickets": len(ids), "notifications": len(ids)}}
        return {"closed": ids}
    return {"error": f"tool {tool.name} not available in dev executor"}


async def _unknown_result(
    log: EventLog, s: Any, rc: RunCtx, call: ToolUseEvent, tool: ToolSpec, operation_id: str | None, exc: Exception
) -> AsyncIterator[Event]:
    """写操作执行抛异常：副作用是否已发生无从判断，当场走结果未知路径——
    记错误、迁移到 input-required、推对账卡片，SSE 流以 done 正常收尾，不自动重试。"""
    unknown = {"call_id": call.call_id, "tool": tool.name, "operation_id": operation_id}
    await record_unknown_result(log, rc.run_id, unknown, cause=type(exc).__name__)
    await s.commit()
    rc.suspended = True
    yield CardEvent(card_type=CardType.RECONCILIATION_REQUIRED, payload=unknown)
    yield DoneEvent(run_id=rc.run_id, stop_reason="awaiting")


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
    credential: str | None = field(default=None, repr=False)  # 终端用户访问令牌（只在内存中），经网关执行工具时换取 OBO 令牌


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
        # 统一审计（S2.3）：闸口判定与决策事件同一事务落 t_audit——谁（Run 发起主体）以哪个 Agent 调了什么工具、结论如何。
        decision_row = audit.entry(
            None,
            "tool.decision",
            f"tool:{tool.name}",
            gate.decision,
            actor=rc.gate.subject,
            act=f"agent:{rc.agent.agent_id}",
            tenant_id=rc.agent.tenant_id,
            detail={
                "run_id": rc.run_id,
                "call_id": call.call_id,
                "risk_level": tool.risk_level,
                "reason": gate.reason,
                "policy_decision_id": gate.policy_decision_id,
                "params_hash": phash,
            },
        )
        decision_row.auth_method = None  # 非请求级审计：身份来源见该 Run 的请求审计行
        s.add(decision_row)
        if gate.decision == "deny":
            await log.append(rc.run_id, RunEventType.TOOL_RESULT, {"call_id": call.call_id, "status": "denied", "output": gate.reason})
            await s.commit()
            rc.history.append({"role": "tool", "call_id": call.call_id, "content": gate.reason})
            yield ToolResultEvent(call_id=call.call_id, status="denied", output=gate.reason)
            return
        if gate.decision == "require_approval":
            # 需要人工审批：若工具支持 dry-run，先生成影响预览；随后把 Run 状态迁移到
            # auth-required 并挂起，等待 /approve 调用后再通过 /resume 续跑完成本次调用。
            preview: Any = {}
            if tool.supports_dry_run:
                try:
                    preview = await execute_tool(tool, call.input, dry_run=True, rc=rc, call_id=call.call_id)
                except GatewayToolError as exc:
                    # 影响预演失败不阻断审批流程：卡片如实标注"预演不可用"，由审批人决定（dry-run 不产生副作用）
                    preview = {"error": f"impact preview unavailable: {exc}"}
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
                result = await execute_tool(tool, call.input, rc=rc, call_id=call.call_id, idempotency_key=gate.idempotency_key)
            except GatewayToolError as exc:
                if tool.risk_level != RiskLevel.READ:
                    async for ev in _unknown_result(log, s, rc, call, tool, gate.idempotency_key, exc):
                        yield ev
                    return
                # 只读调用经网关失败（用户令牌过期、网关不可达等）不会留下副作用：作为工具错误结果交还模型，Run 继续
                result = {"error": f"tool unavailable: {exc}"}
            except Exception as exc:
                if tool.risk_level == RiskLevel.READ:
                    raise
                async for ev in _unknown_result(log, s, rc, call, tool, gate.idempotency_key, exc):
                    yield ev
                return
            await log.append(
                rc.run_id, RunEventType.TOOL_EXECUTED, {"call_id": call.call_id, "tool": tool.name}, idempotency_key=gate.idempotency_key
            )
        if tool.data_class == "confidential":
            rc.gate.taint.add("confidential")  # 污点传播：本次 run 后续的外发类工具调用会因此被收紧为需审批
        if tool.name == "read_artifact":
            # read_artifact 读回的就是外置内容：不再外置，否则模型永远读不到原文，只能反复读取直到轮数耗尽
            summary, ref = result, None
        else:
            summary, ref = ctx.offload_if_large(result, rc.workspace, settings.tool_result_offload_tokens)
        # 结果事件带上工具的数据分类：续跑时据此重建污点，不依赖届时的工具表（工具定义可能已变化）。
        # 业务系统拒绝 / 不存在等作为工具错误
        status: Literal["ok", "error"] = "error" if isinstance(result, dict) and "error" in result else "ok"
        await log.append(
            rc.run_id,
            RunEventType.TOOL_RESULT,
            {"call_id": call.call_id, "status": status, "output": summary, "output_ref": ref, "data_class": tool.data_class},
        )
        await s.commit()
    rc.history.append({"role": "tool", "call_id": call.call_id, "content": ref or summary})
    yield ToolResultEvent(call_id=call.call_id, status=status, output=summary, output_ref=ref)
