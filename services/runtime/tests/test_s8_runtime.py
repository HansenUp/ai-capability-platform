"""S8 Runtime 侧（WP-C）：闸口策略请求（契约 1）与失败关闭、两级发现（元工具拦截 / 上限 / 事件化 / 重放不重检）、
能力网关客户端（服务令牌缓存与契约 3 鉴权头）、统一错误体 ErrorDetail{detail, code}。"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
import runtime_policy_stub as policy_stub
from test_runner_stop_reasons import ScriptedHarness, _events, _log, _publish

from ai_contract import (
    DoneEvent,
    Event,
    HookSet,
    RiskLevel,
    RunEvent,
    RunEventType,
    SessionHandle,
    SkillDir,
    TextEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)
from runtime import gateway_client
from runtime.guard import gate
from runtime.loop import discovery, toolbroker

# ---------------- 闸口：策略请求体（契约 1）与失败关闭 ----------------


def _ctx(**kw: Any) -> gate.GateContext:
    base: dict[str, Any] = {
        "subject": "human:alice",
        "agent_id": "a1",
        "tenant_id": "acme",
        "agent_version": "3",
        "principal_sub": "alice",
        "principal_tenant": "acme",
        "roles": ("platform-user",),
        "resources": {"crm.search": {"system": "crm", "tenant": "acme"}},
    }
    return gate.GateContext(**(base | kw))


CRM = ToolSpec(name="crm.search", description="d", capability_id="cap.crm.search", data_class="confidential")
CLOSE = ToolSpec(name="ticket.close_batch", description="d", capability_id="cap.ticket.close_batch", risk_level=RiskLevel.WRITE_HIGH)


async def test_policy_request_body_follows_contract_1() -> None:
    policy_stub.REQUESTS.clear()
    res = await gate.evaluate(_ctx(taint={"confidential"}), CRM, {"q": "x"}, "c1")
    assert res.decision == "allow" and res.policy_decision_id == "baseline@test:permit:read_in_tenant"
    (req,) = policy_stub.REQUESTS
    assert req == {
        "principal": {"sub": "alice", "tenant": "acme", "roles": ["platform-user"]},
        "action": "invoke",
        "resource": {"id": "cap.crm.search", "tenant": "acme", "system": "crm", "risk_level": "read", "data_class": "confidential"},
        "context": {"approved": False, "taint": ["confidential"], "agent": {"id": "a1", "version": "3", "tenant": "acme"}},
    }


async def test_policy_cross_tenant_resource_is_denied() -> None:
    """资源租户取自能力目录：与主体 / Agent 租户不同即 forbid（以前租户只放 context，跨租户 forbid 从未触发）。"""
    ctx = _ctx(resources={"crm.search": {"system": "crm", "tenant": "other"}})
    res = await gate.evaluate(ctx, CRM, {}, "c1")
    assert res.decision == "deny" and res.reason == "policy" and res.policy_decision_id.endswith("forbid:cross_tenant")
    # 目录未给租户：视为与 Agent 同租户（与基线"资源未声明租户视为同租户"一致）
    assert (await gate.evaluate(_ctx(resources={}), CRM, {}, "c1")).decision == "allow"


async def test_policy_approved_flag_and_write_high_semantics() -> None:
    """高风险写：未审批 → require_approval；持有效审批（参数指纹一致、未过期）→ 请求带 approved=true，放行。"""
    policy_stub.REQUESTS.clear()
    assert (await gate.evaluate(_ctx(), CLOSE, {"ids": ["T-1"]}, "c1")).decision == "require_approval"
    approval = {"params_hash": gate.params_hash({"ids": ["T-1"]}), "expires_at": "2999-01-01T00:00:00+00:00"}
    res = await gate.evaluate(_ctx(approved_call_ids={"c1": approval}), CLOSE, {"ids": ["T-1"]}, "c1")
    assert res.decision == "allow" and res.idempotency_key
    assert [r["context"]["approved"] for r in policy_stub.REQUESTS] == [False, True]
    # 参数变化：审批不再有效，approved=false
    assert (await gate.evaluate(_ctx(approved_call_ids={"c1": approval}), CLOSE, {"ids": ["T-2"]}, "c1")).decision == "require_approval"
    assert policy_stub.REQUESTS[-1]["context"]["approved"] is False


def _raise(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("policy down")


@pytest.mark.parametrize(
    "handler",
    [
        _raise,
        lambda req: httpx.Response(500, json={"detail": "boom"}),
        lambda req: httpx.Response(200, json={"results": []}),
        lambda req: httpx.Response(200, json={"results": [{"decision": "maybe"}]}),
        lambda req: httpx.Response(200, content=b"not json"),
    ],
    ids=["unreachable", "http-500", "no-results", "bad-decision", "not-json"],
)
async def test_policy_unavailable_fails_closed(handler: Any) -> None:
    """策略服务不可达 / 出错 / 响应不合规：一律 deny（policy_unavailable），只读工具也不放行（删除了回退 allow）。"""
    gate.set_policy_transport_for_tests(httpx.MockTransport(handler))
    try:
        res = await gate.evaluate(_ctx(), CRM, {}, "c1")
    finally:
        gate.set_policy_transport_for_tests(policy_stub.transport())
    assert res.decision == "deny" and res.reason == gate.POLICY_UNAVAILABLE and res.policy_decision_id == gate.POLICY_UNAVAILABLE


def test_policy_unavailable_denies_tool_in_run(client: Any) -> None:
    """端到端：策略服务不可达时工具调用记 denied（reason=policy_unavailable），不执行。"""
    gate.set_policy_transport_for_tests(httpx.MockTransport(_raise))
    try:
        body = {"agent_id": "demo", "messages": [{"role": "user", "content": "!tool echo {}"}], "stream": False}
        run_id = client.post("/v1/messages", json=body).json()["run_id"]
    finally:
        gate.set_policy_transport_for_tests(policy_stub.transport())
    log = _log(client, run_id)
    decision = next(e["payload"] for e in log if e["type"] == "tool.decision")
    assert decision["decision"] == "deny" and decision["reason"] == gate.POLICY_UNAVAILABLE
    assert next(e["payload"] for e in log if e["type"] == "tool.result")["status"] == "denied"
    assert not any(e["type"] == "tool.executed" for e in log)


def test_principal_attrs_from_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """主体属性：强制鉴权时取自（入口已验签的）令牌声明；开发态取主体与 Agent 租户。"""
    token = jwt.encode({"sub": "u-1", "tenant": "acme", "realm_access": {"roles": ["b", "a"]}}, "k" * 32, algorithm="HS256")
    assert gate.principal_attrs("human:u-1", token, "default") == ("u-1", "default", ())  # 开发态不读令牌
    monkeypatch.setattr(gate, "settings", dataclasses.replace(gate.settings, auth_required=True))
    assert gate.principal_attrs("human:u-1", token, "default") == ("u-1", "acme", ("a", "b"))
    assert gate.principal_attrs("human:u-2", "not-a-jwt", "t1") == ("u-2", "t1", ())


# ---------------- 能力网关客户端：服务令牌缓存与鉴权头（契约 3） ----------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def svc_auth(monkeypatch: pytest.MonkeyPatch) -> Any:
    """启用服务间鉴权：令牌端点与网关都由 MockTransport 处理；返回记录对象。"""
    rec: dict[str, Any] = {"token_calls": 0, "requests": [], "status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            rec["token_calls"] += 1
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert form == {"grant_type": "client_credentials", "client_id": "ai-runtime-svc", "client_secret": "svc-secret"}
            return httpx.Response(200, json={"access_token": f"svc-{rec['token_calls']}", "expires_in": 40})
        rec["requests"].append(request)
        return httpx.Response(rec["status"], json={"ok": True})

    clock = _Clock()
    s = dataclasses.replace(gateway_client.settings, gateway_token_url="http://idp/token", gateway_client_secret="svc-secret")  # noqa: S106
    monkeypatch.setattr(gateway_client, "settings", s)
    monkeypatch.setattr(gateway_client, "time", clock)
    gateway_client.set_transport_for_tests(httpx.MockTransport(handler))
    rec["clock"] = clock
    yield rec
    gateway_client.set_transport_for_tests(None)


async def test_gateway_client_caches_service_token_and_sends_contract_headers(svc_auth: Any) -> None:
    for _ in range(3):
        await gateway_client.request("POST", "/invoke", user_token="user-tok", timeout_s=1, json={})  # noqa: S106
    assert svc_auth["token_calls"] == 1  # 缓存
    req = svc_auth["requests"][-1]
    assert req.headers["authorization"] == "Bearer svc-1" and req.headers["x-ai-user-token"] == "user-tok"
    svc_auth["clock"].now += 9  # 剩余 31 s：仍可用
    await gateway_client.request("GET", "/capabilities", timeout_s=1)
    assert svc_auth["token_calls"] == 1 and "x-ai-user-token" not in svc_auth["requests"][-1].headers
    svc_auth["clock"].now += 2  # 剩余 29 s < 30 s：提前刷新
    await gateway_client.request("GET", "/capabilities", timeout_s=1)
    assert svc_auth["token_calls"] == 2 and svc_auth["requests"][-1].headers["authorization"] == "Bearer svc-2"
    svc_auth["status"] = 401  # 网关拒绝服务令牌：丢弃缓存，下次重新换取
    await gateway_client.request("GET", "/capabilities", timeout_s=1)
    svc_auth["status"] = 200
    await gateway_client.request("GET", "/capabilities", timeout_s=1)
    assert svc_auth["token_calls"] == 3


async def test_gateway_client_token_failure_is_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    s = dataclasses.replace(gateway_client.settings, gateway_token_url="http://idp/token", gateway_client_secret="x")  # noqa: S106
    monkeypatch.setattr(gateway_client, "settings", s)
    gateway_client.set_transport_for_tests(httpx.MockTransport(lambda req: httpx.Response(401)))
    try:
        with pytest.raises(httpx.HTTPError):  # GatewayAuthError：调用方按网关不可达处理（失败关闭）
            await gateway_client.request("POST", "/invoke", user_token="u", timeout_s=1, json={})  # noqa: S106
        monkeypatch.setattr(gateway_client, "settings", dataclasses.replace(s, gateway_client_secret=None))  # noqa: S106
        with pytest.raises(gateway_client.GatewayAuthError, match="SECRET"):
            await gateway_client.service_token()
    finally:
        gateway_client.set_transport_for_tests(None)


async def test_gateway_client_dev_mode_keeps_user_bearer() -> None:
    """未配置令牌端点（开发态）：沿用此前的用户令牌 Bearer 头，不换服务令牌。"""
    assert not gateway_client.service_auth_enabled()
    assert await gateway_client.headers("user-tok") == {"Authorization": "Bearer user-tok"}
    assert await gateway_client.headers(None) == {}


# ---------------- 两级发现 ----------------


class ToolsHarness(ScriptedHarness):
    """另外记录每轮收到的工具名列表。"""

    def __init__(self, name: str, turns: list[list[Any]]) -> None:
        super().__init__(name, turns)
        self.tools_seen: list[list[str]] = []

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        self.tools_seen.append([t.name for t in tools])
        async for ev in super().run_turn(session, turn, tools, skills, hooks):
            yield ev


def _call(name: str, args: dict[str, Any], call_id: str) -> list[Any]:
    return [ToolUseEvent(call_id=call_id, tool_name=name, input=args), UsageEvent(), DoneEvent(run_id="x", stop_reason="tool_use")]


def _final(text: str = "好的") -> list[Any]:
    return [TextEvent(delta=text), UsageEvent(), DoneEvent(run_id="x")]


ITEMS = [
    {
        "id": "cap.crm.opportunity.search",
        "name": "crm.opportunity.search",
        "description": "查询商机",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "risk_level": "read",
        "data_class": "internal",
        "system": "crm",
        "decision": "allow",
        "score": 0.9,
    },
    {
        "id": "cap.crm.opportunity.close",
        "name": "crm.opportunity.close",
        "description": "关闭商机（高风险写）",
        "input_schema": {"type": "object"},
        "risk_level": "write_high",
        "data_class": "internal",
        "system": "crm",
        "tenant": "default",
        "decision": "require_approval",
        "score": 0.8,
        "supports_dry_run": True,
    },
    {"id": "cap.mail.send", "name": "mail.send", "description": "发邮件", "risk_level": "write_low", "system": "outbound", "score": 0.5},
    {"id": "cap.x", "name": "read_artifact", "description": "保留名不得覆盖", "score": 0.4},
    {"name": "no-id", "description": "缺 id 丢弃"},
    {"id": "cap.bad", "name": "bad.risk", "description": "风险等级不合规丢弃", "risk_level": "nuclear"},
]


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Any:
    """模拟能力网关（工具经网关分发）：/capabilities 503（组装回退本地注册表）、/capabilities/search 返回 ITEMS、/invoke 成功。"""
    rec: dict[str, Any] = {"search": [], "invoke": []}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/capabilities/search":
            rec["search"].append({"body": json.loads(req.content), "headers": dict(req.headers)})
            return httpx.Response(200, json={"items": ITEMS, "degraded": False})
        if req.url.path == "/invoke":
            body = json.loads(req.content)
            rec["invoke"].append(body)
            return httpx.Response(200, json={"ok": True, "status_code": 200, "output": {"tool": body["tool"], "dry_run": body["dry_run"]}})
        return httpx.Response(503)

    monkeypatch.setattr(toolbroker, "settings", dataclasses.replace(toolbroker.settings, tool_dispatch="gateway"))
    toolbroker.set_gateway_transport_for_tests(httpx.MockTransport(handler))
    yield rec
    toolbroker.set_gateway_transport_for_tests(None)


DISCOVERY = {"tools": ["ticket.search"], "discovery": {"enabled": True, "max_tools": 2, "scope": ["crm"]}}


def test_discovery_intercepts_caps_events_and_gates(client: Any, gateway: Any) -> None:
    policy_stub.REQUESTS.clear()
    turns = [
        _call("search_capabilities", {"query": "查询并关闭商机"}, "c1"),
        _call("crm.opportunity.search", {"q": "O-1"}, "c2"),
        _call("crm.opportunity.close", {"id": "O-1"}, "c3"),
        _final("商机已关闭"),
    ]
    harness = ToolsHarness("disc-agent", turns)
    _publish(client, "disc-agent", harness, DISCOVERY)
    msgs = [{"role": "user", "content": "关闭商机 O-1"}]
    with client.stream("POST", "/v1/messages", json={"agent_id": "disc-agent", "messages": msgs}) as resp:
        evs = _events(resp)
    run_id = evs[-1]["run_id"]
    log = _log(client, run_id)

    assembled = next(e for e in log if e["type"] == "context.assembled")["payload"]
    assert assembled["tools"] == ["ticket.search", "search_capabilities", "read_artifact"]
    assert assembled["discovery"] == {"scope": ["crm"], "max_tools": 2}
    # 检索请求（契约 4）：冻结的范围与上限
    (search,) = gateway["search"]
    assert search["body"] == {
        "query": "查询并关闭商机",
        "agent_id": "disc-agent",
        "agent_version": "1",
        "agent_tenant": "default",
        "limit": 2,
        "taint": [],
        "scope": ["crm"],
    }
    # 元工具在闸口前拦截：没有 tool.decision；写 capabilities.discovered（上限 2，第三个丢弃，保留名 / 不合规项不收）
    assert not any(e["type"] == "tool.decision" and e["payload"]["call_id"] == "c1" for e in log)
    (found,) = [e["payload"] for e in log if e["type"] == "capabilities.discovered"]
    assert found["call_id"] == "c1" and [t["name"] for t in found["tools"]] == ["crm.opportunity.search", "crm.opportunity.close"]
    assert found["dropped"] == ["mail.send"] and found["scope"] == ["crm"]
    assert found["tools_hmac"] == discovery.tools_hmac(found["tools"], found["resources"]) and "tools_hash" not in found
    assert found["resources"]["crm.opportunity.close"] == {"system": "crm", "tenant": "default"}  # 检索结果带 tenant（评审 #5）
    result = next(e["payload"] for e in log if e["type"] == "tool.result" and e["payload"]["call_id"] == "c1")
    assert result["status"] == "ok" and [t["name"] for t in result["output"]["tools"]] == [
        "crm.opportunity.search",
        "crm.opportunity.close",
    ]
    assert "limit_reached" in result["output"]
    # 下一轮起 harness 看到发现的工具；发现的工具照常过闸口（资源属性取自检索结果）
    assert harness.tools_seen[1][-2:] == ["crm.opportunity.search", "crm.opportunity.close"]
    decisions = {e["payload"]["call_id"]: e["payload"] for e in log if e["type"] == "tool.decision"}
    assert decisions["c2"]["decision"] == "allow" and decisions["c3"]["decision"] == "require_approval"
    close_req = next(r for r in policy_stub.REQUESTS if r["resource"]["id"] == "cap.crm.opportunity.close")
    assert close_req["resource"]["system"] == "crm" and close_req["resource"]["risk_level"] == "write_high"
    card = next(e for e in evs if e["type"] == "card")
    assert card["payload"]["preview"] == {"tool": "crm.opportunity.close", "dry_run": True}

    # 审批后续跑（重放）：从事件恢复已发现工具，不重新检索；发现的高风险写执行一次
    approval = {"call_id": "c3", "params_hash": card["payload"]["params_hash"]}
    assert client.post(f"/v1/runs/{run_id}/approve", json=approval).status_code == 200
    with client.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": msgs}) as resp:
        assert resp.status_code == 200
        _events(resp)
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "completed"
    assert len(gateway["search"]) == 1  # 没有重新检索
    assert [b["tool"] for b in gateway["invoke"] if not b["dry_run"]] == ["crm.opportunity.search", "crm.opportunity.close"]
    # 网关第二道防线（评审 #3）：/invoke 带闸口的审批结论、污点与 Agent 租户；只有审批后的执行 approved=true
    close = [b for b in gateway["invoke"] if b["tool"] == "crm.opportunity.close"]
    assert [(b["dry_run"], b["approved"]) for b in close] == [(True, False), (False, True)]
    assert all(b["taint"] == [] and b["agent_tenant"] == "default" for b in gateway["invoke"])
    assert harness.tools_seen[-1][-2:] == ["crm.opportunity.search", "crm.opportunity.close"]  # 续跑会话的工具列表含恢复的工具


def test_discovery_respects_cap_across_searches(client: Any, gateway: Any) -> None:
    """上限按整个 Run 计：第二次检索不再加入新工具，已可用的只告知。"""
    turns = [
        _call("search_capabilities", {"query": "商机"}, "c1"),
        _call("search_capabilities", {"query": "邮件"}, "c2"),
        _final(),
    ]
    _publish(client, "disc-cap", ToolsHarness("disc-cap", turns), DISCOVERY)
    body = {"agent_id": "disc-cap", "messages": [{"role": "user", "content": "x"}], "stream": False}
    run_id = client.post("/v1/messages", json=body).json()["run_id"]
    log = _log(client, run_id)
    assert len([e for e in log if e["type"] == "capabilities.discovered"]) == 1  # 第二次没有新工具，不写事件
    second = next(e["payload"]["output"] for e in log if e["type"] == "tool.result" and e["payload"]["call_id"] == "c2")
    assert "mail.send" in second["limit_reached"] and len(second["tools"]) == 2


def test_discovery_gateway_failure_is_tool_error(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    toolbroker.set_gateway_transport_for_tests(httpx.MockTransport(_raise))
    try:
        turns = [_call("search_capabilities", {"query": "商机"}, "c1"), _call("search_capabilities", {}, "c2"), _final()]
        _publish(client, "disc-down", ToolsHarness("disc-down", turns), DISCOVERY)
        body = {"agent_id": "disc-down", "messages": [{"role": "user", "content": "x"}], "stream": False}
        run_id = client.post("/v1/messages", json=body).json()["run_id"]
    finally:
        toolbroker.set_gateway_transport_for_tests(None)
    log = _log(client, run_id)
    results = {e["payload"]["call_id"]: e["payload"] for e in log if e["type"] == "tool.result"}
    assert results["c1"]["status"] == "error" and "unavailable" in results["c1"]["output"]["error"]
    assert results["c2"]["status"] == "error" and "query" in results["c2"]["output"]["error"]
    assert not any(e["type"] == "capabilities.discovered" for e in log)
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "completed"


def test_discovery_disabled_keeps_assembly_and_does_not_search(client: Any, gateway: Any) -> None:
    """未开启发现：不注入元工具（组装报告无 discovery 键），模型请求该名记 unknown tool，不检索。"""
    turns = [_call("search_capabilities", {"query": "商机"}, "c1"), _final()]
    _publish(client, "disc-off", ToolsHarness("disc-off", turns), {"tools": ["ticket.search"]})
    body = {"agent_id": "disc-off", "messages": [{"role": "user", "content": "x"}], "stream": False}
    run_id = client.post("/v1/messages", json=body).json()["run_id"]
    log = _log(client, run_id)
    assembled = next(e for e in log if e["type"] == "context.assembled")["payload"]
    assert assembled["tools"] == ["ticket.search", "read_artifact"] and "discovery" not in assembled
    assert next(e["payload"] for e in log if e["type"] == "tool.result")["output"] == {"error": "unknown tool"}
    assert gateway["search"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"enabled": "yes"},
        {"enabled": True, "max_tools": 0},
        {"enabled": True, "max_tools": 21},
        {"enabled": True, "scope": "crm"},
        {"enabled": True, "extra": 1},
        ["enabled"],
    ],
)
def test_discovery_config_validated_at_publish(client: Any, bad: Any) -> None:
    name = f"disc-bad-{abs(hash(json.dumps(bad))) % 10**8}"
    assert client.post("/admin/agents", json={"agent_id": name, "version": "1", "config": {"discovery": bad}}).status_code == 201
    r = client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True})
    assert r.status_code == 409 and "discovery" in r.json()["detail"] and r.json()["code"] == "CONFLICT"


def _rc(agent_config: dict[str, Any]) -> Any:
    agent = type("A", (), {"config": agent_config, "agent_id": "a", "version": "1"})()
    return toolbroker.RunCtx(
        run_id="r",
        agent=agent,  # type: ignore[arg-type]
        spec=None,  # type: ignore[arg-type]
        tools_by_name={},
        gate=gate.GateContext(subject="human:a", agent_id="a"),
        history=[],
    )


def test_discovery_restore_verifies_hash() -> None:
    """重放恢复：只恢复指纹一致的事件（tools_hmac，带密钥）；被篡改（如把只读改成写、改资源租户）、或无密钥者按裸 sha256
    重算指纹伪造的整条不恢复；评审修复前的旧事件（只有 tools_hash）按旧算法校验以兼容。"""
    tools = [ToolSpec(name="crm.x", description="d", capability_id="cap.crm.x").model_dump(mode="json")]
    resources = {"crm.x": {"system": "crm"}}
    good = {"call_id": "c1", "tools": tools, "resources": resources, "tools_hmac": discovery.tools_hmac(tools, resources)}
    tampered_tools = [dict(tools[0], name="crm.y", risk_level="write_high")]
    bad = dict(good, call_id="c2", tools=tampered_tools)
    forged = {"call_id": "c3", "tools": tampered_tools, "resources": resources}
    forged["tools_hmac"] = discovery.tools_hash(tampered_tools, resources)  # 无密钥者只能算裸 sha256
    legacy_tools = [ToolSpec(name="crm.old", description="d", capability_id="cap.crm.old").model_dump(mode="json")]
    legacy_res = {"crm.old": {"system": "crm"}}
    legacy = {"call_id": "c4", "tools": legacy_tools, "resources": legacy_res, "tools_hash": discovery.tools_hash(legacy_tools, legacy_res)}
    prior = [
        RunEvent(run_id="r", seq=i, type=RunEventType.CAPABILITIES_DISCOVERED, payload=p)
        for i, p in enumerate((good, bad, forged, legacy), start=1)
    ]
    rc = _rc({"discovery": {"enabled": True}})
    discovery.restore(rc, prior)
    assert list(rc.tools_by_name) == ["crm.x", "crm.old"] and [t.name for t in rc.discovered] == ["crm.x", "crm.old"]
    assert rc.gate.resources == {"crm.x": {"system": "crm"}, "crm.old": {"system": "crm"}}


def test_discovery_config_helpers() -> None:
    assert not discovery.enabled(_rc({}).agent) and not discovery.enabled(_rc({"discovery": {"enabled": False}}).agent)
    agent = _rc({"discovery": {"enabled": True, "scope": ["crm", "kb"]}}).agent
    assert discovery.enabled(agent) and discovery.max_tools(agent) == discovery.DEFAULT_MAX_TOOLS == 5
    assert discovery.scope(agent) == ["crm", "kb"]
    assert discovery.validate_config(None) is None and discovery.validate_config({"enabled": False}) is None


# ---------------- 统一错误体 ErrorDetail{detail, code} ----------------


def test_error_body_has_detail_and_code(client: Any) -> None:
    r = client.get("/v1/runs/run_nope")
    assert r.status_code == 404 and set(r.json()) == {"detail", "code"} and r.json()["code"] == "NOT_FOUND"
    body = {"agent_id": "demo", "messages": [{"role": "user", "content": [{"type": "image", "url": "x"}]}], "stream": False}
    r = client.post("/v1/messages", json=body)
    assert r.status_code == 400 and r.json()["code"] == "INLINE_CONTENT_UNSUPPORTED"
    run_id = client.post(
        "/v1/messages", json={"agent_id": "demo", "messages": [{"role": "user", "content": "hi"}], "stream": False}
    ).json()["run_id"]
    r = client.post(f"/v1/runs/{run_id}/approve", json={"call_id": "c", "params_hash": "x"})
    assert r.status_code == 409 and r.json() == {"detail": "run is not awaiting approval", "code": "CONFLICT"}
    r = client.post(f"/v1/runs/{run_id}/input", json={"kind": "bogus"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)  # 422 只留参数校验


def test_openapi_declares_error_detail(client: Any) -> None:
    spec = client.get("/openapi.json").json()
    schema = spec["components"]["schemas"]["ErrorDetail"]
    assert set(schema["required"]) == {"detail", "code"}
    responses = spec["paths"]["/v1/runs/{run_id}/input"]["post"]["responses"]
    assert responses["400"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ErrorDetail"}
    assert responses["422"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/HTTPValidationError"}
