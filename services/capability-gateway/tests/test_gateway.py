"""能力网关测试：两级发现（binding ∩ names、检索契约）、Skill 供应链规则、Token Exchange 开发桩、统一错误体。"""

from fastapi.testclient import TestClient
from gateway_testkit import Upstream

from capability_gateway import state
from capability_gateway.catalog import MemoryCatalog
from capability_gateway.main import app
from capability_gateway.registry import SEED, Binding

c = TestClient(app)


def test_core_discovery_returns_agent_core_bindings():
    """不带 names：demo 的 core 绑定（含 "*" 通配）；高风险工具带幂等键要求。"""
    state.catalog = MemoryCatalog(
        SEED, [Binding(agent_id="demo", capability_id=i) for i in ("cap.ticket.search", "cap.ticket.close_batch")]
    )
    r = c.get("/capabilities", params={"subject": "human:1", "agent": "demo"}).json()
    assert {t["name"] for t in r["tools"]} == {"ticket.search", "ticket.close_batch"}
    assert next(t for t in r["tools"] if t["name"] == "ticket.close_batch")["requires_idempotency_key"] is True


def test_names_whitelist_is_intersected_with_core_bindings():
    """D62 / S8.2：binding(core) ∩ names，按 names 顺序；未绑定、未登记的名字不返回。"""
    state.catalog = MemoryCatalog(
        SEED, [Binding(agent_id="a1", capability_id="cap.ticket.search"), Binding(agent_id="a1", capability_id="cap.ticket.get")]
    )
    r = c.get("/capabilities", params={"subject": "human:1", "agent": "a1", "names": "ticket.get,ticket.close_batch,nope,ticket.search"})
    assert [t["name"] for t in r.json()["tools"]] == ["ticket.get", "ticket.search"]
    r = c.get("/capabilities", params={"subject": "human:1", "agent": "other", "names": "ticket.get"})
    assert r.json()["tools"] == []


def test_search_contract_shape_and_policy_hint(up: Upstream):
    """契约 4：items 字段齐全；write_high 的 require_approval 作为 decision 返回。"""
    up.decisions["cap.ticket.close_batch"] = "require_approval"
    r = c.post("/capabilities/search", json={"query": "查询 销售 商机", "agent_id": "demo", "agent_version": "1"}).json()
    assert r["degraded"] is False and r["items"][0]["name"] == "crm.opportunity.search"
    assert set(r["items"][0]) >= {"id", "name", "description", "input_schema", "risk_level", "data_class", "system", "decision", "score"}
    r2 = c.post("/capabilities/search", json={"query": "关闭 工单", "agent_id": "demo"}).json()
    hit = next(t for t in r2["items"] if t["name"] == "ticket.close_batch")
    assert hit["decision"] == "require_approval"


def test_skill_store_enforces_supply_chain_rules():
    """缺评测集、缺签名或扫描未通过均按业务规则 409 拒绝（{detail, code}）；齐全时 201。"""
    base = {"name": "s", "version": "1.0.0", "owner": "o", "package_uri": "s3://x", "signature": "sig", "scan_passed": True}
    rejects = [{"eval_dataset_ref": None}, {"signature": "", "eval_dataset_ref": "ds1"}, {"scan_passed": False, "eval_dataset_ref": "ds1"}]
    for bad in rejects:
        r = c.post("/skills", json=base | bad)
        assert r.status_code == 409 and isinstance(r.json()["detail"], str) and r.json()["code"]
    assert c.post("/skills", json=base | {"eval_dataset_ref": "ds1"}).status_code == 201


def test_unknown_route_uses_error_detail():
    r = c.get("/capabilities/no.such")
    assert r.status_code == 404 and r.json() == {"detail": "unknown capability", "code": "unknown_capability"}


def test_token_exchange_dev_stub_carries_act():
    """未配置 Keycloak 时的开发桩令牌携带 act 声明与 5 分钟有效期。"""
    body = {"subject_token": "u", "audience": "mcp-ticket", "agent_id": "demo", "agent_version": "1.0.0"}
    r = c.post("/token/exchange", json=body).json()
    assert r["act"] == "agent:demo@1.0.0" and r["expires_in"] == 300


def test_token_exchange_ignores_client_supplied_actor():
    """请求体里混入 actor 字段不影响返回的 act：执行者身份由服务端组装，不接受客户端自称。"""
    body = {"subject_token": "u", "audience": "mcp-ticket", "agent_id": "demo", "agent_version": "1.0.0", "actor": "agent:attacker"}
    r = c.post("/token/exchange", json=body).json()
    assert r["act"] == "agent:demo@1.0.0" and "agent:attacker" not in r.values()


def test_token_exchange_act_format_is_agent_id_at_version():
    body = {"subject_token": "u", "audience": "mcp-crm", "agent_id": "sales-bot", "agent_version": "2.3.1"}
    r = c.post("/token/exchange", json=body).json()
    assert r["act"] == "agent:sales-bot@2.3.1" and r["aud"] == "mcp-crm"


def test_token_exchange_rejects_untrusted_gateway_client():
    """开发态：X-Gateway-Client 不在白名单 → 403。"""
    body = {"subject_token": "u", "audience": "mcp-ticket", "agent_id": "demo", "agent_version": "1.0.0"}
    r = c.post("/token/exchange", json=body, headers={"X-Gateway-Client": "rogue-client"})
    assert r.status_code == 403 and r.json()["code"] == "untrusted_caller"


def test_core_tools_carry_tenant_and_system():
    """Runtime 闸口组装资源属性需要 tenant / system。"""
    r = c.get("/capabilities", params={"subject": "human:1", "agent": "x", "names": "ticket.get"}).json()
    assert r["tools"][0]["tenant"] == "default" and r["tools"][0]["system"] == "ticket"


def test_dev_mode_reads_user_from_authorization_bearer(up: Upstream):
    """开发态：Runtime 以 Authorization: Bearer <用户令牌> 调用（无 X-AI-User-Token），主体与租户取自该令牌的 claims。"""
    import base64
    import json

    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    user = f"{enc({'alg': 'none'})}.{enc({'sub': 'bob', 'tenant': 'default'})}.sig"
    r = c.post("/capabilities/search", json={"query": "工单", "agent_id": "demo"}, headers={"Authorization": f"Bearer {user}"})
    assert r.status_code == 200 and up.policy[-1]["principal"]["sub"] == "bob"
