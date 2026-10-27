"""工具代理（tool broker）：平台侧唯一允许产生工具副作用的入口。

本模块把"闸口判定 → 审批挂起 → 执行 → 结果外置 → 写事件日志"这一整条链路收敛到
`handle_tool` 一个公开入口。无论副作用是由平台自己的轮次循环（loop.runner）触发，还是
将来由 harness 通过回调（例如 PreToolUse 钩子回灌）触发，都**只能**经过这个入口——
任何绕过它直接调用 `execute_tool` 的路径都会丢失闸口判定与审计记录，是不被允许的。

`execute_tool` 之所以同样放在本模块，是为了避免 runner 与本模块互相导入形成循环依赖，
同时也让"真正执行工具"这件事和它的唯一合法调用方处在同一个文件里，便于审计。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx

from ai_contract import (
    CardEvent,
    CardType,
    DoneEvent,
    Event,
    HookPoint,
    RiskLevel,
    RunEventType,
    RunState,
    SessionSpec,
    SkillDir,
    ToolResultEvent,
    ToolSpec,
    ToolUseEvent,
)
from ai_contract.data_class import Level
from runtime import credentials, gateway_client
from runtime.config import settings
from runtime.context import budget as ctx
from runtime.guard.gate import GateContext, GateResult, _approval_valid, evaluate, params_hash
from runtime.guard.hooks import HookChain, HookOutcome
from runtime.loop import discovery, user_input
from runtime.observe import audit
from runtime.state.db import AgentVersion, SessionLocal
from runtime.state.eventlog import EventLog, OperationExists

LOCAL_ONLY_TOOLS = frozenset({"echo"})  # 只在进程内执行、不经能力网关的开发工具（元工具 read_artifact 等另行处理）


def taint_of(status: Any, data_class: Any) -> set[str]:
    """一次工具结果带来的数据分类污点（ADR-0010，D89）：工具结果——无论成功（ok）还是失败（error，错误信息同样可能夹带
    数据）——按工具（或 post_tool 升级后）的数据分级带上污点：confidential → {"confidential"}，internal → {"internal"}
    （S9 OPT-4，D152：internal 同样传播，决定观测脱敏、模型 / 回退通道判定），public 不带；分级缺失（未知工具等未读到数据的
    结果）不带，无法识别的取值按 confidential（失败关闭）。被拒绝（denied）的调用没有读到任何数据。
    工具代理的实时传播与 runner 续跑时的重放重建共用本函数，保证续跑前后污点一致。"""
    if status not in ("ok", "error") or data_class is None:
        return set()
    level = Level.parse(data_class)
    return set() if level == Level.PUBLIC else {level.value}


def hmac_json(value: Any) -> str:
    """工具原始输出的规范化 HMAC-SHA256（密钥 HOOK_DIGEST_KEY，只在服务端）：post_tool hook 改写输出后，事件日志只留
    原文指纹，不留原文（runner 对 post_model 拒绝时被扣下的文本同样只留本函数的指纹）。用带密钥的 HMAC 而不是
    裸 sha256：被脱敏的往往是手机号、身份证号这类取值空间小的内容，裸哈希可被读日志的人穷举还原。"""
    canon = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(settings.hook_digest_key.encode("utf-8"), canon.encode("utf-8"), hashlib.sha256).hexdigest()


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
    if tool.name == "run_skill_script":
        from runtime.skills import execution

        if rc is None or call_id is None:
            raise GatewayToolError("script execution requires a run and call")
        if dry_run:
            return await execution.preview(rc, tool_input)
        return await execution.execute(rc, tool_input, call_id, idempotency_key)
    if settings.tool_dispatch == "gateway" and tool.capability_id and tool.name not in LOCAL_ONLY_TOOLS:
        return await _dispatch_via_gateway(tool, tool_input, dry_run, rc, call_id, idempotency_key)
    return await _dev_execute(tool, tool_input, dry_run)


async def _dispatch_via_gateway(
    tool: ToolSpec, tool_input: dict[str, Any], dry_run: bool, rc: RunCtx | None, call_id: str | None, idempotency_key: str | None
) -> Any:
    body: dict[str, Any] = {
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
    if rc is not None:
        # 网关第二道防线（评审 #3）：闸口对本次调用的审批结论（参数指纹一致且未过期）、本 Run 的数据污点与 Agent 所属租户
        # 一并交给网关，网关据此重新判定（未审批的 require_approval 非预演调用、污点外发、Agent 跨租户在网关同样被拒绝）
        body["approved"] = _approval_valid(rc.gate.approved_call_ids.get(call_id or ""), params_hash(tool_input))
        body["taint"] = sorted(rc.gate.taint)
        body["agent_tenant"] = rc.agent.tenant_id
    try:
        # 经网关客户端（契约 3）：服务态带 ai-runtime-svc 服务令牌 + X-AI-User-Token，开发态沿用用户令牌 Bearer 头
        r = await gateway_client.request(
            "POST", "/invoke", user_token=rc.credential if rc else None, timeout_s=settings.llm_timeout_s, json=body
        )
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


def set_gateway_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试注入（沿用旧入口）：所有能力网关请求（/invoke、/capabilities、/capabilities/search）改经该 transport。"""
    gateway_client.set_transport_for_tests(transport)


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
    log: EventLog,
    s: Any,
    rc: RunCtx,
    call: ToolUseEvent,
    tool: ToolSpec,
    operation_id: str | None,
    exc: Exception | None,
    operation: dict[str, Any] | None = None,
) -> AsyncIterator[Event]:
    """写操作结果无从判断，当场走结果未知路径——记错误、迁移到 input-required、推对账卡片，SSE 流以 done 正常收尾，
    不自动重试。两种来源：执行抛异常（exc，登记行先置 unknown 并单独提交，失租时也留得下）；写前登记发现该操作已被
    他人登记且未完成（exc=None，operation 为现有登记行的状态，附在对账卡上）。"""
    unknown = {"call_id": call.call_id, "tool": tool.name, "operation_id": operation_id}
    if exc is not None and operation_id is not None:
        await log.update_operation(operation_id, user_input.OP_UNKNOWN, pending_only=tool.name == "run_skill_script")
        await s.commit()
    await record_unknown_result(log, rc.run_id, unknown, cause=type(exc).__name__ if exc is not None else "OPERATION_IN_FLIGHT")
    await s.commit()
    rc.suspended = True
    yield CardEvent(card_type=CardType.RECONCILIATION_REQUIRED, payload=unknown | ({"operation": operation} if operation else {}))
    yield DoneEvent(run_id=rc.run_id, stop_reason="awaiting")


