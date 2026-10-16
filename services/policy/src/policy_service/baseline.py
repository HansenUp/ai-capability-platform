"""策略基线评估器：`cedar/policies.cedar` 的 Python 镜像（开发态回退）。

生产环境由 Cedar 引擎（ADR-0001：判定服务化，sidecar HTTP）评估 `policies.cedar`；
本模块只在本地开发 / CI 没有 Cedar 引擎时使用，必须与 Cedar 策略保持逐条一致，
`cedar/tests.json` 中的用例同时约束两者。

注意：Cedar 原生只返回 allow / deny 两种判定，require_approval 是平台授权服务在其之上合成的第三态；
切换到真实 Cedar 引擎时需要用两次查询（先不带审批、再带审批）或独立策略集来表达这一状态，
不能假设引擎会直接返回它。
"""

from __future__ import annotations

from typing import Any


def evaluate(principal: dict[str, Any], resource: dict[str, Any], context: dict[str, Any]) -> tuple[str, str]:
    """按 Cedar 语义评估一次调用请求，返回 (决策, 命中的规则名)。

    参数：
        principal: 主体属性，至少含 `tenant`（租户）。
        resource: 能力属性，含 `risk_level`（read / write_low / write_high）、`tenant`、`system`。
        context: 调用上下文，含 `approved`（是否已人工审批）与 `taint`（本次 Run 已触碰的数据分级集合）。
    返回：
        决策取值 allow / deny / require_approval；规则名与 policies.cedar 中的注释一一对应，便于审计追溯。
    """
    # 同租户判断：资源未声明租户时视为与主体同租户
    tenant_ok = principal.get("tenant", "default") == resource.get("tenant", principal.get("tenant", "default"))
    risk = resource.get("risk_level", "read")  # 能力风险级别，缺省按只读处理
    approved = bool(context.get("approved", False))  # 是否已经过审批
    taint = set(context.get("taint", []))  # 数据分级污点：本次 Run 读过的敏感数据类别
    # forbid 规则优先（Cedar 语义：forbid 覆盖 permit）
    # 跨租户调用一律拒绝，优先级高于所有 permit：审批不能替代基础权限，approved 不是跨租户的理由
    if not tenant_ok:
        return "deny", "forbid:cross_tenant"
    # 一旦读过 confidential 数据，再调用出站类能力必须先审批，防止数据外泄
    if "confidential" in taint and resource.get("system") == "outbound" and not approved:
        return "require_approval", "forbid:taint_outbound"
    # 只读能力：同租户内放行
    if risk == "read" and tenant_ok:
        return "allow", "permit:read_in_tenant"
    # 低风险写：同租户内放行
    if risk == "write_low" and tenant_ok:
        return "allow", "permit:write_low_in_tenant"
    # 高风险写：必须同租户；同租户且已审批才放行，同租户但未审批要求审批（Runtime 据此挂起 Run 为 auth-required）。
    # 非同租户的分支在上面的 forbid:cross_tenant 已经拒绝，这里保留 tenant_ok 条件与 Cedar permit 逐条对应。
    if risk == "write_high" and tenant_ok:
        return ("allow", "permit:write_high_approved") if approved else ("require_approval", "permit:write_high_needs_approval")
    # 未命中任何 permit：默认拒绝（fail-closed）
    return "deny", "default_deny"
