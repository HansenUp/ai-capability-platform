"""Tool gate. Fixed decision order:
hooks → deny rules → policy engine (Cedar service) → risk level → approval → idempotency key → sandbox level.
Default is deny. Data-classification taint is applied as part of the decision; egress is enforced by runtime.net.
（工具闸口：固定判定顺序为 hooks → 红线拒绝规则 → 策略引擎（Cedar 服务）→ 风险等级 →
审批 → 幂等键 → 沙箱级别。默认拒绝（default deny）。数据分类污点
（data-classification taint）传播是判定逻辑的一部分，用于收紧对外发送类工具。出站白名单（S9.6）由统一出站客户端
runtime.net 按 scheme + host + port 精确匹配执行（本模块原有的前缀匹配 egress_allowed 无调用方且可被绕过，已删除）。
注意：hooks 环节由平台统一执行（S5，runtime.guard.hooks）：工具代理（loop.toolbroker.handle_tool）在本模块 evaluate()
之前执行 pre_tool 链，拒绝即不进闸口；改写后的 effective_input 才是送进 evaluate() 的入参。
本模块实现的是 deny → policy → risk → approval → idempotency → sandbox 这几步。
策略引擎（S8）：按冻结契约 1 调用策略服务 /authz/check-batch，主体属性取自验签令牌、资源属性取自能力目录；
策略服务不可达或响应不合规一律拒绝（policy_unavailable，失败关闭），不再回退到本地默认放行。）
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import jwt

from ai_contract import RiskLevel, SandboxLevel, ToolSpec
from runtime import net
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
    run_id: str | None = None  # 所属 Run：写操作的幂等键（= t_tool_operation.operation_id）以它限定作用域
    agent_version: str = ""  # Agent 版本（策略请求 context.agent.version）
    # 主体属性（策略请求 principal，契约 1）：sub / 租户 / realm 角色取自验签令牌（见 principal_attrs）
    principal_sub: str | None = None
    principal_tenant: str | None = None
    roles: tuple[str, ...] = ()
    # 能力目录给出的资源属性（工具名 → {system, tenant}）：组装器与两级发现写入，策略请求 resource 用
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class GateResult:
    """一次闸口判定的结果。"""

    decision: Decision  # 最终决策
    reason: str  # 决策原因说明
    idempotency_key: str | None = None  # 为写操作生成的幂等键
    sandbox_level: SandboxLevel = SandboxLevel.READ_ONLY  # 本次调用应使用的沙箱级别
    policy_decision_id: str | None = None  # 策略引擎返回的决策 ID，便于审计追溯


POLICY_TIMEOUT_S = 2.0  # 策略服务单次判定的超时（秒）
POLICY_TOKEN_HEADER = "X-Policy-Service-Token"  # noqa: S105 —— 头名而非密钥：策略服务的服务间鉴权头
POLICY_UNAVAILABLE = "policy_unavailable"  # 策略服务不可达 / 响应不合规时的拒绝原因（失败关闭）
_DECISIONS = ("allow", "deny", "require_approval")

_policy_transport: httpx.AsyncBaseTransport | None = None  # 测试注入


def set_policy_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试注入：策略服务请求改经给定 transport。"""
    global _policy_transport
    _policy_transport = transport


def principal_attrs(subject: str, token: str | None, fallback_tenant: str) -> tuple[str, str, tuple[str, ...]]:
    """策略请求的主体属性 (sub, tenant, roles)。

    强制鉴权时取自发起人访问令牌——该令牌在 API 入口已验签（持久化执行的令牌也是验签后加密落库的），这里只读声明：
    sub、租户（JWT_TENANT_CLAIM）、realm 角色。开发态（无令牌或不是 JWT）：sub 取主体去掉类型前缀，租户取 Agent 所属租户
    （开发态 Agent 正是按请求头租户选出的），无角色。"""
    sub, tenant = subject.split(":", 1)[-1], fallback_tenant
    roles: tuple[str, ...] = ()
    if settings.auth_required and token:
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return sub, tenant, roles
        if isinstance(claims.get("sub"), str) and claims["sub"]:
            sub = claims["sub"]
        if isinstance(claims.get(settings.jwt_tenant_claim), str) and claims[settings.jwt_tenant_claim]:
            tenant = claims[settings.jwt_tenant_claim]
        realm = claims.get("realm_access")
        raw = realm.get("roles", []) if isinstance(realm, dict) else []
        roles = tuple(sorted(str(r) for r in raw)) if isinstance(raw, list) else ()
    return sub, tenant, roles


