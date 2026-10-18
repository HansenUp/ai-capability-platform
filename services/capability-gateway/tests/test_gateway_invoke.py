"""/invoke：服务间鉴权（契约 3）、OBO + act 断言转发、§0 主体租户取自用户令牌、写规范（幂等键）、策略失败关闭、连接器 down。"""

from __future__ import annotations

import hashlib
import json

import jwt
import pytest
from fastapi.testclient import TestClient
from gateway_testkit import Upstream

from capability_gateway import main, state
from capability_gateway.catalog import ConnectorVersion
from capability_gateway.registry import Capability

c = TestClient(main.app)
BODY = {
    "tool": "ticket.get",
    "input": {"id": "T-1001"},
    "subject": "human:u1",
    "agent_id": "ticket-summary",
    "agent_version": "1",
    "run_id": "r1",
    "call_id": "c1",
}
pytestmark = pytest.mark.usefixtures("ticket_connector")


def hdrs(up: Upstream, *, user: str | None = None, service: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {service or up.idp.service_token()}", "X-AI-User-Token": user or up.idp.user_token()}


def test_invoke_exchanges_token_and_forwards_with_act_assertion(keycloak: Upstream) -> None:
    up = keycloak
    user = up.idp.user_token()
    r = c.post("/invoke", json=BODY, headers=hdrs(up, user=user))
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["output"]["id"] == "T-1001"
    (form,) = up.token_calls
    assert form["subject_token"] == user and form["audience"] == "mcp-ticket"
    (req,) = up.connector
    assert req.url.path == "/tools/ticket.get" and req.headers["authorization"] == "Bearer obo-1"
    assert req.headers["x-ai-act"] == "agent:ticket-summary@1" and json.loads(req.content) == {"id": "T-1001"}
    assert req.headers["x-ai-run-id"] == "r1" and req.headers["x-ai-call-id"] == "c1"
    # act 断言：用 /.well-known/jwks.json 验签，字段按契约 2
    assertion = req.headers["x-ai-act-assertion"]
    jwk = c.get("/.well-known/jwks.json").json()["keys"][0]
    claims = jwt.decode(assertion, jwt.PyJWK(jwk).key, algorithms=["ES256"], audience="mcp-ticket", issuer="capability-gateway")
    assert claims["sub"] == "user-1" and claims["run_id"] == "r1" and claims["call_id"] == "c1"
    assert claims["act"] == {"sub": "agent:ticket-summary@1", "act": {"sub": "client:ai-runtime-svc"}}
    assert claims["obo_hash"] == hashlib.sha256(b"obo-1").hexdigest() and claims["exp"] - claims["iat"] <= 60 and claims["jti"]
    c.post("/invoke", json=BODY, headers=hdrs(up, user=user))
    assert len(up.token_calls) == 1  # OBO 令牌按用户令牌缓存


def test_policy_principal_tenant_comes_from_user_token(keycloak: Upstream) -> None:
    """§0 / D128：principal 取自验签令牌（不是能力租户），请求体里的 subject 被忽略；resource / context 字段齐全。"""
    up = keycloak
    state.catalog._caps["cap.ticket.get"] = state.catalog._caps["cap.ticket.get"].model_copy(update={"tenant": "t2"})  # type: ignore[attr-defined]
    r = c.post(
        "/invoke",
        json=BODY | {"taint": ["confidential"], "approved": True},
        headers=hdrs(up, user=up.idp.user_token(sub="alice", tenant="t2")),
    )
    assert r.status_code == 200
    (req,) = up.policy
    assert req["principal"] == {"sub": "alice", "tenant": "t2", "roles": ["employee"]}
    assert req["resource"] == {"id": "cap.ticket.get", "tenant": "t2", "system": "ticket", "risk_level": "read", "data_class": "internal"}
    assert req["context"] == {
        "approved": True,
        "taint": ["confidential"],
        "agent": {"id": "ticket-summary", "version": "1", "tenant": "t2"},
    }
    # 用户租户里没有该工具：404，不会按别的租户的能力转发
    assert c.post("/invoke", json=BODY, headers=hdrs(up, user=up.idp.user_token(tenant="t3"))).status_code == 404


def test_service_to_service_auth(keycloak: Upstream) -> None:
    """S4 #1：缺服务令牌、用户令牌直连（azp 不受信任）、服务令牌受众不符 → 401；缺用户令牌 → 401；缺 tenant 声明 → 403。"""
    up = keycloak
    user = up.idp.user_token()
    assert c.post("/invoke", json=BODY, headers={"X-AI-User-Token": user}).json()["code"] == "service_token_required"
    r = c.post("/invoke", json=BODY, headers={"Authorization": f"Bearer {user}", "X-AI-User-Token": user})
    assert r.status_code == 401  # 用户令牌直连网关（aud / azp 都不是服务客户端）
    direct = up.idp.token(sub="u", azp="portal-web", aud="capability-gateway")
    assert c.post("/invoke", json=BODY, headers=hdrs(up, service=direct)).json()["code"] == "untrusted_caller"
    wrong_aud = up.idp.service_token(aud="other")
    assert c.post("/invoke", json=BODY, headers=hdrs(up, service=wrong_aud)).status_code == 401
    r = c.post("/invoke", json=BODY, headers={"Authorization": f"Bearer {up.idp.service_token()}"})
    assert r.status_code == 401 and r.json()["code"] == "user_token_required"
    r = c.post("/invoke", json=BODY, headers=hdrs(up, user=up.idp.user_token(tenant=None)))
    assert r.status_code == 403 and r.json()["code"] == "no_tenant"
    for path, body in (
        ("/capabilities/search", {"query": "工单", "agent_id": "a"}),
        ("/authz/check", {"agent": "a", "capability": "cap.ticket.get"}),
    ):
        assert c.post(path, json=body).status_code == 401
    assert c.get("/capabilities", params={"subject": "s", "agent": "a"}).status_code == 401
    assert up.connector == [] and up.token_calls == []


def test_authz_check_uses_token_identity(keycloak: Upstream) -> None:
    up = keycloak
    up.decisions["cap.ticket.close_batch"] = "require_approval"
    r = c.post("/authz/check", json={"agent": "demo", "capability": "cap.ticket.close_batch", "subject": "spoofed"}, headers=hdrs(up))
    assert r.json() == {"decision": "require_approval", "engine": "cedar", "policy_ids": ["p1"], "reason": None}
    assert up.policy[-1]["principal"]["sub"] == "user-1"


def test_policy_deny_and_unavailable_fail_closed(up: Upstream) -> None:
    up.decisions["cap.ticket.get"] = "deny"
    r = c.post("/invoke", json=BODY)
    assert r.status_code == 403 and r.json()["code"] == "policy_denied"
    up.policy_down = True
    r = c.post("/invoke", json=BODY)
    assert r.status_code == 403 and r.json()["code"] == "policy_unavailable"
    r = c.post("/authz/check", json={"agent": "a", "capability": "cap.ticket.get"})
    assert r.json()["decision"] == "deny" and r.json()["reason"] == "policy_unavailable"
    assert up.connector == []


def test_business_errors_are_returned_not_raised(keycloak: Upstream) -> None:
    keycloak.connector_status = 404
    r = c.post("/invoke", json=BODY, headers=hdrs(keycloak))
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["status_code"] == 404


def test_registration_requires_admin_token_when_keycloak_enabled(keycloak: Upstream, monkeypatch: pytest.MonkeyPatch) -> None:
    """启用 Keycloak 后登记连接器需要管理令牌，否则可把 OBO 令牌导向任意端点。"""
    from capability_gateway import config

    evil = {"name": "mcp-ticket", "endpoint": "http://evil.test", "system": "ticket", "version": "9"}
    assert c.post("/mcp/servers", json=evil).status_code == 403
    monkeypatch.setattr(config, "GATEWAY_ADMIN_TOKEN", "admin-secret")
    assert c.post("/mcp/servers", json=evil, headers={"X-Gateway-Admin-Token": "wrong"}).status_code == 403
    assert all(v["endpoint"] != "http://evil.test" for v in c.get("/mcp/servers", headers={"X-Gateway-Admin-Token": "admin-secret"}).json())


def test_dev_mode_forwards_subject_and_signs_assertion(up: Upstream) -> None:
    r = c.post("/invoke", json=BODY)
    assert r.status_code == 200
    h = up.connector[0].headers
    assert h["x-ai-dev-sub"] == "u1" and "authorization" not in h
    claims = jwt.decode(h["x-ai-act-assertion"], options={"verify_signature": False})
    assert claims["sub"] == "human:u1" and "obo_hash" not in claims and claims["act"]["act"]["sub"] == "client:capability-gateway"


def test_write_requires_idempotency_key_unless_dry_run(up: Upstream) -> None:
    body = BODY | {"tool": "ticket.close_batch", "input": {"ids": ["T-1"]}}
    r = c.post("/invoke", json=body)
    assert r.status_code == 409 and r.json()["code"] == "idempotency_key_required" and up.policy == []
    assert c.post("/invoke", json=body | {"dry_run": True}).status_code == 200
    assert json.loads(up.connector[-1].content) == {"ids": ["T-1"], "dry_run": True}  # 预演不传 null 幂等键
    assert c.post("/invoke", json=body | {"idempotency_key": "k1"}).status_code == 200
    assert json.loads(up.connector[-1].content) == {"ids": ["T-1"], "dry_run": False, "idempotency_key": "k1"}


async def test_connector_down_returns_503(up: Upstream) -> None:
    cv = await state.catalog.active_connector("mcp-ticket")
    assert cv is not None
    await state.catalog.save_probe(cv.model_copy(update={"health": "down", "consecutive_failures": 3}), None)
    r = c.post("/invoke", json=BODY)
    assert r.status_code == 503 and r.json()["code"] == "connector_down" and up.connector == []


async def test_capability_routed_by_connector_name(up: Upstream) -> None:
    await state.catalog.put_connector(ConnectorVersion(name="mcp-x", endpoint="http://ticket.test", system="other", status="active"))
    await state.catalog.upsert(Capability(id="cap.x.read", name="x.read", description="d", system="ticket", connector="mcp-x"))
    up.manifest.append({"name": "x.read", "risk_level": "read"})  # /invoke 要求工具在承载连接器的 manifest 中
    assert c.post("/invoke", json=BODY | {"tool": "x.read"}).status_code == 200
    assert up.connector[-1].url.path == "/tools/x.read"


def test_service_mode_discovery_and_detail_filtered_by_user_tenant(keycloak: Upstream) -> None:
    """评审 #2：服务态第一级发现与能力详情一律按用户令牌租户过滤；缺用户令牌 401，其他租户的能力详情 404。"""
    up = keycloak
    svc = {"Authorization": f"Bearer {up.idp.service_token()}"}
    params = {"subject": "s", "agent": "ticket-summary", "names": "ticket.get"}
    assert c.get("/capabilities", params={"subject": "s", "agent": "*"}, headers=svc).status_code == 401
    assert c.get("/capabilities/cap.ticket.get", headers=svc).status_code == 401
    same = c.get("/capabilities", params=params, headers=hdrs(up)).json()["tools"]
    assert [t["name"] for t in same] == ["ticket.get"]
    other = hdrs(up, user=up.idp.user_token(tenant="t9"))
    assert c.get("/capabilities", params=params, headers=other).json()["tools"] == []
    assert c.get("/capabilities", params={"subject": "s", "agent": "ticket-summary"}, headers=other).json()["tools"] == []
    assert c.get("/capabilities/cap.ticket.get", headers=hdrs(up)).json()["tenant"] == "default"
    r = c.get("/capabilities/cap.ticket.get", headers=other)
    assert r.status_code == 404 and r.json()["code"] == "unknown_capability"


def test_connector_listing_requires_admin_or_service(keycloak: Upstream, monkeypatch: pytest.MonkeyPatch) -> None:
    """评审 #6：GET /mcp/servers[/{name}] 需管理令牌或受信服务令牌。"""
    from capability_gateway import config

    monkeypatch.setattr(config, "GATEWAY_ADMIN_TOKEN", "admin-secret")
    up = keycloak
    for path in ("/mcp/servers", "/mcp/servers/mcp-ticket"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"X-Gateway-Admin-Token": "wrong"}).status_code == 401
        assert c.get(path, headers={"Authorization": f"Bearer {up.idp.user_token()}"}).status_code == 401  # 用户令牌（aud 不符）
        direct = up.idp.token(sub="u", azp="portal-web", aud="capability-gateway")
        assert c.get(path, headers={"Authorization": f"Bearer {direct}"}).status_code == 403
        assert c.get(path, headers={"X-Gateway-Admin-Token": "admin-secret"}).status_code == 200
        assert c.get(path, headers={"Authorization": f"Bearer {up.idp.service_token()}"}).status_code == 200


