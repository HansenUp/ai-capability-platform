// 平台基线策略（策略即代码）。规则名与 policy_service/baseline.py 中的返回值一一对应。
// Cedar 语义：forbid 优先于 permit；未命中任何 permit 时默认拒绝（fail-closed）。

// permit:read_in_tenant —— 只读级能力在调用者所属租户内放行。
permit(principal, action == Action::"invoke", resource)
when { resource.risk_level == "read" && principal.tenant == resource.tenant };

// permit:write_low_in_tenant —— 低风险写操作在租户内放行。
permit(principal, action == Action::"invoke", resource)
when { resource.risk_level == "write_low" && principal.tenant == resource.tenant };

// permit:write_high_approved —— 高风险写操作必须同时满足：调用者与能力同租户 + 带有明确的人工审批。
// 审批不能替代基础权限：approved 只是在"本来就有权限"之上追加的人工确认，缺少租户归属时不得放行。
// 未审批时不命中本条，Runtime 闸口将 Run 挂起为 auth-required 等待审批。
permit(principal, action == Action::"invoke", resource)
when { resource.risk_level == "write_high" && principal.tenant == resource.tenant && context.approved == true };

// forbid:cross_tenant —— 跨租户调用一律禁止，作为租户边界的兜底。
// Cedar 多条 permit 取并集：只在单条 permit 上加租户条件，后续新增的 permit 仍可能绕过租户边界；
// forbid 优先于 permit，因此这里显式禁止跨租户，确保任何 permit（含带审批的高风险写）都无法绕过。
// 再次强调：审批不能替代基础权限，approved == true 不构成跨租户调用的理由。
forbid(principal, action == Action::"invoke", resource)
when { principal.tenant != resource.tenant };

// forbid:taint_outbound —— 数据污点规则：本次 Run 一旦读取过 confidential 级数据，
// 再调用出站类（system == "outbound"）能力必须先审批，防止敏感数据外泄。
forbid(principal, action == Action::"invoke", resource)
when { context.taint.contains("confidential") && resource.system == "outbound" && context.approved == false };