def policy_request(ctx: GateContext, tool: ToolSpec, approved: bool) -> dict[str, Any]:
    """契约 1 的单条判定请求：principal 取自验签令牌；resource 属性取自能力目录（目录未给租户时视为与 Agent 同租户，
    与基线规则"资源未声明租户视为同租户"一致）；context 带审批状态、污点与执行 Agent。"""
    meta = ctx.resources.get(tool.name, {})
    return {
        "principal": {"sub": ctx.principal_sub or ctx.subject, "tenant": ctx.principal_tenant or ctx.tenant_id, "roles": list(ctx.roles)},
        "action": "invoke",
        "resource": {
            "id": tool.capability_id or tool.name,
            "tenant": meta.get("tenant") or ctx.tenant_id,
            "system": meta.get("system") or "",
            "risk_level": tool.risk_level.value,
            "data_class": tool.data_class,
        },
        "context": {
            "approved": approved,
            "taint": sorted(ctx.taint),
            "agent": {"id": ctx.agent_id, "version": ctx.agent_version, "tenant": ctx.tenant_id},
        },
    }


async def _policy_check(ctx: GateContext, tool: ToolSpec, approved: bool) -> tuple[Decision, str, str]:
    """调用策略服务 ``POST /authz/check-batch``（契约 1，单条请求）判定，返回 (决策, 判定标识, 原因)。

    失败关闭：策略服务不可达、超时、非 200 或响应不合规一律 deny（原因 policy_unavailable），不再回退到本地默认放行。
    判定标识形如 ``<engine>@<policy_version>:<命中策略 id>``，随 TOOL_DECISION 与工具审计落库，可与策略服务的判定记录对照。"""
    body = {"requests": [policy_request(ctx, tool, approved)]}
    try:
        headers = {POLICY_TOKEN_HEADER: settings.policy_service_token} if settings.policy_service_token else {}
        async with net.client(timeout=POLICY_TIMEOUT_S, transport=_policy_transport) as c:
            r = await c.post(f"{settings.policy_url.rstrip('/')}/authz/check-batch", json=body, headers=headers)
        if r.status_code != 200:
            return "deny", POLICY_UNAVAILABLE, POLICY_UNAVAILABLE
        data = r.json()
        result = data["results"][0]
        decision = result["decision"]
        if decision not in _DECISIONS:
            return "deny", POLICY_UNAVAILABLE, POLICY_UNAVAILABLE
        ids = result.get("policy_ids") or []
        decision_id = f"{data.get('engine', '?')}@{data.get('policy_version', '?')}:{'+'.join(str(i) for i in ids) or '-'}"
        return decision, decision_id, str(result.get("reason") or "policy")
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return "deny", POLICY_UNAVAILABLE, POLICY_UNAVAILABLE


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
    # 2) policy engine（策略引擎判定，失败关闭）：approved 为该调用是否持有有效审批（参数指纹一致且未过期）
    approved = _approval_valid(ctx.approved_call_ids.get(call_id), params_hash(tool_input))
    decision, decision_id, _why = await _policy_check(ctx, tool, approved)
    if decision == "deny":
        return GateResult("deny", POLICY_UNAVAILABLE if decision_id == POLICY_UNAVAILABLE else "policy", policy_decision_id=decision_id)
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
    if decision == "require_approval" and approved:
        decision = "allow"
    # 5) idempotency key for writes（为写操作或显式要求幂等键的工具生成幂等键，避免重放时重复执行）
    key = None
    if tool.risk_level != RiskLevel.READ or tool.requires_idempotency_key:
        key = idempotency_key(ctx, call_id, tool_input)
    # 6) sandbox level: fail-closed — never escalate beyond the tool's declared level
    # （沙箱级别：失败即拒绝，绝不允许超出工具自身声明的沙箱级别。）
    sandbox = tool.sandbox_level
    return GateResult(decision, "ok" if decision == "allow" else decision, key, sandbox, decision_id)


IDEMPOTENCY_KEY_MAX = 80  # 与 t_run_event.idempotency_key / t_tool_operation.operation_id 列宽一致


def idempotency_key(ctx: GateContext, call_id: str, tool_input: dict[str, Any]) -> str:
    """写操作的幂等键（S7 起同时是全局操作标识 operation_id，经能力网关 idempotency_key 透传给下游）：
    ``{run_id}:{call_id}:{入参指纹}``——以 Run 限定作用域：同一 Run 内重放 / 接管得到同一个键（去重、对账），不同 Run 即使
    call_id 与入参相同（如脚本化 harness 的固定 call_id）也是不同的操作，既不会被误判为"已执行"而跳过，也不会撞上他人
    未完成的登记而卡在对账。下游据此按"Run + 调用"去重。call_id 过长时以其指纹代替，保证不超过列宽。
    未给 run_id（直接构造的闸口上下文）时退回旧格式 ``{agent_id}:{call_id}:{入参指纹}``。"""
    digest = uuid.uuid5(uuid.NAMESPACE_URL, str(sorted(tool_input.items()))).hex[:12]
    scope = ctx.run_id or ctx.agent_id
    key = f"{scope}:{call_id}:{digest}"
    if len(key) > IDEMPOTENCY_KEY_MAX:
        key = f"{scope}:{uuid.uuid5(uuid.NAMESPACE_URL, call_id).hex[:16]}:{digest}"[:IDEMPOTENCY_KEY_MAX]
    return key