def test_gateway_requires_approval_for_unapproved_execution(up: Upstream) -> None:
    """评审 #3：策略 require_approval 时，未审批的非 dry-run 调用被网关拒绝（第二道防线）；预演与已审批放行。"""
    up.decisions["cap.ticket.close_batch"] = "require_approval"
    body = BODY | {"tool": "ticket.close_batch", "input": {"ids": ["T-1"]}, "idempotency_key": "k1"}
    r = c.post("/invoke", json=body)
    assert r.status_code == 403 and r.json()["code"] == "approval_required" and up.connector == []
    assert c.post("/invoke", json=body | {"dry_run": True}).status_code == 200
    assert c.post("/invoke", json=body | {"approved": True}).status_code == 200
    assert up.policy[-1]["context"]["approved"] is True


def test_gateway_enforces_taint_and_agent_tenant_with_real_rules(up: Upstream) -> None:
    """评审 #3：按策略规则（baseline 与 Cedar 同一套）——污点外发未审批、Agent 跨租户在网关被拒绝。"""
    up.baseline = True
    assert c.post("/invoke", json=BODY).status_code == 200
    r = c.post("/invoke", json=BODY | {"agent_tenant": "t9"})
    assert r.status_code == 403 and r.json()["code"] == "policy_denied"
    assert up.policy[-1]["context"]["agent"]["tenant"] == "t9"
    mail = BODY | {"tool": "mail.send", "input": {"to": ["a@b.example"]}, "idempotency_key": "k2", "taint": ["confidential"]}
    r = c.post("/invoke", json=mail)
    assert r.status_code == 403 and r.json()["code"] == "approval_required"
    assert up.policy[-1]["context"]["taint"] == ["confidential"] and up.connector[-1].url.path == "/tools/ticket.get"
