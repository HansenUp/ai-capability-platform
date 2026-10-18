"""策略服务桩（S8）：按契约 1 实现 ``POST /authz/check-batch``，判定规则与 policies.cedar 基线一致（跨租户 forbid、
机密污点外发需审批、只读 / 低风险写放行、高风险写未审批需审批）。闸口不再在策略不可达时回退放行，runtime 测试经
``gate.set_policy_transport_for_tests`` 注入本桩；专测失败关闭的用例另行注入不可达 / 出错的 transport。"""

from __future__ import annotations

import json
from typing import Any

import httpx

REQUESTS: list[dict[str, Any]] = []  # 收到的判定请求（按序），供用例断言请求体


def decide(req: dict[str, Any]) -> tuple[str, str]:
    """单条判定：返回 (decision, 命中的策略 id)。"""
    principal, resource, context = req["principal"], req["resource"], req["context"]
    if principal["tenant"] != resource["tenant"] or context["agent"]["tenant"] != resource["tenant"]:
        return "deny", "forbid:cross_tenant"
    approved = bool(context.get("approved"))
    if "confidential" in context.get("taint", []) and resource.get("system") == "outbound" and not approved:
        return "require_approval", "forbid:taint_outbound"
    risk = resource.get("risk_level")
    if risk == "read":
        return "allow", "permit:read_in_tenant"
    if risk == "write_low":
        return "allow", "permit:write_low_in_tenant"
    if risk == "write_high":
        return ("allow", "permit:write_high_approved") if approved else ("require_approval", "permit:write_high_needs_approval")
    return "deny", "default_deny"


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path != "/authz/check-batch":
        return httpx.Response(404, json={"detail": "not found"})
    body = json.loads(request.content)
    results = []
    for req in body["requests"]:
        REQUESTS.append(req)
        decision, pid = decide(req)
        results.append({"decision": decision, "policy_ids": [pid], "reason": pid})
    return httpx.Response(200, json={"results": results, "engine": "baseline", "policy_version": "test"})


def transport() -> httpx.MockTransport:
    return httpx.MockTransport(handler)
