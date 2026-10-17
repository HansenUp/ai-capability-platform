"""S2.1 / S2.2：JWT 本地验签（JWKS 缓存、签发者 / 受众 / 过期校验）、用户令牌与应用 Client Credentials 令牌的主体解析、
未带令牌 401、租户隔离与管理员角色。

用 cryptography 现场生成 RSA 密钥对签发令牌，JWKS 经 httpx.MockTransport 提供；把 auth 模块里的 settings
替换为 AUTH_REQUIRED=true 的副本，其余模块（db 等）不受影响。
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select

from runtime.api import auth
from runtime.state.db import Audit, SessionLocal

ISSUER = "http://idp.test/realms/ai-platform"
AUDIENCE = "ai-runtime"
JWKS_URL = "http://idp.test/realms/ai-platform/protocol/openid-connect/certs"


class IdP:
    """测试用 IdP：持有若干签名密钥，按 kid 签发令牌，并以 MockTransport 提供 JWKS（记录拉取次数）。"""

    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.published: set[str] = set()  # 当前 JWKS 中公布的 kid
        self.fetches = 0
        self.down = False
        self.add_key("k1")

    def add_key(self, kid: str, publish: bool = True) -> None:
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if publish:
            self.published.add(kid)

    def _jwks(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        if self.down:
            return httpx.Response(503)
        keys = []
        for kid in sorted(self.published):
            jwk = json.loads(RSAAlgorithm.to_jwk(self.keys[kid].public_key()))
            keys.append(jwk | {"kid": kid, "use": "sig", "alg": "RS256"})
        return httpx.Response(200, json={"keys": keys})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._jwks)

    def token(self, claims: dict[str, Any] | None = None, kid: str = "k1", drop: tuple[str, ...] = (), **headers: Any) -> str:
        now = int(time.time())
        body: dict[str, Any] = {"iss": ISSUER, "aud": AUDIENCE, "sub": "u-1", "iat": now, "exp": now + 300, "tenant": "default"}
        body |= claims or {}
        for k in drop:
            body.pop(k, None)
        return jwt.encode(body, self.keys[kid], algorithm="RS256", headers={"kid": kid, **headers})


def app_claims(client_id: str = "agent-demo", **extra: Any) -> dict[str, Any]:
    """Keycloak 服务账号（Client Credentials）令牌的典型 claims。"""
    return {"sub": "sa-1", "azp": client_id, "client_id": client_id, "preferred_username": f"service-account-{client_id}"} | extra


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[IdP]:
    """开启强制鉴权：auth 模块改用 AUTH_REQUIRED=true 的配置，JWKS 由测试 IdP 提供。"""
    provider = IdP()
    strict = dataclasses.replace(auth.settings, auth_required=True, jwks_url=JWKS_URL, jwt_issuer=ISSUER, jwt_audience=AUDIENCE)
    monkeypatch.setattr(auth, "settings", strict)
    auth.set_transport_for_tests(provider.transport())
    yield provider
    auth.set_transport_for_tests(None)


async def _audits(action: str | None = None) -> list[Audit]:
    async with SessionLocal() as s:
        q = select(Audit).order_by(Audit.id.desc())
        if action:
            q = q.where(Audit.action == action)
        return list((await s.execute(q.limit(20))).scalars())


def _chat(client: Any, headers: dict[str, str], agent_id: str = "demo") -> httpx.Response:
    body = {"agent_id": agent_id, "messages": [{"role": "user", "content": "hi"}], "stream": False}
    return client.post("/v1/messages", json=body, headers=headers)


# ---------------- 未带令牌 / 无效令牌 ----------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/messages"),
        ("get", "/v1/runs/r-1"),
        ("get", "/v1/runs/r-1/events"),
        ("post", "/v1/runs/r-1/cancel"),
        ("get", "/admin/agents"),
    ],
)
def test_missing_token_is_401_on_every_api(client: Any, idp: IdP, method: str, path: str) -> None:
    """强制鉴权下所有 /v1、/admin 接口未带令牌一律 401，并带 RFC 6750 的 WWW-Authenticate 头。"""
    r = getattr(client, method)(path, json={}) if method == "post" else client.get(path)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


def test_healthz_needs_no_token(client: Any, idp: IdP) -> None:
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize(
    ("case", "detail"),
    [
        ("expired", "token expired"),
        ("wrong_iss", "invalid token"),
        ("wrong_aud", "invalid token"),
        ("no_exp", "invalid token"),
        ("unknown_kid", "unknown signing key"),
        ("tampered", "invalid token"),
        ("hs256", "unsupported token algorithm"),
        ("none", "unsupported token algorithm"),
        ("garbage", "malformed token"),
    ],
)
def test_invalid_tokens_are_401(client: Any, idp: IdP, case: str, detail: str) -> None:
    """签名、算法、签发者、受众、过期、kid 任一不对都是 401；HS256 / none 在取公钥之前就被拒绝。"""
    now = int(time.time())
    if case == "expired":
        token = idp.token({"exp": now - 3600, "iat": now - 7200})
    elif case == "wrong_iss":
        token = idp.token({"iss": "http://evil.test/realms/x"})
    elif case == "wrong_aud":
        token = idp.token({"aud": "someone-else"})
    elif case == "no_exp":
        token = idp.token(drop=("exp",))
    elif case == "unknown_kid":
        idp.add_key("k-unpublished", publish=False)
        token = idp.token(kid="k-unpublished")
    elif case == "tampered":
        head, body, sig = idp.token().split(".")
        forged = jwt.utils.base64url_encode(json.dumps({"sub": "admin", "iss": ISSUER, "aud": AUDIENCE}).encode()).decode()
        token = f"{head}.{forged}.{sig}"
    elif case == "hs256":
        token = jwt.encode(
            {"sub": "x", "iss": ISSUER, "aud": AUDIENCE, "exp": now + 60, "iat": now}, "k" * 32, algorithm="HS256", headers={"kid": "k1"}
        )
    elif case == "none":
        token = jwt.encode({"sub": "x", "iss": ISSUER, "exp": now + 60, "iat": now}, None, algorithm="none", headers={"kid": "k1"})
    else:
        token = "not-a-jwt"  # noqa: S105 —— 测试用非法令牌
    r = _chat(client, bearer(token))
    assert r.status_code == 401, r.text
    assert r.json()["detail"].startswith(detail)


def test_missing_tenant_claim_is_403(client: Any, idp: IdP) -> None:
    """令牌验签通过但没有租户声明：拒绝，不回退到默认租户。"""
    r = _chat(client, bearer(idp.token(drop=("tenant",))))
    assert r.status_code == 403


# ---------------- 主体解析 ----------------


def test_client_credentials_token_identifies_app(client: Any, idp: IdP) -> None:
    """S2.2：应用以 Client Credentials 令牌调用，subject 与 actor 都是 app:<client_id>，审计记 client_credentials。"""
    r = _chat(client, bearer(idp.token(app_claims())))
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    run = client.get(f"/v1/runs/{run_id}", headers=bearer(idp.token(app_claims()))).json()
    assert run["subject"] == "app:agent-demo"
    events = client.get(f"/v1/runs/{run_id}/events", headers=bearer(idp.token(app_claims()))).json()
    assert events[0]["payload"]["actor"] == "app:agent-demo"


async def test_client_credentials_request_is_audited(client: Any, idp: IdP) -> None:
    r = _chat(client, bearer(idp.token(app_claims())))
    run_id = r.json()["run_id"]
    row = next(a for a in await _audits("POST /v1/messages") if a.object == f"run:{run_id}")
    assert (row.actor, row.act, row.auth_method, row.tenant_id, row.result) == (
        "app:agent-demo",
        "app:agent-demo",
        "client_credentials",
        "default",
        "ok",
    )


def test_user_token_with_act_claim() -> None:
    """用户令牌：subject=human:<sub>；act 声明（RFC 8693 对象或 Keycloak 字符串）记为 actor，否则取签发客户端。"""
    base = {"sub": "u-9", "tenant": "t1", "azp": "portal-web", "realm_access": {"roles": ["sales"]}}
    p = auth.principal_from_claims(base | {"act": {"sub": "agent:ticket-bot"}})
    assert (p.subject, p.actor, p.auth_method, p.tenant_id, p.roles) == (
        "human:u-9",
        "agent:ticket-bot",
        "user",
        "t1",
        frozenset({"sales"}),
    )
    assert auth.principal_from_claims(base | {"act": "agent:x"}).actor == "agent:x"
    assert auth.principal_from_claims(base).actor == "app:portal-web"


# ---------------- 租户隔离与角色 ----------------


def test_other_tenant_cannot_see_agent_or_run(client: Any, idp: IdP) -> None:
    """令牌租户与 Agent / Run 的租户不同：与不存在一样返回 404。"""
    own = bearer(idp.token(app_claims()))
    run_id = _chat(client, own).json()["run_id"]
    other = bearer(idp.token(app_claims(tenant="t-other")))
    assert _chat(client, other).status_code == 404
    assert client.get(f"/v1/runs/{run_id}", headers=other).status_code == 404
    assert client.post(f"/v1/runs/{run_id}/cancel", headers=other).status_code == 404


def test_admin_requires_platform_admin_role(client: Any, idp: IdP) -> None:
    body = {"agent_id": "auth-admin-probe", "version": "1"}
    assert client.post("/admin/agents", json=body, headers=bearer(idp.token(app_claims()))).status_code == 403
    admin = bearer(idp.token(app_claims("console-api", realm_access={"roles": ["platform-admin"]})))
    assert client.post("/admin/agents", json=body, headers=admin).status_code == 201


# ---------------- JWKS 缓存 ----------------


def test_jwks_is_cached_and_refreshed_on_key_rotation(client: Any, idp: IdP, monkeypatch: pytest.MonkeyPatch) -> None:
    """多次请求只拉一次 JWKS；IdP 轮换出新 kid 后按需刷新一次即可验签（去掉最小刷新间隔，模拟间隔已过）。"""
    monkeypatch.setattr(auth, "JWKS_MIN_REFRESH_S", 0.0)
    for _ in range(3):
        assert _chat(client, bearer(idp.token(app_claims()))).status_code == 200
    assert idp.fetches == 1
    idp.add_key("k2")
    assert _chat(client, bearer(idp.token(app_claims(), kid="k2"))).status_code == 200
    assert idp.fetches == 2


def test_unknown_kid_refresh_is_rate_limited(client: Any, idp: IdP) -> None:
    """伪造 kid 的请求不能把 IdP 打满：最小刷新间隔内只刷新一次。"""
    assert _chat(client, bearer(idp.token(app_claims()))).status_code == 200
    idp.add_key("k-fake", publish=False)
    for _ in range(5):
        assert _chat(client, bearer(idp.token(app_claims(), kid="k-fake"))).status_code == 401
    assert idp.fetches <= 2


def test_jwks_unavailable_is_503(client: Any, idp: IdP) -> None:
    idp.down = True
    assert _chat(client, bearer(idp.token(app_claims()))).status_code == 503


def test_jwks_outage_does_not_refetch_on_every_request(client: Any, idp: IdP, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存过期且 IdP 不可达：沿用旧公钥，且最小刷新间隔内不再逐请求重试 IdP（评审项：过期刷新不限频）。"""
    assert _chat(client, bearer(idp.token(app_claims()))).status_code == 200
    cache = auth._jwks_cache()
    cache._fetched_at -= cache.ttl_s + 1  # 模拟缓存过期
    cache._attempted_at -= auth.JWKS_MIN_REFRESH_S + 1
    idp.down = True
    for _ in range(4):
        assert _chat(client, bearer(idp.token(app_claims()))).status_code == 200
    assert idp.fetches == 2  # 首次拉取 + 过期后一次失败的刷新