def operation_view(op: Any) -> dict[str, Any]:
    """t_tool_operation 登记行的对账视图（附在对账卡 payload.operation 上；不含任何结果原文）。"""
    return {"status": op.status, "result_hash": op.result_hash, "node": op.node, "epoch": op.epoch}


async def _register_operation(log: EventLog, s: Any, run_id: str, operation_id: str, call_id: str) -> dict[str, Any] | None:
    """写前登记（D111）：在当前事务插入 t_tool_operation（随后同事务写 TOOL_EXECUTING），返回 None 表示本执行者已认领。
    主键冲突 = 该操作已被登记过：回滚登记，若发起人对账选择了 retry（下游声明幂等）则改回 executing 认领重做（与随后的
    TOOL_EXECUTING 同事务、受 fence 约束）；否则返回现有登记行的视图，由调用方去重（executed）或转对账，绝不重复执行。"""
    try:
        await log.record_operation(run_id, operation_id, call_id, node=settings.node_id)
        return None
    except OperationExists:
        await s.rollback()
    op = await log.get_operation(operation_id)
    if op is not None and op.status == user_input.OP_RETRY:
        await log.update_operation(operation_id, user_input.OP_EXECUTING)
        return None
    return operation_view(op) if op is not None else {"status": "missing"}


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
    # 令牌到期时间（持久化执行由 drive_durable 给出）：非 None 时每次 TOOL_EXECUTING 前检查剩余有效期，
    # 不足 CREDENTIAL_MIN_TTL_S 即挂起为 input-required{kind:credential}，不把写工具推入结果未知。None 时不检查（行为不变）。
    credential_expires_at: datetime | None = None
    # credential 所属主体；与 Run 发起主体不同时记入 TOOL_EXECUTING 与工具审计（D87）。D87 修订后续跑只允许发起人，
    # 经 API 不再出现不同的值；机制保留（直接调用 drive 时仍按此记录）
    credential_subject: str | None = None
    hooks: HookChain = field(default_factory=HookChain)  # 本 Agent 版本的 hook 链；空链时事件日志与此前逐字节一致
    # 两级发现（S8.2）：search_capabilities 动态加入本 Run 的工具（按加入顺序，供 harness 的工具列表追加；未开启发现时恒为空）
    discovered: list[ToolSpec] = field(default_factory=list)
    hook_records: list[dict[str, Any]] = field(default_factory=list)  # 最近一次 handle_tool 的 hook 结论（供工具 span 记录）
    # 本 Run 组装出的技能（Skill 平台工具 load_skill / read_skill_file / run_skill_script 据此读取，见 runtime.skills.tools）
    skills: list[SkillDir] = field(default_factory=list)

    def foreign_credential_subject(self) -> str | None:
        """执行工具所用令牌的主体与 Run 发起主体不同时返回该主体，否则 None（不写，保持日志不变）。"""
        cs = self.credential_subject
        return cs if cs is not None and cs != self.gate.subject else None


@dataclass
class _Call:
    """一次工具调用在工具代理中的处理状态（单个调用与批内调用共用）。"""

    call: ToolUseEvent
    tool: ToolSpec | None
    effective: dict[str, Any]  # 实际用于闸口判定与执行的入参（pre_tool 可改写）
    phash: str  # effective 的参数指纹，贯穿审批绑定与写前记录
    pre: HookOutcome | None = None  # pre_tool 结论（续跑时为 None：不重跑）
    hook_records: list[dict[str, Any]] = field(default_factory=list)  # 本调用的 hook 结论（pre_tool + post_tool）
    gate: GateResult | None = None  # 闸口判定结果（进了闸口才有）
    preview: Any = None  # 待审批调用的影响预览
    batch: dict[str, Any] | None = None  # 并行批次 {id, index, size}（单个调用为 None）
    preparation_error: dict[str, Any] | None = None


