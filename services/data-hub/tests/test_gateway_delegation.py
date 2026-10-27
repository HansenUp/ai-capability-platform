"""Actual gateway invoke with isolated identity, policy, catalog and connector substitutes."""

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from test_guarded import signed_idp as signed_idp
from test_query import sample as sample
from test_reviewed import setup as setup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from capability_gateway import act, auth, config, connectors, main, policy, state  # noqa: E402
from capability_gateway.auth import Caller  # noqa: E402
from capability_gateway.errors import GatewayError  # noqa: E402
from capability_gateway.policy import Principal  # noqa: E402
from capability_gateway.registry import Capability  # noqa: E402
from data_hub.delegated_identity import DelegatedIdentity, DelegationProfile  # noqa: E402
from data_hub.local_tool import LocalDataQuery  # noqa: E402
from data_hub.query import QueryDenied  # noqa: E402
from data_hub.reviewed import ReviewedQuery, UserProfile  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "allow",
        "invoke-denied",
        "read-denied",
        "read-unavailable",
        "bad-binding",
        "replay",
        "runtime-allow",
        "auth-allow",
        "auth-runtime",
        "auth-no-service",
        "auth-wrong-aud",
        "auth-forged",
        "auth-no-user",
    ],
)
async def test_real_gateway_invoke_to_isolated_datahub(setup, monkeypatch, mode):
    path, _, test_state, _, build = setup
    idp_key = ec.generate_private_key(ec.SECP256R1())
    gateway_key = ec.generate_private_key(ec.SECP256R1())
    now = int(time.time())
    claims = {
        "iss": "https://idp.invalid",
        "aud": "data-hub",
        "azp": "gateway",
        "sub": "alice",
        "tenant": "tenant-a",
        "token_use": "access",
        "principal_type": "user",
        "iat": now,
        "exp": now + 300,
        "realm_access": {"roles": ["viewer"]},
    }
    obo = jwt.encode(claims, idp_key, algorithm="ES256", headers={"typ": "at+jwt"})
    cap = Capability(
        id="cap.data.query",
        name="data.query",
        description="synthetic",
        tenant="tenant-a",
        system="data",
        risk_level="read",
        data_class="confidential",
    )
    connector = SimpleNamespace(name="data-hub", endpoint="http://data-hub.invalid", protocol="rest", health="up")

    class Catalog:
        async def by_name(self, name, tenant):
            return cap if name == "data.query" and tenant == "tenant-a" else None

        async def connector_for(self, capability):
            return connector

    async def decision(*args, **kwargs):
        return policy.Decision("deny" if mode == "invoke-denied" else "allow")

    async def compliant(*args):
        return None

    async def exchange(*args):
        return obo, time.monotonic() + 60

    monkeypatch.setattr(state, "catalog", Catalog())
    monkeypatch.setattr(policy, "check", decision)
    monkeypatch.setattr(connectors, "require_compliant_tool", compliant)
    monkeypatch.setattr(main, "_obo_token", exchange)
    monkeypatch.setattr(config, "KEYCLOAK_TOKEN_URL", "https://idp.invalid/token")
    monkeypatch.setattr(config, "ACT_ISSUER", "gateway-issuer")
    monkeypatch.setattr(config, "ACT_TTL_S", 60)
    act.reset_for_tests(gateway_key)
    profile = DelegationProfile("https://idp.invalid", "data-hub", "gateway", "gateway-issuer", "runtime", "agent", "v1", "run", "call")
    seen = []

    async def transport(request):
        if request.url.host == "idp.invalid":
            assert request.method == "GET" and request.url.path == "/certs"
            jwk = jwt.algorithms.ECAlgorithm.to_jwk(idp_key.public_key(), as_dict=True)
            return httpx.Response(200, json={"keys": [jwk | {"kid": "synthetic-idp", "alg": "ES256", "use": "sig"}]})
        assert request.url.host == "data-hub.invalid" and request.method == "POST"
        received_obo = request.headers["authorization"].removeprefix("Bearer ")
        assertion = request.headers["X-AI-Act-Assertion"]
        if mode == "bad-binding":
            received_obo += "changed"

        async def tokens():
            return received_obo, assertion

        async def obo_verify(token):
            return jwt.decode(
                token,
                idp_key.public_key(),
                algorithms=["ES256"],
                audience="data-hub",
                issuer="https://idp.invalid",
                options={"require": ["iat", "exp", "iss", "aud"]},
            )

        async def act_verify(token):
            return jwt.decode(
                token,
                gateway_key.public_key(),
                algorithms=["ES256"],
                audience="data-hub",
                issuer="gateway-issuer",
                options={"require": ["iat", "exp", "iss", "aud"]},
            )

        identity = DelegatedIdentity(profile, tokens, obo_verify, act_verify)
        old = build()

        async def data_policy(scope):
            seen.append(scope.action)
            if mode == "read-unavailable":
                raise RuntimeError("policy unavailable")
            result = await old._policy(scope)
            return replace(result, effect="deny") if mode == "read-denied" else result

        guard = ReviewedQuery(
            path,
            old._registry,
            UserProfile("https://idp.invalid", "data-hub", frozenset({"gateway"}), allow_delegated=True),
            identity,
            old._binding,
            data_policy,
            gate_timeout_s=1,
        )
        arguments = json.loads(request.content)
        try:
            output = await LocalDataQuery(guard).invoke(arguments)
            if mode == "replay":
                assert await LocalDataQuery(guard).invoke(arguments) == output
            return httpx.Response(200, json=output)
        except QueryDenied:
            return httpx.Response(403, json={"detail": "Restricted query denied"})

    monkeypatch.setattr(config, "transport", httpx.MockTransport(transport))
    monkeypatch.setattr(config, "JWT_ISSUER", "https://idp.invalid")
    monkeypatch.setattr(config, "JWKS_URL", "https://idp.invalid/certs")
    monkeypatch.setattr(config, "SERVICE_AUDIENCE", "capability-gateway")
    monkeypatch.setattr(config, "USER_AUDIENCE", frozenset({"ai-runtime"}))
    monkeypatch.setattr(config, "TRUSTED_CALLERS", frozenset({"runtime"}))
    auth.reset_for_tests()
    caller = Caller("runtime", Principal("alice", "tenant-a", ("viewer",), exp=now + 300, token="synthetic"))  # noqa: S106 - synthetic test token
    body = main.InvokeIn(
        tool="data.query",
        input={"view_id": "orders", "version": "v1", "columns": ["item", "secret"], "filters": [], "limit": 10},
        agent_id="agent",
        agent_version="v1",
        agent_tenant="tenant-a",
        run_id="run",
        call_id="call",
    )
    service_claims = {
        "iss": "https://idp.invalid",
        "aud": "capability-gateway",
        "sub": "service-account-runtime",
        "azp": "runtime",
        "iat": now,
        "exp": now + 300,
    }
    user_claims = {
        "iss": "https://idp.invalid",
        "aud": "ai-runtime",
        "sub": "alice",
        "tenant": "tenant-a",
        "azp": "portal",
        "iat": now,
        "exp": now + 300,
        "realm_access": {"roles": ["viewer"]},
    }
    if mode == "auth-wrong-aud":
        service_claims["aud"] = "other"
    service_token = jwt.encode(
        service_claims, gateway_key if mode == "auth-forged" else idp_key, algorithm="ES256", headers={"kid": "synthetic-idp"}
    )
    user_token = jwt.encode(user_claims, idp_key, algorithm="ES256", headers={"kid": "synthetic-idp"})
    headers = {"Authorization": f"Bearer {service_token}", "X-AI-User-Token": f"Bearer {user_token}"}
    if mode == "auth-no-service":
        del headers["Authorization"]
    if mode == "auth-no-user":
        del headers["X-AI-User-Token"]
    try:
        if mode.startswith("auth-"):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://gateway.invalid") as client:
                if mode == "auth-runtime":
                    from ai_contract import ToolSpec
                    from runtime.loop import toolbroker

                    async def runtime_request(method, url, *, user_token, timeout_s, json):
                        assert user_token == "synthetic-user"  # noqa: S105 - synthetic test credential
                        return await client.request(method, url, json=json, headers=headers)

                    monkeypatch.setattr(toolbroker.gateway_client, "request", runtime_request)
                    rc = SimpleNamespace(
                        gate=SimpleNamespace(subject="alice", approved_call_ids={}, taint=set()),
                        agent=SimpleNamespace(agent_id="agent", version="v1", tenant_id="tenant-a"),
                        run_id="run",
                        credential="synthetic-user",
                    )
                    tool = ToolSpec(name="data.query", description="synthetic", capability_id="cap.data.query")
                    result = await toolbroker._dispatch_via_gateway(tool, body.input, False, rc, "call", None)
                    assert len(result["rows"]) == 3 and seen == ["read_data", "read_data"]
                else:
                    response = await client.post("/invoke", json=body.model_dump(), headers=headers)
                    if mode == "auth-allow":
                        assert response.status_code == 200 and response.json()["ok"] is True
                        assert seen == ["read_data", "read_data"]
                    else:
                        assert response.status_code == 401 and seen == []
        elif mode == "invoke-denied":
            with pytest.raises(GatewayError):
                await main.invoke(body, caller)
            assert seen == []
        elif mode == "runtime-allow":
            from ai_contract import ToolSpec
            from runtime.loop import toolbroker

            async def fake_gateway_request(method, url, *, user_token, timeout_s, json):
                assert method == "POST" and url == "/invoke" and user_token == "synthetic-user"  # noqa: S105 - synthetic test credential
                response = await main.invoke(main.InvokeIn(**json), caller)
                return httpx.Response(200, json=response)

            monkeypatch.setattr(toolbroker.gateway_client, "request", fake_gateway_request)
            rc = SimpleNamespace(
                gate=SimpleNamespace(subject="alice", approved_call_ids={}, taint=set()),
                agent=SimpleNamespace(agent_id="agent", version="v1", tenant_id="tenant-a"),
                run_id="run",
                credential="synthetic-user",
            )
            tool = ToolSpec(name="data.query", description="synthetic", capability_id="cap.data.query")
            output = await toolbroker._dispatch_via_gateway(tool, body.input, False, rc, "call", None)
            assert len(output["rows"]) == 3 and all(row[1] == "[REDACTED]" for row in output["rows"])
            assert seen == ["read_data", "read_data"]
        else:
            result = await main.invoke(body, caller)
            if mode in ("allow", "replay"):
                assert result["ok"] is True and len(result["output"]["rows"]) == 3
                assert all(row[1] == "[REDACTED]" for row in result["output"]["rows"])
                assert seen == ["read_data"] * (4 if mode == "replay" else 2)
            else:
                assert result["ok"] is False and result["status_code"] == 403
                assert seen == ([] if mode == "bad-binding" else ["read_data"])
    finally:
        act.reset_for_tests()
        auth.reset_for_tests()
