"""策略基线评估器：`cedar/policies.cedar` 的 Python 镜像（仅在显式 `POLICY_ENGINE=baseline` 时使用）。

生产环境由 Cedar 引擎（`services/policy/cedar-engine`，sidecar HTTP，D126）评估 `policies.cedar`；
本模块必须与 Cedar 策略逐条一致，`cedar/tests.json` 中的用例同时约束两者（含命中的规则 id）。

分两层：
- `authorize`：与 Cedar 原生语义一致，只返回 allow / deny 与"决定性策略"（allow 时为命中的 permit，
  deny 时为命中的 forbid）；
- `synthesize`：两种引擎共用的第三态合成——Cedar 原生只有 allow / deny，require_approval 由两次查询合成：
  approved=false 判定为 deny 且 approved=true 判定为 allow → require_approval。
"""

from __future__ import annotations

from typing import Any

# 一次原生判定：(是否放行, 决定性策略 id 列表, 求值错误列表)
RawDecision = tuple[bool, list[str], list[str]]

# 多条 forbid 同时命中时，reason 取最根本的那条（租户边界优先于污点规则）
FORBID_PRIORITY = ("forbid:cross_tenant", "forbid:agent_cross_tenant", "forbid:taint_outbound")


def authorize(principal: dict[str, Any], action: str, resource: dict[str, Any], context: dict[str, Any]) -> RawDecision:
    """按 Cedar 语义评估一条请求（forbid 优先，未命中 permit 默认拒绝），属性须已齐全。"""
    tenant = principal["tenant"]
    agent = context.get("agent")
    forbids: list[str] = []
    # forbid:cross_tenant —— 跨租户一律禁止（所有动作），审批不能替代基础权限
    if tenant != resource["tenant"]:
        forbids.append("forbid:cross_tenant")
    # forbid:agent_cross_tenant —— 代理执行的 Agent 与能力不同租户一律禁止（所有动作）
    if agent is not None and agent["tenant"] != resource["tenant"]:
        forbids.append("forbid:agent_cross_tenant")
    # forbid:taint_outbound —— 读过 confidential 数据后调用出站类能力必须先审批
    if action == "invoke" and "confidential" in context["taint"] and resource["system"] == "outbound" and not context["approved"]:
        forbids.append("forbid:taint_outbound")
    if forbids:
        return False, sorted(forbids), []
    if action != "invoke":
        return False, [], []  # read_data / approve 当前没有任何 permit
    risk = resource["risk_level"]
    permits: list[str] = []
    if risk == "read":
        permits.append("permit:read_in_tenant")
    if risk == "write_low":
        permits.append("permit:write_low_in_tenant")
    if risk == "write_high" and context["approved"] is True:
        permits.append("permit:write_high_approved")
    return (True, permits, []) if permits else (False, [], [])


def _primary(ids: list[str]) -> str | None:
    """按 FORBID_PRIORITY 选出最能说明原因的策略 id。"""
    for f in FORBID_PRIORITY:
        if f in ids:
            return f
    return ids[0] if ids else None


def synthesize(q1: RawDecision, q2: RawDecision | None) -> tuple[str, list[str], str]:
    """由原生判定合成平台三态，返回 (决策, 策略 id, 原因)。

    q1 为原请求的判定；q2 为把 approved 置为 true 的同一请求的判定（原请求已审批时为 None）。
    q1 放行 → allow；q1 拒绝、q2 放行 → require_approval；否则 deny。求值出错一律 deny。
    """
    allowed, ids, errors = q1
    if allowed and not errors:
        return "allow", ids, ids[0] if ids else "allow"
    if errors:
        return "deny", ids, "policy_error"
    if q2 is not None and q2[0] and not q2[2]:
        return "require_approval", sorted(set(ids) | set(q2[1])), _primary(ids) or (q2[1][0] if q2[1] else "approval_required")
    return "deny", ids, _primary(ids) or "default_deny"


def evaluate(principal: dict[str, Any], resource: dict[str, Any], context: dict[str, Any]) -> tuple[str, str]:
    """兼容旧调用方的单条评估（动作固定为 invoke），返回 (决策, 原因)。

    旧语义保留：资源未声明租户时视为与主体同租户、未声明风险级别按只读处理。
    策略服务的 HTTP 入口不走这里，而是先做严格的请求校验（缺租户 / 风险级别直接 deny）。
    """
    p = {"tenant": principal.get("tenant", "default")}
    r = {
        "tenant": resource.get("tenant", p["tenant"]),
        "risk_level": resource.get("risk_level", "read"),
        "system": resource.get("system", ""),
    }
    ctx: dict[str, Any] = {"approved": context.get("approved") is True, "taint": list(context.get("taint", []))}
    if isinstance(context.get("agent"), dict):
        ctx["agent"] = context["agent"]
    q1 = authorize(p, "invoke", r, ctx)
    q2 = None if ctx["approved"] else authorize(p, "invoke", r, {**ctx, "approved": True})
    decision, _, reason = synthesize(q1, q2)
    return decision, reason
