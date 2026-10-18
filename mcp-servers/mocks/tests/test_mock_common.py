"""三个 mock 的公共行为：healthz、manifest 满足写规范、生产态鉴权（OBO 受众 + act 断言）、调用审计、只绑 127.0.0.1。"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from mcp_mocks import PORTS, crm, knowledge, mail
from mcp_mocks import __main__ as entry
from mcp_template.spec import validate_manifest
from mcp_template.testing import ALICE, FakeGateway, FakeIdP

CASES = [
    (knowledge.app, "mcp-knowledge", "knowledge.search", {"query": "GW-200"}),
    (crm.app, "mcp-crm", "crm.opportunity.search", {}),
    (mail.app, "mcp-mail", "mail.send", {"to": ["a@corp.example"], "subject": "s", "body": "b", "dry_run": True}),
]


@pytest.mark.parametrize(("app", "aud", "tool", "body"), CASES, ids=["knowledge", "crm", "mail"])
def test_health_manifest_and_auth(app: object, aud: str, tool: str, body: dict, auth: Callable[..., dict[str, str]]) -> None:
    client = TestClient(app)  # type: ignore[arg-type]
    assert client.get("/healthz").json() == {"status": "ok"}
    manifest = client.get("/tools").json()
    assert validate_manifest(manifest) == []
    assert tool in {t["name"] for t in manifest}
    assert client.post(f"/tools/{tool}", json=body).status_code == 401
    assert client.post(f"/tools/{tool}", json=body, headers=auth(aud, sub=ALICE, roles=("sales",))).status_code == 200


@pytest.mark.parametrize(("app", "aud", "tool", "body"), CASES, ids=["knowledge", "crm", "mail"])
def test_obo_for_another_connector_is_rejected(app: object, aud: str, tool: str, body: dict, idp: FakeIdP, gw: FakeGateway) -> None:
    """链路④的前提：每个连接器只收受众为自己的 OBO 与断言。"""
    client = TestClient(app)  # type: ignore[arg-type]
    token = idp.token(ALICE, aud="mcp-ticket")
    headers = {"Authorization": f"Bearer {token}", "X-AI-Act-Assertion": gw.assertion(token, aud="mcp-ticket")}
    assert client.post(f"/tools/{tool}", json=body, headers=headers).status_code == 401
    ok_token = idp.token(ALICE, aud=aud)
    wrong_assertion_aud = {"Authorization": f"Bearer {ok_token}", "X-AI-Act-Assertion": gw.assertion(ok_token, aud="mcp-ticket")}
    assert client.post(f"/tools/{tool}", json=body, headers=wrong_assertion_aud).status_code == 401
    bare = {"Authorization": f"Bearer {idp.token(ALICE, aud=aud)}", "X-AI-Act": "agent:x@1"}
    assert client.post(f"/tools/{tool}", json=body, headers=bare).status_code == 401  # 生产态缺断言


def test_calls_audit_records_verified_act_chain(kclient: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    r = kclient.post("/tools/knowledge.search", json={"query": "GW-200"}, headers=auth("mcp-knowledge", agent="agent:kb-helper@2"))
    assert r.status_code == 200
    calls = kclient.get("/_mock/calls").json()
    assert len(calls) == 1
    c = calls[0]
    assert c["tool"] == "knowledge.search" and c["sub"] == ALICE and c["tenant"] == "default"
    assert c["audience"] == "mcp-knowledge" and c["act_verified"] is True
    assert c["act_chain"] == ["agent:kb-helper@2", "client:ai-runtime-svc"] and c["act"] == "agent:kb-helper@2"
    assert c["run_id"] == "run-test" and c["call_id"] == "call-test"
    assert kclient.post("/_mock/reset").json() == {"reset": True}
    assert kclient.get("/_mock/calls").json() == []


def test_dev_mode_headers(cclient: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    dev = {"X-AI-Dev-Sub": ALICE, "X-AI-Act": "agent:dev@1"}
    assert cclient.post("/tools/crm.opportunity.search", json={}, headers=dev).status_code == 401
    monkeypatch.setenv("MCP_CRM_DEV_AUTH", "true")
    assert cclient.post("/tools/crm.opportunity.search", json={}, headers=dev).json()["total"] == 3
    assert cclient.get("/_mock/calls").json()[-1]["act_verified"] is False


def test_entrypoint_binds_loopback_only() -> None:
    assert entry.HOST == "127.0.0.1"
    assert PORTS == {"knowledge": 8092, "crm": 8093, "mail": 8094}
