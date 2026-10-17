"""身份与行级权限：JWT 验签（aud / iss / exp / alg）、租户缺失 403、X-AI-Act 仅对可信 azp 采信、开发头仅在开发模式生效、
跨用户 / 跨租户读取与"不存在"不可区分。"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient
from ticket_testkit import ALICE, AUDIENCE, BOB, DAVE, ISSUER, IdP, bearer

NOT_FOUND = {"detail": "ticket not found or no permission"}


def _ids(client: TestClient, headers: dict[str, str], **body: object) -> set[str]:
    r = client.post("/tools/ticket.search", json={"limit": 50, **body}, headers=headers)
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}


# ---------------- 行级权限 ----------------


def test_alice_sees_only_her_tickets(client: TestClient, idp: IdP) -> None:
    """sales 角色：只能看到本人作为 requester 或 owner 的工单。"""
    ids = _ids(client, bearer(idp.token(ALICE, roles=("sales",))))
    assert ids == {"T-1001", "T-1002", "T-1003", "T-1004", "T-1005", "T-1012"}


def test_bob_aftersales_sees_all_default_tenant(client: TestClient, idp: IdP) -> None:
    """aftersales 角色：看到本租户全部工单，但看不到其他租户。"""
    ids = _ids(client, bearer(idp.token(BOB, roles=("aftersales",))))
    assert len(ids) == 16
    assert all(i.startswith("T-1") for i in ids)


def test_dave_sees_only_tenant_t2_own(client: TestClient, idp: IdP) -> None:
    ids = _ids(client, bearer(idp.token(DAVE, tenant="t2", roles=("sales",))))
    assert ids == {"T-2001", "T-2002", "T-2003"}


def test_cross_user_get_is_indistinguishable_from_missing(client: TestClient, idp: IdP) -> None:
    """无权限与不存在返回完全相同的 404（状态码与响应体）。"""
    h = bearer(idp.token(ALICE, roles=("sales",)))
    denied = client.post("/tools/ticket.get", json={"id": "T-1006"}, headers=h)  # 存在，但与 alice 无关
    missing = client.post("/tools/ticket.get", json={"id": "T-9999"}, headers=h)
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json() == NOT_FOUND
    assert denied.headers.get("content-length") == missing.headers.get("content-length")


def test_cross_tenant_get_is_404_even_for_aftersales(client: TestClient, idp: IdP) -> None:
    h = bearer(idp.token(BOB, roles=("aftersales",)))
    assert client.post("/tools/ticket.get", json={"id": "T-2001"}, headers=h).json() == NOT_FOUND
    h2 = bearer(idp.token(DAVE, tenant="t2", roles=("sales",)))
    assert client.post("/tools/ticket.get", json={"id": "T-1001"}, headers=h2).json() == NOT_FOUND


def test_audience_may_be_a_list(client: TestClient, idp: IdP) -> None:
    h = bearer(idp.token(ALICE, aud=["account", AUDIENCE]))
    assert client.post("/tools/ticket.search", json={}, headers=h).status_code == 200


# ---------------- 令牌校验 ----------------


def test_missing_token_is_401_with_www_authenticate(client: TestClient, idp: IdP) -> None:
    r = client.post("/tools/ticket.search", json={})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "other-service"},
        {"iss": "http://evil.test/realms/x"},
        {"exp": int(time.time()) - 600},
        {"drop": ("exp",)},
        {"drop": ("aud",)},
    ],
    ids=["wrong-aud", "wrong-iss", "expired", "no-exp", "no-aud"],
)
def test_invalid_claims_are_401(client: TestClient, idp: IdP, claims: dict) -> None:
    r = client.post("/tools/ticket.search", json={}, headers=bearer(idp.token(**claims)))
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_alg_none_is_401(client: TestClient, idp: IdP) -> None:
    now = int(time.time())
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": ALICE, "iat": now, "exp": now + 300, "tenant": "default"}, None, algorithm="none"
    )
    r = client.post("/tools/ticket.search", json={}, headers=bearer(token))
    assert r.status_code == 401


def test_hs256_signed_with_public_key_is_401(client: TestClient, idp: IdP) -> None:
    """HS* 被拒绝：不能用公开的 JWKS 公钥当共享密钥伪造令牌。"""
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": ALICE, "iat": now, "exp": now + 300, "tenant": "default"}
    token = jwt.encode(claims, "secret-secret-secret-secret-secret", algorithm="HS256", headers={"kid": "k1"})
    assert client.post("/tools/ticket.search", json={}, headers=bearer(token)).status_code == 401


def test_bad_signature_and_garbage_are_401(client: TestClient, idp: IdP) -> None:
    other = IdP()  # 另一把私钥，kid 相同
    assert client.post("/tools/ticket.search", json={}, headers=bearer(other.token())).status_code == 401
    assert client.post("/tools/ticket.search", json={}, headers=bearer("not-a-jwt")).status_code == 401


def test_missing_tenant_claim_is_403(client: TestClient, idp: IdP) -> None:
    r = client.post("/tools/ticket.search", json={}, headers=bearer(idp.token(tenant=None)))
    assert r.status_code == 403


def test_jwks_unreachable_is_503(client: TestClient, idp: IdP) -> None:
    idp.down = True
    r = client.post("/tools/ticket.search", json={}, headers=bearer(idp.token()))
    assert r.status_code == 503


# ---------------- X-AI-Act 与开发头 ----------------


def _close_by(client: TestClient, headers: dict[str, str]) -> str:
    r = client.post("/tools/ticket.close_batch", json={"ids": ["T-1002"], "idempotency_key": "key-act-0001"}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["by"]


def test_x_ai_act_honored_only_for_trusted_azp(client: TestClient, idp: IdP) -> None:
    act = {"X-AI-Act": "agent:ticket-summary@1"}
    assert _close_by(client, bearer(idp.token(ALICE, azp="capability-gateway")) | act) == "agent:ticket-summary@1"


def test_x_ai_act_ignored_for_untrusted_azp(client: TestClient, idp: IdP) -> None:
    act = {"X-AI-Act": "agent:ticket-summary@1"}
    assert _close_by(client, bearer(idp.token(ALICE, azp="some-other-app")) | act) == ALICE


def test_trusted_azp_is_configurable(client: TestClient, idp: IdP, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TICKET_TRUSTED_AZP", "gw-a, gw-b")
    act = {"X-AI-Act": "agent:x@1"}
    assert _close_by(client, bearer(idp.token(ALICE, azp="gw-b")) | act) == "agent:x@1"


def test_dev_headers_ignored_unless_dev_mode(client: TestClient, idp: IdP, monkeypatch: pytest.MonkeyPatch) -> None:
    dev = {"X-AI-Dev-Sub": ALICE, "X-AI-Dev-Tenant": "default", "X-AI-Dev-Roles": "aftersales"}
    r = client.post("/tools/ticket.search", json={}, headers=dev)
    assert r.status_code == 401
    monkeypatch.setenv("MCP_TICKET_DEV_AUTH", "false")
    assert client.post("/tools/ticket.search", json={}, headers=dev).status_code == 401
    monkeypatch.setenv("MCP_TICKET_DEV_AUTH", "true")
    r = client.post("/tools/ticket.search", json={"limit": 50}, headers=dev)
    assert r.status_code == 200
    assert r.json()["total"] == 16  # roles 头生效：aftersales 看到全租户


def test_dev_mode_defaults_tenant_and_still_checks_tokens_without_dev_sub(
    client: TestClient, idp: IdP, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_TICKET_DEV_AUTH", "true")
    r = client.post("/tools/ticket.search", json={}, headers={"X-AI-Dev-Sub": ALICE})
    assert r.status_code == 200 and r.json()["total"] == 6  # 租户默认 default，无角色
    assert client.post("/tools/ticket.search", json={}).status_code == 401  # 开发模式下没有开发头也没有令牌
    assert client.post("/tools/ticket.search", json={}, headers=bearer(idp.token())).status_code == 200  # 令牌照常可用


def test_healthz_and_manifest_need_no_auth(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    manifest = {t["name"]: t for t in client.get("/tools").json()}
    assert set(manifest) == {"ticket.search", "ticket.get", "ticket.close_batch"}
    assert manifest["ticket.search"]["risk_level"] == manifest["ticket.get"]["risk_level"] == "read"
    close = manifest["ticket.close_batch"]
    assert close["risk_level"] == "write_high"
    assert close["supports_dry_run"] and close["requires_idempotency_key"] and close["compensate_tool"]
    assert all(t["description"] and t["input_schema"] for t in manifest.values())