@pytest.mark.parametrize("header", [{"alg": ["RS256"]}, {"kid": ["k1"]}])
def test_non_string_header_fields_are_401(client: Any, idp: IdP, header: dict[str, Any]) -> None:
    """令牌头里 alg / kid 不是字符串：401 而不是 500。"""
    head, body, sig = idp.token(app_claims()).split(".")
    raw = json.loads(jwt.utils.base64url_decode(head)) | header
    forged = jwt.utils.base64url_encode(json.dumps(raw).encode()).decode()
    assert _chat(client, bearer(f"{forged}.{body}.{sig}")).status_code == 401


def test_service_account_username_alone_is_not_an_app() -> None:
    """只认 client_id 声明判定应用令牌：用户名形如 service-account-x 的普通用户仍是人员。"""
    p = auth.principal_from_claims({"sub": "u-2", "tenant": "default", "azp": "portal-web", "preferred_username": "service-account-x"})
    assert (p.subject, p.auth_method) == ("human:u-2", "user")


def test_approver_must_be_another_human(idp: IdP) -> None:
    """强制鉴权时：应用令牌不能审批；人员不能审批自己发起的 Run；其他人员可以。"""
    from starlette.requests import Request

    req = Request({"type": "http", "headers": [], "state": {}})
    app = auth.Principal(subject="app:agent-demo", auth_method="client_credentials")
    with pytest.raises(HTTPException) as e:
        auth.check_approver(req, app, "app:agent-demo")
    assert e.value.status_code == 403
    alice = auth.Principal(subject="human:alice", auth_method="user")
    with pytest.raises(HTTPException):
        auth.check_approver(req, alice, "human:alice")
    auth.check_approver(req, alice, "app:agent-demo")  # 人员审批应用发起的 Run：放行
