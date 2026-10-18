"""鉴权矩阵（S8.4）：OBO 验签 + act 断言（ES256、网关 JWKS、iss / aud / sub / obo_hash / 寿命 / jti 防重放），
生产态缺断言 401，裸 X-AI-Act 只在开发态采信。"""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from mcp_template.act import obo_hash
from mcp_template.testing import ALICE, BOB, FakeGateway, FakeIdP, obo_headers

AUD = "mcp-template"


def _search(client: TestClient, headers: dict[str, str]) -> int:
    return client.post("/tools/ticket.search", json={"q": "x"}, headers=headers).status_code


def _close_by(client: TestClient, headers: dict[str, str]) -> str:
    r = client.post("/tools/ticket.close_batch", json={"ids": ["T-9"], "idempotency_key": "key-act-0001"}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["by"]


def _h(idp: FakeIdP, gw: FakeGateway, sub: str = ALICE) -> dict[str, str]:
    token = idp.token(sub, aud=AUD)
    return {"Authorization": f"Bearer {token}", "X-AI-Act-Assertion": gw.assertion(token, aud=AUD)}


def test_valid_obo_and_assertion_adopts_act_chain(client: TestClient, idp: FakeIdP, gw: FakeGateway) -> None:
    headers = obo_headers(idp, gw, aud=AUD, agent="agent:ticket-summary@3")
    assert _close_by(client, headers) == "agent:ticket-summary@3"


def test_missing_assertion_is_401_in_production(client: TestClient, idp: FakeIdP) -> None:
    token = idp.token(ALICE, aud=AUD)
    r = client.post("/tools/ticket.search", json={}, headers={"Authorization": f"Bearer {token}", "X-AI-Act": "agent:x@1"})
    assert r.status_code == 401 and "assertion" in r.json()["detail"]
    assert r.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize(
    "kw",
    [
        {"aud": "mcp-other"},
        {"iss": "evil-gateway"},
        {"exp": int(time.time()) - 120, "iat": int(time.time()) - 150},
        {"ttl": 120},
        {"iat": int(time.time()) - 10, "exp": int(time.time()) + 300},
        {"drop": ("jti",)},
        {"drop": ("exp",)},
        {"drop": ("act",)},
        {"act": {"sub": "client:ai-runtime-svc"}},
    ],
    ids=["wrong-aud", "wrong-iss", "expired", "ttl-over-60", "exp-far-future", "no-jti", "no-exp", "no-act", "act-not-agent"],
)
def test_invalid_assertion_is_401(client: TestClient, idp: FakeIdP, gw: FakeGateway, kw: dict) -> None:
    token = idp.token(ALICE, aud=AUD)
    kw = dict(kw)
    aud = kw.pop("aud", AUD)
    headers = {"Authorization": f"Bearer {token}", "X-AI-Act-Assertion": gw.assertion(token, aud=aud, **kw)}
    assert _search(client, headers) == 401


def test_wrong_algorithm_or_key_is_401(client: TestClient, idp: FakeIdP, gw: FakeGateway) -> None:
    token = idp.token(ALICE, aud=AUD)
    auth = {"Authorization": f"Bearer {token}"}
    rs256 = gw.assertion(token, aud=AUD, algorithm="RS256", key=idp.key)  # 非 ES256
    assert _search(client, auth | {"X-AI-Act-Assertion": rs256}) == 401
    forged = gw.assertion(token, aud=AUD, key=ec.generate_private_key(ec.SECP256R1()))  # 同 kid、另一把私钥
    assert _search(client, auth | {"X-AI-Act-Assertion": forged}) == 401
    unknown_kid = FakeGateway(kid="gw-unknown").assertion(token, aud=AUD)
    assert _search(client, auth | {"X-AI-Act-Assertion": unknown_kid}) == 401
    assert _search(client, auth | {"X-AI-Act-Assertion": "garbage"}) == 401


def test_subject_mismatch_is_403(client: TestClient, idp: FakeIdP, gw: FakeGateway) -> None:
    assert _search(client, _h(idp, gw)) == 200
    token = idp.token(ALICE, aud=AUD)
    headers = {"Authorization": f"Bearer {token}", "X-AI-Act-Assertion": gw.assertion(token, aud=AUD, sub=BOB)}
    r = client.post("/tools/ticket.search", json={}, headers=headers)
    assert r.status_code == 403 and "subject" in r.json()["detail"]


def test_assertion_bound_to_another_token_is_403(client: TestClient, idp: FakeIdP, gw: FakeGateway) -> None:
    token, other = idp.token(ALICE, aud=AUD), idp.token(ALICE, aud=AUD, jti="other")
    stolen = gw.assertion(other, aud=AUD)  # 为另一枚 OBO 签发的断言
    r = client.post("/tools/ticket.search", json={}, headers={"Authorization": f"Bearer {token}", "X-AI-Act-Assertion": stolen})
    assert r.status_code == 403 and "bound" in r.json()["detail"]


def test_obo_hash_accepts_base64url(client: TestClient, idp: FakeIdP, gw: FakeGateway) -> None:
    import base64
    import hashlib

    token = idp.token(ALICE, aud=AUD)
    b64 = base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest()).rstrip(b"=").decode()
    assert b64 != obo_hash(token)
    headers = {"Authorization": f"Bearer {token}", "X-AI-Act-Assertion": gw.assertion(token, aud=AUD, obo_hash=b64)}
    assert _search(client, headers) == 200