@dataclass
class _Outcome:
    """处理阶段的结论：done（结果已落日志）/ suspended（Run 已挂起）/ approval（待审批，调用方迁状态并出卡）/ execute（放行待执行）
    / run（TOOL_EXECUTING 已提交，待执行）/ dedup（幂等键命中，不再执行）。events 为要按序下发的事件。"""

    kind: Literal["done", "suspended", "approval", "execute", "run", "dedup"]
    events: list[Event] = field(default_factory=list)


async def _pre_tool(rc: RunCtx, call: ToolUseEvent, already_logged: bool) -> _Call:
    """pre_tool hook（续跑时不重跑：调用方传入的 call.input 即事件日志中的 effective_input）。"""
    tool = rc.tools_by_name.get(call.tool_name)
    c = _Call(call=call, tool=tool, effective=call.input, phash="")
    if not already_logged and rc.hooks.has(HookPoint.PRE_TOOL):
        c.pre = await rc.hooks.run(
            HookPoint.PRE_TOOL,
            {
                "call_id": call.call_id,
                "tool": call.tool_name,
                "input": call.input,
                "risk_level": "write_high" if call.tool_name == "run_skill_script" else (tool.risk_level.value if tool else "unknown"),
                "data_class": tool.data_class if tool else None,
                "taint": sorted(rc.gate.taint),
            },
        )
        c.hook_records += c.pre.records
        if not c.pre.denied and "input" in c.pre.overrides:
            c.effective = c.pre.overrides["input"]
    if tool is not None and call.tool_name == "run_skill_script" and not (c.pre and c.pre.denied):
        from runtime.skills import execution
        from runtime.skills.verify import SkillVerifyError

        try:
            c.tool, c.effective = await execution.prepare(rc, c.effective, replay=already_logged)
        except (SkillVerifyError, OSError, ValueError) as exc:
            c.preparation_error = {"error": str(exc), "code": getattr(exc, "code", "SKILL_VERIFY_FAILED")}
    c.phash = params_hash(c.effective)
    return c


def _requested_payload(c: _Call, reasoning: str | None) -> dict[str, Any]:
    """TOOL_REQUESTED 的负载（键序固定：未开启并行时与此前逐字节一致；batch 只在多调用批次中出现）。"""
    requested: dict[str, Any] = {
        "call_id": c.call.call_id,
        "tool": c.call.tool_name,
        "input": c.call.input,
        "risk_level": c.tool.risk_level if c.tool else "unknown",
    }
    if reasoning:
        requested["reasoning"] = reasoning
    if c.pre is not None:
        requested["hooks"] = c.pre.records
        if not c.pre.denied:
            requested["effective_input"] = c.effective
    if c.batch is not None:
        requested["batch"] = c.batch
    if c.call.tool_name == "run_skill_script" and c.preparation_error is None:
        requested["effective_input"] = c.effective
    return requested


async def _resolved(log: EventLog, s: Any, rc: RunCtx, call_id: str, status: Literal["ok", "error", "denied"], output: Any) -> _Outcome:
    """不执行即得出结果的调用（拒绝、未知工具、入参不合规）：记 TOOL_RESULT、提交、写入历史。"""
    await log.append(rc.run_id, RunEventType.TOOL_RESULT, {"call_id": call_id, "status": status, "output": output})
    await s.commit()
    rc.history.append({"role": "tool", "call_id": call_id, "content": output})
    return _Outcome("done", [ToolResultEvent(call_id=call_id, status=status, output=output)])


