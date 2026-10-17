"""Tool gate. Fixed decision order:
hooks → deny rules → policy engine (Cedar service) → risk level → approval → idempotency key → sandbox level.
Default is deny. Egress allowlist and data-classification taint are applied as part of the decision.
（工具闸口：固定判定顺序为 hooks → 红线拒绝规则 → 策略引擎（Cedar 服务）→ 风险等级 →
审批 → 幂等键 → 沙箱级别。默认拒绝（default deny）。出站白名单与数据分类污点
（data-classification taint）传播是判定逻辑的一部分，用于收紧对外发送类工具。
注意：hooks 环节由平台统一执行（S5，runtime.guard.hooks）：工具代理（loop.toolbroker.handle_tool）在本模块 evaluate()
之前执行 pre_tool 链，拒绝即不进闸口；改写后的 effective_input 才是送进 evaluate() 的入参。
本模块实现的是 deny → policy → risk → approval → idempotency → sandbox 这几步。）
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from ai_contract import RiskLevel, SandboxLevel, ToolSpec
from runtime.config import settings

Decision = Literal["allow", "deny", "require_approval"]  # 闸口最终决策的三种取值

# 出站发送类工具的前缀：邮件/即时消息/HTTP/Webhook；命中 confidential 污点时会收紧这些工具。
EXTERNAL_SEND_TOOL_PREFIXES = ("mail.", "im.", "http.", "webhook.")


@dataclass
class GateContext:
    """一次 Run 内闸口判定所需的上下文状态，贯穿整个 Run 生命周期演进。"""

    subject: str  # 调用主体
    agent_id: str  # Agent 标识
    tenant_id: str = "default"  # 租户标识
    taint: set[str] = field(default_factory=set)  # data classes read so far in this run（本次 run 中已读取过的数据分类集合，用于污点传播）
    deny_tools: set[str] = field(default_factory=set)  # persona/platform redlines（人设/平台级红线工具名单）
    # 已批准调用的绑定信息：键为 call_id，值含批准时的 params_hash 与过期时间（ISO 字符串）。
    # 审批附着于"具体参数 + 具体资源 + 有效期"，而不是一个光秃秃的 call_id。
    approved_call_ids: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class GateResult:
    """一次闸口判定的结果。"""

    decision: Decision  # 最终决策
    reason: str  # 决策原因说明
    idempotency_key: str | None = None  # 为写操作生成的幂等键
    sandbox_level: SandboxLevel = SandboxLevel.READ_ONLY  # 本次调用应使用的沙箱级别
    policy_decision_id: str | None = None  # 策略引擎返回的决策 ID，便于审计追溯


async def _policy_check(ctx: GateContext, tool: ToolSpec, tool_input: dict[str, Any]) -> tuple[Decision, str]:
    """Call the policy service; if unreachable in dev, fall back to risk-based defaults.
    （调用策略服务（Cedar）做鉴权判定；开发环境下若策略服务不可达，回退到基于风险等级的默认策略。）
    """
    body = {
        "subject": ctx.subject,
        "agent": ctx.agent_id,
        "capability": tool.capability_id or tool.name,
        "action": "invoke",
        "resource": {"risk_level": tool.risk_level, "data_class": tool.data_class},
        "context": {"tenant": ctx.tenant_id, "taint": sorted(ctx.taint)},
    }
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.post(f"{settings.policy_url}/authz/check", json=body)
            r.raise_for_status()
            data = r.json()
            return data["decision"], data.get("decision_id", "")
    except (httpx.HTTPError, KeyError):
        # Dev fallback mirrors the Cedar baseline: reads allowed, low writes allowed, high writes need approval.
        # （开发态回退策略，模拟 Cedar 基线规则：只读放行，低风险写放行，高风险写需要审批。）
        if tool.risk_level == RiskLevel.WRITE_HIGH:
            return "require_approval", "fallback:write_high"
        return "allow", "fallback:baseline"


def params_hash(tool_input: dict[str, Any]) -> str:
    """对工具入参做规范化 JSON（按 key 排序、无多余空白）后取 sha256，截前 16 位作为参数指纹。

    审批绑定与写前记录都用它标识"这一次批的到底是哪一组参数"。
    """
    canon = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _approval_valid(approval: dict[str, Any] | None, current_hash: str) -> bool:
    """判断一条已记录的审批对当前调用是否仍然有效：参数指纹一致且未过期。"""
    if not approval or approval.get("params_hash") != current_hash:
        return False
    expires_at = approval.get("expires_at")
    if not expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return datetime.now(UTC) < deadline


async def evaluate(ctx: GateContext, tool: ToolSpec, tool_input: dict[str, Any], call_id: str) -> GateResult:
    """按固定顺序执行闸口判定：deny 规则 → 策略引擎 → 风险等级/污点收紧 → 审批放行 → 幂等键 → 沙箱级别。"""
    # 1) deny rules (redlines)（红线拒绝规则：命中即刻拒绝，优先级最高）
    if tool.name in ctx.deny_tools:
        return GateResult("deny", "redline")
    # 2) policy engine（策略引擎判定）
    decision, decision_id = await _policy_check(ctx, tool, tool_input)
    if decision == "deny":
        return GateResult("deny", "policy", policy_decision_id=decision_id)
    # 3) risk level + taint: reading confidential data tightens outbound tools
    # （风险等级与污点收紧：若本次 run 已读取过机密数据，任何外发类工具都需要人工审批。）
    if "confidential" in ctx.taint and tool.name.startswith(settings.confidential_blocked_tool_prefixes):
        # 数据分级矩阵（S4.9）：机密污点下，检索 / 外部查询类服务一律阻断（查询语句本身就会带出机密上下文）
        return GateResult("deny", "data_class:confidential", policy_decision_id=decision_id)
    if "confidential" in ctx.taint and tool.name.startswith(EXTERNAL_SEND_TOOL_PREFIXES):
        decision = "require_approval"
    if tool.risk_level == RiskLevel.WRITE_HIGH and decision == "allow":
        decision = "require_approval"
    # 4) approval already granted for this call?（该调用是否已经获得过人工批准，若是则直接放行）
    # 放行必须同时满足三个条件：call_id 已批准、当前参数指纹与批准时一致、且尚未过期。
    # 审批附着于具体参数与资源，参数一旦变化或审批超时，都必须重新走一次人工审批，不得顺延复用。
    if decision == "require_approval" and _approval_valid(ctx.approved_call_ids.get(call_id), params_hash(tool_input)):
        decision = "allow"
    # 5) idempotency key for writes（为写操作或显式要求幂等键的工具生成幂等键，避免重放时重复执行）
    key = None
    if tool.risk_level != RiskLevel.READ or tool.requires_idempotency_key:
        key = f"{ctx.agent_id}:{call_id}:{uuid.uuid5(uuid.NAMESPACE_URL, str(sorted(tool_input.items()))).hex[:12]}"
    # 6) sandbox level: fail-closed — never escalate beyond the tool's declared level
    # （沙箱级别：失败即拒绝，绝不允许超出工具自身声明的沙箱级别。）
    sandbox = tool.sandbox_level
    return GateResult(decision, "ok" if decision == "allow" else decision, key, sandbox, decision_id)


def egress_allowed(url: str) -> bool:
    """Only registered MCP endpoints, the model gateway and explicit allowlist entries may be reached.
    （只有已注册的 MCP 端点、模型网关以及显式配置的白名单条目才允许被访问；其余一律拒绝出站。）
    """
    allowed = (settings.model_gateway_url, settings.capability_gateway_url, settings.policy_url, *settings.egress_allowlist)
    return any(url.startswith(a) for a in allowed if a)