def test_replayed_assertion_is_401(client: TestClient, idp: FakeIdP, gw: FakeGateway) -> None:
    headers = _h(idp, gw)
    assert _search(client, headers) == 200
    r = client.post("/tools/ticket.search", json={}, headers=headers)
    assert r.status_code == 401 and "replayed" in r.json()["detail"]


def test_gateway_jwks_down_is_503_and_unconfigured_is_500(
    client: TestClient, idp: FakeIdP, gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw.down = True
    assert _search(client, _h(idp, gw)) == 503
    monkeypatch.delenv("MCP_TEMPLATE_ACT_JWKS_URL")
    assert _search(client, _h(idp, gw)) == 500


def test_gateway_jwks_is_cached(client: TestClient, idp: FakeIdP, gw: FakeGateway) -> None:
    for _ in range(3):
        assert _search(client, _h(idp, gw)) == 200
    assert gw.fetches == 1


def test_act_issuer_and_audience_configurable(client: TestClient, idp: FakeIdP, gw: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TEMPLATE_ACT_ISSUER", "gw-prod")
    monkeypatch.setenv("MCP_TEMPLATE_ACT_AUDIENCE", "connector-x")
    token = idp.token(ALICE, aud=AUD)
    ok = gw.assertion(token, aud="connector-x", iss="gw-prod")
    assert _search(client, {"Authorization": f"Bearer {token}", "X-AI-Act-Assertion": ok}) == 200
    assert _search(client, _h(idp, gw)) == 401  # 默认 iss / aud 不再接受


def test_invalid_obo_rejected_before_assertion(client: TestClient, idp: FakeIdP, gw: FakeGateway) -> None:
    token = idp.token(ALICE, aud="other-service")
    headers = {"Authorization": f"Bearer {token}", "X-AI-Act-Assertion": gw.assertion(token, aud=AUD)}
    assert _search(client, headers) == 401
    no_tenant = idp.token(ALICE, aud=AUD, tenant=None)
    assert _search(client, {"Authorization": f"Bearer {no_tenant}", "X-AI-Act-Assertion": gw.assertion(no_tenant, aud=AUD)}) == 403


def test_idp_jwks_down_is_503(client: TestClient, idp: FakeIdP, gw: FakeGateway) -> None:
    idp.down = True
    assert _search(client, _h(idp, gw)) == 503


# ---------------- 开发态 ----------------


def test_bare_act_only_in_dev_mode(client: TestClient, idp: FakeIdP, monkeypatch: pytest.MonkeyPatch) -> None:
    bare = {"X-AI-Act": "agent:bare@1"}
    trusted = {"Authorization": f"Bearer {idp.token(ALICE, aud=AUD)}"} | bare
    assert _search(client, trusted) == 401  # 生产态：裸头不算数，缺断言 401
    monkeypatch.setenv("MCP_TEMPLATE_DEV_AUTH", "true")
    assert _close_by(client, {"Authorization": f"Bearer {idp.token(ALICE, aud=AUD)}"} | bare) == "agent:bare@1"
    untrusted = {"Authorization": f"Bearer {idp.token(BOB, aud=AUD, azp='some-app')}"} | bare
    assert _close_by(client, untrusted) == BOB  # azp 不可信：不采信裸头


def test_dev_headers_only_in_dev_mode(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    dev = {"X-AI-Dev-Sub": ALICE, "X-AI-Act": "agent:dev@1"}
    assert _search(client, dev) == 401
    monkeypatch.setenv("MCP_TEMPLATE_DEV_AUTH", "true")
    assert _close_by(client, dev) == "agent:dev@1"


def test_dev_mode_still_verifies_presented_assertion(
    client: TestClient, idp: FakeIdP, gw: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_TEMPLATE_DEV_AUTH", "true")
    token = idp.token(ALICE, aud=AUD)
    bad = gw.assertion(token, aud="mcp-other")
    assert _search(client, {"Authorization": f"Bearer {token}", "X-AI-Act-Assertion": bad, "X-AI-Act": "agent:x@1"}) == 401


def test_replay_store_shared_via_redis(client: TestClient, idp: FakeIdP, gw: FakeGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    """评审 #8：配置 <P>_ACT_REPLAY_REDIS_URL 后 jti 记在共享 Redis——另一副本（进程内缓存为空）收到同一断言同样拒绝。"""
    import fakeredis

    from mcp_template import act

    server = fakeredis.FakeServer()
    act.set_redis_factory_for_tests(lambda url: fakeredis.FakeAsyncRedis(server=server))
    monkeypatch.setenv("MCP_TEMPLATE_ACT_REPLAY_REDIS_URL", "redis://replay.test/0")
    try:
        headers = _h(idp, gw)
        assert _search(client, headers) == 200
        act.replay_cache.clear()  # 换一个副本：进程内缓存没有该 jti
        r = client.post("/tools/ticket.search", json={}, headers=headers)
        assert r.status_code == 401 and "replayed" in r.json()["detail"]
        keys = fakeredis.FakeRedis(server=server).keys(f"{act.REPLAY_KEY_PREFIX}*")
        assert len(keys) == 1 and 0 < fakeredis.FakeRedis(server=server).pttl(keys[0]) <= (act.MAX_TTL_S + act.LEEWAY_S) * 1000

        class Down:
            async def set(self, *a: object, **k: object) -> bool:
                raise ConnectionError("redis down")

        act.set_redis_factory_for_tests(lambda url: Down())
        assert _search(client, _h(idp, gw)) == 503  # 共享存储不可用：失败关闭
    finally:
        act.set_redis_factory_for_tests(None)


async def test_jwks_stale_keys_are_capped(idp: FakeIdP, gw: FakeGateway) -> None:
    """评审 #8：JWKS 刷新失败时沿用旧公钥有上限（JWKS_MAX_STALE_S，默认 1 小时），超过即 JWKSUnavailable。"""
    from mcp_template import jwks
    from mcp_template.testing import GATEWAY_JWKS_URL, jwks_transport

    cache = jwks.JWKSCache(GATEWAY_JWKS_URL, transport=jwks_transport(idp, gw))
    kid = next(iter(gw.jwks()["keys"]))["kid"]
    assert await cache.get(kid) is not None
    gw.down = True
    cache._attempted_at = float("-inf")
    cache._fetched_at = time.monotonic() - (jwks.JWKS_TTL_S + 60)  # 过期但未超上限：沿用旧公钥
    assert await cache.get(kid) is not None
    cache._attempted_at = float("-inf")
    cache._fetched_at = time.monotonic() - (jwks.JWKS_MAX_STALE_S + 1)
    with pytest.raises(jwks.JWKSUnavailable):
        await cache.get(kid)