async def _decide(rc: RunCtx, log: EventLog, s: Any, c: _Call) -> _Outcome:
    """pre_tool 拒绝 / 平台工具拦截 / 未知工具 / 闸口判定（写 TOOL_DECISION 与审计行）。approval 与 execute 时不提交（调用方决定）；
    待审批的调用在此生成影响预览（dry-run 无副作用）。"""
    call, tool, effective, phash, pre = c.call, c.tool, c.effective, c.phash, c.pre
    if pre is not None and pre.denied:
        # pre_tool 拒绝：不进闸口，结论记 tool.decision 事件与审计，结果记 denied
        reason = f"hook:{pre.denied_by}"
        await log.append(
            rc.run_id, RunEventType.TOOL_DECISION, {"call_id": call.call_id, "decision": "deny", "reason": reason, "params_hash": phash}
        )
        hook_row = audit.entry(
            None,
            "tool.decision",
            f"tool:{call.tool_name}",
            "deny",
            actor=rc.gate.subject,
            act=f"agent:{rc.agent.agent_id}",
            tenant_id=rc.agent.tenant_id,
            detail={"run_id": rc.run_id, "call_id": call.call_id, "reason": reason, "params_hash": phash},
        )
        hook_row.auth_method = None
        s.add(hook_row)
        return await _resolved(log, s, rc, call.call_id, "denied", reason)
    if tool is not None and tool.name == user_input.TOOL_NAME:
        # 平台工具 request_user_input（S7 §3）：闸口前拦截，不执行、不进闸口——挂起为 input-required 等发起人回答，
        # 回答（provide_input）写 TOOL_RESULT 后续跑。入参不合规作为工具错误结果交还模型。
        try:
            prompt, schema = user_input.parse_request(effective)
        except ValueError as exc:
            return await _resolved(log, s, rc, call.call_id, "error", {"error": str(exc)})
        asked = await user_input.request_answer(log, rc.run_id, call.call_id, prompt, schema)
        await s.commit()
        rc.suspended = True
        return _Outcome("suspended", [asked, DoneEvent(run_id=rc.run_id, stop_reason="awaiting")])
    if tool is not None and tool.name == discovery.TOOL_NAME and discovery.enabled(rc.agent):
        # 元工具 search_capabilities（S8.2）：闸口前拦截，不进闸口——经能力网关检索，新工具加入本 Run 并写
        # CAPABILITIES_DISCOVERED（重放 / 接管据此恢复，不重新检索）；发现的工具此后调用照常过闸口。
        await s.commit()  # TOOL_REQUESTED 先提交：检索期间不持有写事务（检索失败 / 崩溃时续跑重新检索，此前无任何记录）
        found = await discovery.search(rc, effective)
        if found.event is not None:
            await log.append(rc.run_id, RunEventType.CAPABILITIES_DISCOVERED, {"call_id": call.call_id} | found.event)
        status_d: Literal["ok", "error"] = "error" if "error" in found.result else "ok"
        await log.append(rc.run_id, RunEventType.TOOL_RESULT, {"call_id": call.call_id, "status": status_d, "output": found.result})
        await s.commit()
        discovery.apply(rc, found)  # 提交成功后才加入本 Run（与事件日志一致）
        rc.history.append({"role": "tool", "call_id": call.call_id, "content": found.result})
        return _Outcome("done", [ToolResultEvent(call_id=call.call_id, status=status_d, output=found.result)])
    if c.preparation_error is not None:
        return await _resolved(log, s, rc, call.call_id, "error", c.preparation_error)
    if tool is not None and tool.name in _skill_tool_names() and (tool.name != "run_skill_script" or tool.risk_level == RiskLevel.READ):
        return await _skill_tool(rc, log, s, c)
    if tool is None:
        # 模型请求了一个未在本次组装工具集中出现的工具名：直接记为错误结果，不进入闸口判定。
        return await _resolved(log, s, rc, call.call_id, "error", {"error": "unknown tool"})
    gate = c.gate = await evaluate(rc.gate, tool, effective, call.call_id)
    if tool.name == "run_skill_script":
        call_digest = hashlib.sha256(call.call_id.encode()).hexdigest()[:16]
        gate.idempotency_key = f"{rc.run_id}:script:{call_digest}:{phash}"
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
    decision_detail: dict[str, Any] = {
        "run_id": rc.run_id,
        "call_id": call.call_id,
        "risk_level": tool.risk_level,
        "reason": gate.reason,
        "policy_decision_id": gate.policy_decision_id,
        "params_hash": phash,
    }
    if rc.foreign_credential_subject():
        decision_detail["credential_subject"] = rc.foreign_credential_subject()  # 以审批人等他人令牌执行（D87）
    decision_row = audit.entry(
        None,
        "tool.decision",
        f"tool:{tool.name}",
        gate.decision,
        actor=rc.gate.subject,
        act=f"agent:{rc.agent.agent_id}",
        tenant_id=rc.agent.tenant_id,
        detail=decision_detail,
    )
    decision_row.auth_method = None  # 非请求级审计：身份来源见该 Run 的请求审计行
    s.add(decision_row)
    if gate.decision == "deny":
        return await _resolved(log, s, rc, call.call_id, "denied", gate.reason)
    if gate.decision == "require_approval":
        # 需要人工审批：若工具支持 dry-run，先生成影响预览；随后由调用方把 Run 迁到 auth-required 并挂起，
        # 等待 /approve 后续跑完成本次调用。
        c.preview = {}
        if tool.supports_dry_run:
            try:
                c.preview = await execute_tool(tool, effective, dry_run=True, rc=rc, call_id=call.call_id)
            except GatewayToolError as exc:
                # 影响预演失败不阻断审批流程：卡片如实标注"预演不可用"，由审批人决定（dry-run 不产生副作用）
                c.preview = {"error": f"impact preview unavailable: {exc}"}
        return _Outcome("approval")
    return _Outcome("execute")


def _skill_tool_names() -> frozenset[str]:
    from runtime.skills import tools as skill_tools  # 延迟导入：skills 依赖沙箱与出站客户端，工具代理导入时不必加载

    return skill_tools.TOOL_NAMES


