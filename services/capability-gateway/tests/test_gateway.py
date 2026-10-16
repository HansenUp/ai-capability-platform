"""能力网关测试：两级发现、策略过滤、Skill 供应链规则、Token Exchange 开发桩。"""

from fastapi.testclient import TestClient

from capability_gateway.main import app

c = TestClient(app)


def test_core_discovery_returns_agent_preloaded_tools():
    """第一级发现只返回 demo Agent 预载的两个工具，且高风险工具带幂等键要求。"""
    r = c.get("/capabilities", params={"subject": "human:1", "agent": "demo"}).json()
    names = {t["name"] for t in r["tools"]}
    assert names == {"ticket.search", "ticket.close_batch"}
    assert next(t for t in r["tools"] if t["name"] == "ticket.close_batch")["requires_idempotency_key"] is True


def test_second_level_search_filters_by_policy():
    """第二级发现能按意图召回，并给出 decision_hint（高风险写为 require_approval）。"""
    r = c.post("/capabilities/search", json={"subject": "human:1", "agent": "demo", "intent": "查询 销售 商机"}).json()
    assert any(t["name"] == "crm.opportunity.search" for t in r["candidates"])
    r2 = c.post("/capabilities/search", json={"subject": "human:1", "agent": "demo", "intent": "关闭 工单"}).json()
    hit = next(t for t in r2["candidates"] if t["name"] == "ticket.close_batch")
    assert hit["decision_hint"] == "require_approval"


def test_skill_store_enforces_supply_chain_rules():
    """缺评测集或缺签名均 422；齐全时 201。"""
    base = {"name": "s", "version": "1.0.0", "owner": "o", "package_uri": "s3://x", "signature": "sig", "scan_passed": True}
    assert c.post("/skills", json=base | {"eval_dataset_ref": None}).status_code == 422
    assert c.post("/skills", json=base | {"signature": "", "eval_dataset_ref": "ds1"}).status_code == 422
    assert c.post("/skills", json=base | {"eval_dataset_ref": "ds1"}).status_code == 201


def test_token_exchange_dev_stub_carries_act():
    """未配置 Keycloak 时的开发桩令牌携带 act 声明与 5 分钟有效期。"""
    body = {"subject_token": "u", "audience": "mcp-ticket", "agent_id": "demo", "agent_version": "1.0.0"}
    r = c.post("/token/exchange", json=body).json()
    assert r["act"] == "agent:demo@1.0.0" and r["expires_in"] == 300


def test_token_exchange_ignores_client_supplied_actor():
    """请求体里混入 actor 字段不影响返回的 act：执行者身份由服务端组装，不接受客户端自称。"""
    body = {
        "subject_token": "u",
        "audience": "mcp-ticket",
        "agent_id": "demo",
        "agent_version": "1.0.0",
        "actor": "agent:attacker",  # 伪造的执行者声明，必须被忽略
    }
    r = c.post("/token/exchange", json=body).json()
    assert r["act"] == "agent:demo@1.0.0"
    assert "agent:attacker" not in r.values()


def test_token_exchange_act_format_is_agent_id_at_version():
    """act 声明格式固定为 agent:<id>@<version>，且 aud 回显目标受众。"""
    body = {"subject_token": "u", "audience": "mcp-crm", "agent_id": "sales-bot", "agent_version": "2.3.1"}
    r = c.post("/token/exchange", json=body).json()
    assert r["act"] == "agent:sales-bot@2.3.1"
    assert r["aud"] == "mcp-crm"


def test_token_exchange_rejects_untrusted_gateway_client():
    """非受信任的网关客户端（X-Gateway-Client 不在白名单）不得签发 OBO 令牌，返回 403。"""
    body = {"subject_token": "u", "audience": "mcp-ticket", "agent_id": "demo", "agent_version": "1.0.0"}
    r = c.post("/token/exchange", json=body, headers={"X-Gateway-Client": "rogue-client"})
    assert r.status_code == 403
