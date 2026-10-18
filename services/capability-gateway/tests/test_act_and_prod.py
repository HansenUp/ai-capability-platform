"""act 断言（S8.4，契约 2）签名与字段；GATEWAY_ENV=prod 的启动约束与 mcp_proxy 不挂载；生产分支 resolve_caller_client 验签。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from gateway_testkit import Upstream

from capability_gateway import act, auth, config, main

c = TestClient(main.app)


@pytest.fixture
def act_key(tmp_path: Path) -> Path:
    key = ec.generate_private_key(ec.SECP256R1())
    p = tmp_path / "act.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return p


def test_assertion_fields_and_jwks(act_key: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    act.reset_for_tests()
    monkeypatch.setattr(config, "ACT_TTL_S", 60)
    act.load_key(str(act_key))
    token = act.sign(
        sub="u1",
        audience="mcp-crm",
        agent_id="sales",
        agent_version="3",
        caller="ai-runtime-svc",
        run_id="r",
        call_id="c",
        obo_token="obo-x",  # noqa: S106 —— 测试用假令牌
        now=1_000,
    )
    header = jwt.get_unverified_header(token)
    jwks = c.get("/.well-known/jwks.json").json()
    (jwk,) = jwks["keys"]
    assert header["alg"] == "ES256" and header["kid"] == jwk["kid"] and jwk["crv"] == "P-256" and jwk["use"] == "sig"
    claims = jwt.decode(token, jwt.PyJWK(jwk).key, algorithms=["ES256"], audience="mcp-crm", options={"verify_exp": False})
    assert claims == {
        "iss": "capability-gateway",
        "aud": "mcp-crm",
        "sub": "u1",
        "iat": 1000,
        "exp": 1060,
        "jti": claims["jti"],
        "act": {"sub": "agent:sales@3", "act": {"sub": "client:ai-runtime-svc"}},
        "run_id": "r",
        "call_id": "c",
        "obo_hash": hashlib.sha256(b"obo-x").hexdigest(),
    }
    other = act.sign(
        sub="u1", audience="mcp-crm", agent_id="sales", agent_version="3", caller="x", run_id=None, call_id=None, obo_token=None
    )
    assert jwt.decode(other, options={"verify_signature": False})["jti"] != claims["jti"]  # 每次签发唯一 jti
    act.reset_for_tests()


def test_prod_requires_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    act.reset_for_tests()
    monkeypatch.setattr(config, "GATEWAY_ENV", "prod")
    monkeypatch.setattr(config, "ACT_KEY_FILE", None)
    with pytest.raises(RuntimeError, match="GATEWAY_ACT_SIGNING_KEY_FILE"):
        act.load_key()
    act.reset_for_tests()


def test_prod_config_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "GATEWAY_ENV", "prod")
    monkeypatch.setattr(config, "KEYCLOAK_TOKEN_URL", None)
    monkeypatch.setattr(config, "CATALOG_BACKEND", "memory")
    problems = config.prod_problems()
    assert "KEYCLOAK_TOKEN_URL" in problems and "CATALOG_BACKEND=mysql" in problems and "GATEWAY_ACT_SIGNING_KEY_FILE" in problems
    assert "POLICY_SERVICE_TOKEN" in problems
    with pytest.raises(RuntimeError, match="GATEWAY_ENV=prod requires"):
        config.check_prod()


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    code = (
        "import capability_gateway.main as m\n"
        "rs = [x for r in m.app.routes for x in (r.original_router.routes if hasattr(r, 'original_router') else [r])]\n"
        "print(sorted({r.path for r in rs}))"
    )
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("GATEWAY_", "KEYCLOAK_", "CATALOG_", "POLICY_"))}
    return subprocess.run([sys.executable, "-c", code], env=clean | env, capture_output=True, text=True, timeout=60)  # noqa: S603


def test_prod_startup_refuses_missing_config_and_hides_mcp_proxy(act_key: Path) -> None:
    r = _run({"GATEWAY_ENV": "prod"})
    assert r.returncode != 0 and "GATEWAY_ENV=prod requires" in r.stderr
    full = {
        "GATEWAY_ENV": "prod",
        "KEYCLOAK_TOKEN_URL": "http://idp.test/realms/ai/protocol/openid-connect/token",
        "GATEWAY_ADMIN_TOKEN": "a",
        "GATEWAY_AUTHZ_TOKEN": "b",
        "POLICY_SERVICE_TOKEN": "c",
        "GATEWAY_TRUSTED_CALLERS": "ai-runtime-svc",
        "GATEWAY_USER_AUDIENCE": "ai-runtime",
        "GATEWAY_ACT_SIGNING_KEY_FILE": str(act_key),
        "CATALOG_BACKEND": "mysql",
        "GATEWAY_DATABASE_URL": "mysql+asyncmy://u:p@127.0.0.1:1/ai_gateway",
    }
    r = _run(full)
    assert r.returncode == 0, r.stderr
    assert "/mcp/{server}" not in r.stdout and "/authz/mcp-check" not in r.stdout and "/invoke" in r.stdout
    assert "/mcp/{server}" in _run({}).stdout  # 开发态仍挂载


async def test_resolve_caller_client_production_branch_verifies_jwks(keycloak: Upstream) -> None:
    up = keycloak
    assert await auth.resolve_caller_client(f"Bearer {up.idp.service_token()}") == "ai-runtime-svc"
    forged = jwt.encode({"azp": "ai-runtime-svc", "aud": "capability-gateway", "iss": "x", "exp": 9e9, "iat": 0}, "k", algorithm="HS256")
    with pytest.raises(auth.GatewayError) as e:
        await auth.resolve_caller_client(f"Bearer {forged}")
    assert e.value.status_code == 401
    with pytest.raises(auth.GatewayError):
        await auth.resolve_caller_client(None, "ai-runtime-svc")  # 生产不接受 X-Gateway-Client 自称
    expired = up.idp.service_token(exp=1)
    with pytest.raises(auth.GatewayError, match="expired"):
        await auth.resolve_caller_client(f"Bearer {expired}")