async def _skill_tool(rc: RunCtx, log: EventLog, s: Any, c: _Call) -> _Outcome:
    """Skill 平台工具 load_skill / read_skill_file / run_skill_script（S9.5 / S9.7，契约 ⑤ runtime.skills.tools.handle）：
    与 search_capabilities 同法在闸口前拦截——TOOL_REQUESTED 先提交，再由 skills.tools.handle 执行（读已验签的技能文件 /
    沙箱执行清单声明的脚本），结果写 TOOL_RESULT（观测字段放 payload.skill），重放读日志、不重新执行。"""
    from runtime.skills import tools as skill_tools

    assert c.tool is not None
    await s.commit()
    res = await skill_tools.handle(rc, ToolUseEvent(call_id=c.call.call_id, tool_name=c.call.tool_name, input=c.effective))
    if res is None:
        res = skill_tools.ToolResult("error", {"error": f"unknown skill tool {c.call.tool_name!r}"})
    data_class = Level.highest((c.tool.data_class, res.event.get("data_class", c.tool.data_class))).value
    rc.gate.taint |= taint_of(res.status, data_class)
    summary, ref = ctx.offload_if_large(res.output, rc.workspace, settings.tool_result_offload_tokens)
    payload: dict[str, Any] = {
        "call_id": c.call.call_id,
        "status": res.status,
        "output": summary,
        "output_ref": ref,
        "data_class": data_class,
    }
    if res.event:
        payload["skill"] = res.event
    await log.append(rc.run_id, RunEventType.TOOL_RESULT, payload)
    await s.commit()
    rc.history.append({"role": "tool", "call_id": c.call.call_id, "content": ref or summary})
    return _Outcome("done", [ToolResultEvent(call_id=c.call.call_id, status=res.status, output=summary, output_ref=ref)])


def _approval_card(rc: RunCtx, c: _Call) -> dict[str, Any]:
    """单个待审批调用的影响预览卡 payload（params_hash 必须推给前端：审批接口要求回传同一份参数指纹）。"""
    assert c.tool is not None
    return {"call_id": c.call.call_id, "tool": c.tool.name, "preview": c.preview, "run_id": rc.run_id, "params_hash": c.phash}


async def _begin_execute(rc: RunCtx, c: _Call) -> _Outcome:
    """执行前：幂等去重、令牌有效期检查、跨 Worker 写前登记，最后写 TOOL_EXECUTING 并单独提交（write-ahead）。"""
    assert c.tool is not None and c.gate is not None
    call, tool, operation_id = c.call, c.tool, c.gate.idempotency_key  # 写操作的全局操作标识（= 幂等键，经网关透传）
    async with SessionLocal() as s:
        log = EventLog(s)
        deduplicated = bool(operation_id) and await log.has_idempotency_key(operation_id or "")
        if not deduplicated and rc.credential_expires_at is not None and not credentials.is_fresh(rc.credential_expires_at):
            # 令牌剩余有效期不足（S7 §4）：执行前挂起等发起人刷新，避免执行中途令牌过期把写工具推入结果未知
            asked = await user_input.request_credential(log, rc.run_id, call.call_id, "expiring")
            await s.commit()
            rc.suspended = True
            return _Outcome("suspended", [asked, DoneEvent(run_id=rc.run_id, stop_reason="awaiting")])
        if not deduplicated and operation_id is not None:
            # 跨 Worker 去重（D111）：写前登记与 TOOL_EXECUTING 同一事务，主键冲突只回滚登记本身。已被他人执行完成 → 去重；
            # 未完成 → 结果未知（对账卡附登记状态），绝不重复执行。
            existing = await _register_operation(log, s, rc.run_id, operation_id, call.call_id)
            if existing is not None and existing.get("status") == user_input.OP_EXECUTED:
                deduplicated = True
            elif existing is not None:
                events = [ev async for ev in _unknown_result(log, s, rc, call, tool, operation_id, None, operation=existing)]
                return _Outcome("suspended", events)
        if deduplicated:
            # 幂等键命中：该写操作此前已经真正执行过（如续跑重放场景），不再重复执行，避免重放导致的重复副作用。
            return _Outcome("dedup")
        # 写前记录（write-ahead）：先落 TOOL_EXECUTING 并单独提交，再真正执行——崩溃后日志里一定留有执行意图，
        # 重放时写调用转人工对账、只读调用直接重执行，而不是盲目重跑一遍写操作产生重复副作用。
        executing: dict[str, Any] = {"call_id": call.call_id, "tool": tool.name, "operation_id": operation_id, "params_hash": c.phash}
        if rc.foreign_credential_subject():
            executing["credential_subject"] = rc.foreign_credential_subject()
        await log.append(rc.run_id, RunEventType.TOOL_EXECUTING, executing)
        await s.commit()
    return _Outcome("run")


