"""测试件：自签密钥的假 IdP（RSA，签 OBO 令牌）与假能力网关（ES256，签 act 断言），JWKS 经 httpx.MockTransport 提供。

只用于测试（连接器单测与 evals/e2e 的 mock 自测），不得在服务代码中导入。典型用法::

    idp, gw = FakeIdP(), FakeGateway()
    jwks.set_transport_for_tests(jwks_transport(idp, gw))
    monkeypatch.setenv(...auth_env("MCP_KNOWLEDGE", "mcp-knowledge")...)
    headers = obo_headers(idp, gw, aud="mcp-knowledge", sub=ALICE, roles=("sales",))
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from mcp_template.act import obo_hash

ISSUER = "http://idp.test/realms/ai-platform"
IDP_JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
GATEWAY_JWKS_URL = "http://gateway.test/.well-known/jwks.json"
GATEWAY_ISSUER = "capability-gateway"

# 与 services/identity/realm-export.json 中的开发用户一致
ALICE = "00000000-0000-4000-8000-0000000a11ce"  # sales，tenant default
BOB = "00000000-0000-4000-8000-000000000b0b"  # aftersales，tenant default
CAROL = "00000000-0000-4000-8000-0000000ca201"  # platform-admin，tenant default
DAVE = "00000000-0000-4000-8000-0000000da7e0"  # sales，tenant t2


class FakeIdP:
    """假 IdP：RSA 私钥签发 OBO 令牌（kid=k1），以 MockTransport 提供 JWKS。"""

    def __init__(self, kid: str = "k1") -> None:
        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.down = False
        self.fetches = 0

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key())) | {"kid": self.kid, "use": "sig", "alg": "RS256"}
        return {"keys": [jwk]}

    def token(
        self,
        sub: str = ALICE,
        *,
        aud: str | list[str] = "mcp-template",
        tenant: str | None = "default",
        roles: tuple[str, ...] = (),
        azp: str = "capability-gateway",
        drop: tuple[str, ...] = (),
        **claims: Any,
    ) -> str:
        now = int(time.time())
        body: dict[str, Any] = {
            "iss": ISSUER,
            "aud": aud,
            "sub": sub,
            "iat": now,
            "exp": now + 300,
            "azp": azp,
            "realm_access": {"roles": list(roles)},
        }
        if tenant is not None:
            body["tenant"] = tenant
        body |= claims
        for k in drop:
            body.pop(k, None)
        return jwt.encode(body, self.key, algorithm="RS256", headers={"kid": self.kid})


class FakeGateway:
    """假能力网关：ES256（P-256）私钥签发 act 断言（kid=gw-1），以 MockTransport 提供 JWKS。"""

    def __init__(self, kid: str = "gw-1") -> None:
        self.kid = kid
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.down = False
        self.fetches = 0

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(ECAlgorithm.to_jwk(self.key.public_key())) | {"kid": self.kid, "use": "sig", "alg": "ES256"}
        return {"keys": [jwk]}

    def assertion(
        self,
        obo_token: str,
        *,
        aud: str,
        sub: str | None = None,
        agent: str = "agent:demo@1",
        runtime_client: str = "client:ai-runtime-svc",
        ttl: int = 60,
        drop: tuple[str, ...] = (),
        algorithm: str = "ES256",
        key: Any = None,
        **claims: Any,
    ) -> str:
        """签发绑定 ``obo_token`` 的 act 断言；sub 缺省取 OBO 令牌的 sub（不验签解析）。"""
        if sub is None:
            sub = jwt.decode(obo_token, options={"verify_signature": False}).get("sub", "")
        now = int(time.time())
        body: dict[str, Any] = {
            "iss": GATEWAY_ISSUER,
            "aud": aud,
            "sub": sub,
            "iat": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
            "act": {"sub": agent, "act": {"sub": runtime_client}},
            "run_id": "run-test",
            "call_id": "call-test",
            "obo_hash": obo_hash(obo_token),
        }
        body |= claims
        for k in drop:
            body.pop(k, None)
        return jwt.encode(body, key if key is not None else self.key, algorithm=algorithm, headers={"kid": self.kid})


def jwks_transport(idp: FakeIdP, gateway: FakeGateway) -> httpx.MockTransport:
    """按主机分流：``gateway.test`` → 网关 JWKS，其余 → IdP JWKS。``down=True`` 时返回 503。"""

    def handle(request: httpx.Request) -> httpx.Response:
        src: FakeIdP | FakeGateway = gateway if request.url.host == "gateway.test" else idp
        src.fetches += 1
        return httpx.Response(503) if src.down else httpx.Response(200, json=src.jwks())

    return httpx.MockTransport(handle)


def auth_env(prefix: str, audience: str) -> dict[str, str]:
    """测试用鉴权环境变量（生产态：不开开发头）。"""
    return {
        f"{prefix}_JWKS_URL": IDP_JWKS_URL,
        f"{prefix}_ISSUER": ISSUER,
        f"{prefix}_AUDIENCE": audience,
        f"{prefix}_ACT_JWKS_URL": GATEWAY_JWKS_URL,
    }


def obo_headers(
    idp: FakeIdP,
    gateway: FakeGateway,
    *,
    aud: str,
    sub: str = ALICE,
    tenant: str | None = "default",
    roles: tuple[str, ...] = (),
    agent: str = "agent:demo@1",
) -> dict[str, str]:
    """一次调用的生产态请求头：OBO 令牌 + 绑定它的 act 断言（每次调用生成新 jti，不可复用）。"""
    token = idp.token(sub, aud=aud, tenant=tenant, roles=roles)
    return {"Authorization": f"Bearer {token}", "X-AI-Act-Assertion": gateway.assertion(token, aud=aud, agent=agent)}
