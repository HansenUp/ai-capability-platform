// 平台基线策略（策略即代码）。每条规则的 @id 与 policy_service/baseline.py 中的规则名一一对应，
// cedar-engine 以 @id 作为策略 id 返回（policy_ids），缺失或重复的 @id 会让整份策略加载失败。
// Cedar 语义：forbid 优先于 permit；未命中任何 permit 时默认拒绝（fail-closed）。
// Cedar 只返回 allow / deny；require_approval 由策略服务用两次查询合成：
// approved=false 判定为 deny 且 approved=true 判定为 allow → require_approval。

// 只读级能力在调用者所属租户内放行。
@id("permit:read_in_tenant")
permit(principal, action == Action::"invoke", resource)
when { resource.risk_level == "read" && principal.tenant == resource.tenant };

// 低风险写操作在租户内放行。
@id("permit:write_low_in_tenant")
permit(principal, action == Action::"invoke", resource)
when { resource.risk_level == "write_low" && principal.tenant == resource.tenant };

// 高风险写操作必须同时满足：调用者与能力同租户 + 带有明确的人工审批。
// 审批不能替代基础权限：approved 只是在"本来就有权限"之上追加的人工确认，缺少租户归属时不得放行。
// 未审批时不命中本条（approved=true 的第二次查询命中），策略服务据此合成 require_approval。
@id("permit:write_high_approved")
permit(principal, action == Action::"invoke", resource)
when { resource.risk_level == "write_high" && principal.tenant == resource.tenant && context.approved == true };

// Skill 上架审批：只有同租户的 skill-reviewer 角色可以批准（S9，D140）。
@id("permit:approve_skill_reviewer")
permit(principal, action == Action::"approve_skill", resource)
when { principal.roles.contains("skill-reviewer") && principal.tenant == resource.tenant };

// 双人审批：提交人不能批准自己提交的 Skill（forbid 优先，任何 permit 都无法绕过）。
@id("forbid:self_approve_skill")
forbid(principal, action == Action::"approve_skill", resource)
when { resource.submitter == principal };

// 跨租户调用一律禁止，作为租户边界的兜底（对所有动作生效）。
// Cedar 多条 permit 取并集：只在单条 permit 上加租户条件，后续新增的 permit 仍可能绕过租户边界；
// forbid 优先于 permit，因此这里显式禁止跨租户，确保任何 permit（含带审批的高风险写）都无法绕过。
@id("forbid:cross_tenant")
forbid(principal, action, resource)
when { principal.tenant != resource.tenant };

// 代理执行的 Agent 与能力不同租户时一律禁止（对所有动作生效）：
// 用户与能力同租户也不足以放行另一租户的 Agent 代为调用，审批同样不能绕过。
@id("forbid:agent_cross_tenant")
forbid(principal, action, resource)
when { context has agent && context.agent.tenant != resource.tenant };

// 数据污点规则：本次 Run 一旦读取过 confidential 级数据，
// 再调用出站类（system == "outbound"）能力必须先审批，防止敏感数据外泄。
@id("forbid:taint_outbound")
forbid(principal, action == Action::"invoke", resource)
when { context.taint.contains("confidential") && resource.system == "outbound" && context.approved == false };