async def _run(rc: RunCtx, c: _Call) -> tuple[Any, Exception | None]:
    """真正执行（不碰事件日志，可并发）。写工具执行抛异常 → (None, exc) 走结果未知；只读经网关失败作为工具错误结果；
    只读的其他异常照常上抛（未留下副作用，续跑时直接重执行）。"""
    assert c.tool is not None and c.gate is not None
    try:
        return await execute_tool(c.tool, c.effective, rc=rc, call_id=c.call.call_id, idempotency_key=c.gate.idempotency_key), None
    except GatewayToolError as exc:
        if c.tool.risk_level != RiskLevel.READ:
            return None, exc
        # 只读调用经网关失败（用户令牌过期、网关不可达等）不会留下副作用：作为工具错误结果交还模型，Run 继续
        return {"error": f"tool unavailable: {exc}"}, None
    except Exception as exc:
        if c.tool.risk_level == RiskLevel.READ:
            raise
        return None, exc


async def _finish(rc: RunCtx, c: _Call, result: Any, exc: Exception | None, deduplicated: bool) -> _Outcome:
    """执行后：结果未知转对账；否则写回登记行、TOOL_EXECUTED、post_tool、污点传播、结果外置与 TOOL_RESULT，并写入历史。"""
    assert c.tool is not None and c.gate is not None
    call, tool, operation_id = c.call, c.tool, c.gate.idempotency_key
    script_event: dict[str, Any] = {}
    if tool.name == "run_skill_script" and exc is None and not deduplicated:
        from runtime.skills.tools import ToolResult as SkillToolResult

        if isinstance(result, SkillToolResult):
            script_event = result.event
            result = result.output
    async with SessionLocal() as s:
        log = EventLog(s)
        if exc is not None:
            return _Outcome("suspended", [ev async for ev in _unknown_result(log, s, rc, call, tool, operation_id, exc)])
        if deduplicated:
            result = {"deduplicated": True}
        else:
            if operation_id is not None:
                # 结果写回登记行并单独提交（不受 fence 约束）：即使本执行者随后因失租写不进 TOOL_EXECUTED，
                # 接管者的对账卡也能看到"已执行"与结果指纹，一键确认。
                await log.update_operation(
                    operation_id, user_input.OP_EXECUTED, hmac_json(result), pending_only=tool.name == "run_skill_script"
                )
                await s.commit()
            await log.append(
                rc.run_id, RunEventType.TOOL_EXECUTED, {"call_id": call.call_id, "tool": tool.name}, idempotency_key=operation_id
            )
        # 业务系统拒绝 / 不存在等作为工具错误
        status: Literal["ok", "error"] = "error" if isinstance(result, dict) and "error" in result else "ok"
        data_class = tool.data_class
        post_extra: dict[str, Any] = {}
        if script_event:
            data_class = Level.highest((data_class, script_event.get("data_class", data_class))).value
            post_extra["skill"] = script_event
        if rc.hooks.has(HookPoint.POST_TOOL):
            raw_digest = hmac_json(result)
            post = await rc.hooks.run(
                HookPoint.POST_TOOL,
                {"call_id": call.call_id, "tool": tool.name, "status": status, "output": result, "data_class": data_class},
            )
            c.hook_records += post.records
            if post.denied:
                # 拒绝 / 出错 / 超时：扣留输出（失败关闭），模型只看到扣留说明
                status, result = "error", {"error": f"output withheld by hook:{post.denied_by}"}
            else:
                result, data_class = post.payload["output"], post.payload["data_class"]
            post_extra = {"hooks": post.records, "raw_output_hmac": raw_digest}
        # 污点传播：本次 run 后续的外发类工具调用会因此被收紧为需审批（与续跑重建共用 taint_of）
        rc.gate.taint |= taint_of(status, data_class)
        if tool.name == "read_artifact":
            # read_artifact 读回的就是外置内容：不再外置，否则模型永远读不到原文，只能反复读取直到轮数耗尽
            summary, ref = result, None
        else:
            summary, ref = ctx.offload_if_large(result, rc.workspace, settings.tool_result_offload_tokens)
            if ref is not None and "confidential" in rc.gate.taint and data_class != "confidential":
                # 外置工件的分级（D88）：工具分级与写入时 Run 已有污点两者取高——机密 Run 里经 internal 工具外置的内容
                # 同样可能夹带机密上下文，附进新 Run 时按 confidential 继承（见 api.messages._artifact_data_class）。
                # 只在污点高于工具分级时记录，其余情况 data_class 即工件分级（日志不变）。
                post_extra["artifact_data_class"] = "confidential"
        # 结果事件带上（post_tool 升级后的）数据分类：续跑时据此重建污点，不依赖届时的工具表（工具定义可能已变化）。
        await log.append(
            rc.run_id,
            RunEventType.TOOL_RESULT,
            {"call_id": call.call_id, "status": status, "output": summary, "output_ref": ref, "data_class": data_class} | post_extra,
        )
        await s.commit()
    rc.history.append({"role": "tool", "call_id": call.call_id, "content": ref or summary})
    return _Outcome("done", [ToolResultEvent(call_id=call.call_id, status=status, output=summary, output_ref=ref)])


