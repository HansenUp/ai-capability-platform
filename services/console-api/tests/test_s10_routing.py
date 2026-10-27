import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from console_api import routing_auth


@pytest.fixture
def signer(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "routing", "alg": "RS256"}
    monkeypatch.setenv("CONSOLE_ROUTING_JWKS_URL", "https://idp.test/jwks")
    monkeypatch.setenv("CONSOLE_ROUTING_ISSUER", "https://idp.test")
    monkeypatch.setenv("CONSOLE_ROUTING_AUDIENCE", "console-routing")
    monkeypatch.setattr(routing_auth, "_cached", None)
    monkeypatch.setattr(routing_auth, "_transport", httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": [public]})))

    def token(*, _kid="routing", **overrides):
        claims = {
            "sub": "routing-service",
            "client_id": "ai-routing-svc",
            "tenant": "routing-a",
            "scope": "routing:read",
            "iss": "https://idp.test",
            "aud": "console-routing",
            "iat": int(time.time()),
            "exp": int(time.time()) + 120,
        }
        return {"Authorization": "Bearer " + jwt.encode(claims | overrides, key, algorithm="RS256", headers={"kid": _kid})}

    return token


def test_routing_service_scope_tenant_and_no_header_impersonation(c, signer):
    for tenant in ("routing-a", "routing-b"):
        headers = signer(tenant=tenant) | {"X-Console-Tenant": "forged", "X-Console-Role": "PLATFORM_ADMIN"}
        response = c.get("/internal/routing?tenant=forged", headers=headers)
        assert response.status_code == 200 and response.json()["tenant_id"] == tenant
    assert c.get("/internal/routing", headers={"X-Console-Role": "PLATFORM_ADMIN"}).status_code == 401
    assert c.get("/internal/routing", headers={"Authorization": "Bearer test-node-token"}).status_code == 401


@pytest.mark.parametrize(
    "claims,status",
    [
        ({"client_id": "ai-node-svc"}, 403),
        ({"scope": "routing:write"}, 403),
        ({"tenant": ""}, 403),
        ({"tenant": ["routing-a"]}, 403),
        ({"aud": "runtime"}, 401),
        ({"iss": "https://evil.test"}, 401),
        ({"exp": 1}, 401),
        ({"iat": int(time.time()) + 3600}, 401),
    ],
)
def test_routing_service_rejects_invalid_claims(c, signer, claims, status):
    assert c.get("/internal/routing", headers=signer(**claims)).status_code == status


def test_routing_auth_unconfigured_or_jwks_unavailable_fails_closed(c, signer, monkeypatch):
    monkeypatch.setattr(routing_auth, "_transport", httpx.MockTransport(lambda request: httpx.Response(503)))
    assert c.get("/internal/routing", headers=signer()).status_code == 503
    monkeypatch.delenv("CONSOLE_ROUTING_AUDIENCE")
    assert c.get("/internal/routing", headers=signer()).status_code == 503


def test_routing_service_unknown_kid_and_symmetric_token_rejected(c, signer):
    token = jwt.encode({"sub": "fake"}, "local-test-key-at-least-thirty-two-bytes", algorithm="HS256", headers={"kid": "routing"})
    assert c.get("/internal/routing", headers={"Authorization": "Bearer " + token}).status_code == 401
    assert c.get("/internal/routing", headers=signer()).status_code == 200
    assert c.get("/internal/routing", headers=signer(_kid="unknown-key")).status_code == 401
    assert (
        c.post("/nodes/a/commands", headers=signer() | {"X-Console-Role": "USER"}, json={"type": "reload_config", "args": {}}).status_code
        == 403
    )