async def _execute(rc: RunCtx, c: _Call) -> AsyncIterator[Event]:
    """单个放行调用的执行（写前登记 → 执行 → 落结果）。"""
    begun = await _begin_execute(rc, c)
    if begun.kind == "suspended":
        for ev in begun.events:
            yield ev
        return
    result, exc = (None, None) if begun.kind == "dedup" else await _run(rc, c)
    done = await _finish(rc, c, result, exc, begun.kind == "dedup")
    for ev in done.events:
        yield ev


def _tool_use_message(calls: list[ToolUseEvent], reasoning: str | None) -> dict[str, Any]:
    """模型可见历史中的工具调用消息：单个调用为 tool_use，同批多个调用为一条 tool_uses（按 index，与重放同形）。"""
    uses = [{"call_id": x.call_id, "name": x.tool_name, "input": x.input} for x in calls]
    msg: dict[str, Any] = {"role": "assistant", "tool_use": uses[0]} if len(uses) == 1 else {"role": "assistant", "tool_uses": uses}
    if reasoning:
        msg["reasoning"] = reasoning
    return msg


async def handle_tool(rc: RunCtx, call: ToolUseEvent, already_logged: bool = False, reasoning: str | None = None) -> AsyncIterator[Event]:
    """Gate → (approval) → execute → offload → log. Sets rc.suspended when the run must wait.
    （处理单次工具调用的完整流程：闸口判定 →（如需）等待审批 → 执行 → 结果外置 → 写入事件日志。
    当 Run 必须等待人工干预（如审批）时，将 rc.suspended 置 True，调用方据此提前返回。

    already_logged=True 用于续跑场景：该调用此前已经写过 TOOL_REQUESTED 事件（且已获批准），
    这里只需补完后续的判定与执行，不再重复记录 TOOL_REQUESTED，避免事件日志重复。

    reasoning：产生该工具调用的那一轮模型的思维链文本（没有则为 None）。非空时写入历史中 tool_use 消息的
    顶层 "reasoning" 字段与 TOOL_REQUESTED 事件，使其可从事件日志重建（需要回传推理内容的模型据此续接）。

    Hooks（S5）：pre_tool 在写 TOOL_REQUESTED 之前执行——拒绝则记 denied（reason=hook:<名称>）并写 tool.decision
    审计、不进闸口；改写的入参作为 effective_input 与模型原始 input 一并记入 TOOL_REQUESTED，闸口判定、审批指纹与执行
    一律用 effective_input（历史中的 tool_use 保持模型原始入参）。post_tool 在执行后、结果外置前执行：输出与升级后的
    data_class 进 TOOL_RESULT，原文只留 sha256；拒绝 / 出错 / 超时则扣留输出、status=error。already_logged（续跑）时
    不重跑 pre_tool：调用方传入的 call.input 即事件日志中的 effective_input。

    同一轮的多个工具调用（并行，S9）见 handle_batch。）
    """
    rc.hook_records = []
    c = await _pre_tool(rc, call, already_logged)
    rc.hook_records = c.hook_records
    if not already_logged:
        rc.history.append(_tool_use_message([call], reasoning))
    async with SessionLocal() as s:
        log = EventLog(s)
        if not already_logged:
            await log.append(rc.run_id, RunEventType.TOOL_REQUESTED, _requested_payload(c, reasoning))
        out = await _decide(rc, log, s, c)
        if out.kind == "approval":
            assert c.tool is not None
            await log.transition(rc.run_id, RunState.AUTH_REQUIRED, f"tool {c.tool.name} needs approval")
            await s.commit()
            rc.suspended = True
            card = CardEvent(card_type=CardType.IMPACT_PREVIEW, payload=_approval_card(rc, c))
            out = _Outcome("suspended", [card, DoneEvent(run_id=rc.run_id, stop_reason="awaiting")])
        elif out.kind == "execute":
            await s.commit()
    for ev in out.events:
        yield ev
    if out.kind == "execute":
        async for ev in _execute(rc, c):
            yield ev


@dataclass
class BatchItem:
    """批内的一个调用：call 与它在批中的位置 {id, index, size}（续跑时取自 TOOL_REQUESTED 的 batch）。"""

    call: ToolUseEvent
    batch: dict[str, Any]


def _sort_batch_results(rc: RunCtx, order: dict[str, int]) -> None:
    """同批工具结果在历史中按 index 排列（与 invariant.rebuild_messages 同形）：批消息之后只会是本批的结果。"""
    start = next(
        (
            i + 1
            for i in range(len(rc.history) - 1, -1, -1)
            if rc.history[i].get("role") == "assistant" and any(u.get("call_id") in order for u in rc.history[i].get("tool_uses") or [])
        ),
        None,
    )
    if start is None:
        return
    # 续跑时 order 只含尚无结果的调用：位置以批消息 tool_uses 的顺序（即 index 顺序）为准
    position = {str(u.get("call_id")): i for i, u in enumerate(rc.history[start - 1]["tool_uses"])}
    tail = rc.history[start:]
    if all(m.get("role") == "tool" and str(m.get("call_id")) in position for m in tail):
        rc.history[start:] = sorted(tail, key=lambda m: position[str(m.get("call_id"))])


async def handle_batch(
    rc: RunCtx, items: list[BatchItem], already_logged: bool = False, reasoning: str | None = None
) -> AsyncIterator[Event]:
    """同一轮模型输出的多个工具调用（并行工具调用，S9 D149）。

    1. 依次执行 pre_tool，历史追加一条 tool_uses 消息，按 index 在一个事务里写全部 TOOL_REQUESTED（带 batch）；
    2. 逐条过闸口（顺序与 index 一致）：拒绝 / 未知工具当场落结果；request_user_input 挂起即停（其余调用续跑时再处理）；
    3. 放行的只读调用并发执行（PARALLEL_TOOL_MAX；事件日志写入仍串行），写调用按 index 串行，遇结果未知即停；
       排在首个待审批调用之后的写调用不执行（等审批后按序续跑）；
    4. 同批待审批的调用合成一张影响预览卡（payload.calls[]），Run 迁 auth-required 挂起；全部批准后续跑。
    历史中的结果按 index 排列，与重放同形。already_logged=True 为续跑：只处理尚无结果的调用，不重写 TOOL_REQUESTED。"""
    rc.hook_records = []
    order = {it.call.call_id: int(it.batch.get("index", 0)) for it in items}
    calls: list[_Call] = []
    for it in sorted(items, key=lambda x: order[x.call.call_id]):
        c = await _pre_tool(rc, it.call, already_logged)
        c.batch = it.batch
        rc.hook_records += c.hook_records
        calls.append(c)
    if not already_logged:
        rc.history.append(_tool_use_message([c.call for c in calls], reasoning))
        async with SessionLocal() as s:
            log = EventLog(s)
            for i, c in enumerate(calls):
                await log.append(rc.run_id, RunEventType.TOOL_REQUESTED, _requested_payload(c, reasoning if i == 0 else None))
            await s.commit()
    try:
        approvals: list[_Call] = []
        allowed: list[_Call] = []
        for c in calls:
            async with SessionLocal() as s:
                out = await _decide(rc, EventLog(s), s, c)
                await s.commit()
            for ev in out.events:
                yield ev
            if out.kind == "suspended":
                return
            if out.kind == "approval":
                approvals.append(c)
            elif out.kind == "execute":
                allowed.append(c)
        reads = [c for c in allowed if c.tool is not None and c.tool.risk_level == RiskLevel.READ]
        writes = [c for c in allowed if c not in reads]
        async for ev in _execute_reads(rc, reads):
            yield ev
        if rc.suspended:
            return
        blocked_from = min((order[c.call.call_id] for c in approvals), default=None)
        for c in writes:
            if blocked_from is not None and order[c.call.call_id] > blocked_from:
                break  # 写按序串行：排在待审批调用之后的写等审批通过后再执行
            async for ev in _execute(rc, c):
                yield ev
            if rc.suspended:
                return  # 遇结果未知（或令牌需刷新）即停
        if approvals:
            async with SessionLocal() as s:
                log = EventLog(s)
                names = ", ".join(sorted({c.tool.name for c in approvals if c.tool is not None}))
                await log.transition(rc.run_id, RunState.AUTH_REQUIRED, f"tools {names} need approval")
                await s.commit()
            rc.suspended = True
            payload = _approval_card(rc, approvals[0]) | {
                "calls": [{k: v for k, v in _approval_card(rc, c).items() if k != "run_id"} for c in approvals]
            }
            yield CardEvent(card_type=CardType.IMPACT_PREVIEW, payload=payload)
            yield DoneEvent(run_id=rc.run_id, stop_reason="awaiting")
    finally:
        _sort_batch_results(rc, order)
        rc.hook_records = [r for c in calls for r in c.hook_records]


async def _execute_reads(rc: RunCtx, reads: list[_Call]) -> AsyncIterator[Event]:
    """只读调用并发执行：写前记录与结果落日志按 index 串行（事件日志按 seq 追加，不能并发写），只有执行本身并发
    （上限 PARALLEL_TOOL_MAX）。某个调用抛出非预期异常时，其余调用的结果照常落日志后再上抛（同单个只读调用）。"""
    begun: list[tuple[_Call, bool]] = []
    for c in reads:
        out = await _begin_execute(rc, c)
        if out.kind == "suspended":
            for ev in out.events:
                yield ev
            return
        begun.append((c, out.kind == "dedup"))
    sem = asyncio.Semaphore(max(1, settings.parallel_tool_max))

    async def one(c: _Call, dedup: bool) -> tuple[Any, Exception | None]:
        if dedup:
            return None, None
        async with sem:
            return await _run(rc, c)

    results = await asyncio.gather(*(one(c, d) for c, d in begun), return_exceptions=True)
    failure: BaseException | None = None
    for (c, dedup), res in zip(begun, results, strict=True):
        if isinstance(res, BaseException):
            failure = failure or res
            continue
        done = await _finish(rc, c, res[0], res[1], dedup)
        for ev in done.events:
            yield ev
    if failure is not None:
        raise failure
