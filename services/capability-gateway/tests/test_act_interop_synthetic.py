"""Real gateway signer / MCP verifier interoperability; only ephemeral keys and HTTP doubles."""

import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException

from capability_gateway import act as issuer
from capability_gateway import config
from mcp_template import jwks
from mcp_template.act import ActConfig, replay_cache, verify_act_assertion

SYNTHETIC_OBO = "synthetic-obo"


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(config, "ACT_ISSUER", "synthetic-gateway")
    monkeypatch.setattr(config, "ACT_TTL_S", 60)
    issuer.reset_for_tests(ec.generate_private_key(ec.SECP256R1()))

    def serve(request):
        assert request.method == "GET"
        assert str(request.url) == "http://synthetic.invalid/jwks"
        return httpx.Response(200, json=issuer.jwks())

    jwks.set_transport_for_tests(httpx.MockTransport(serve))
    replay_cache.clear()
    yield ActConfig(jwks_url="http://synthetic.invalid/jwks", issuer=config.ACT_ISSUER, audience="mcp-ticket")
    jwks.set_transport_for_tests(None)
    replay_cache.clear()
    issuer.reset_for_tests()


def assertion(**changes):
    values = dict(
        sub="alice",
        audience="mcp-ticket",
        agent_id="ticket",
        agent_version="1",
        caller="runtime",
        run_id="run-1",
        call_id="call-1",
        obo_token=SYNTHETIC_OBO,
    )
    return issuer.sign(**(values | changes))


async def verify(token, cfg, **changes):
    return await verify_act_assertion(token, cfg=cfg, **(dict(obo_token=SYNTHETIC_OBO, obo_sub="alice") | changes))


@pytest.mark.asyncio
async def test_signed_chain_and_replay(bridge):
    token = assertion()
    context = await verify(token, bridge)
    assert context.verified
    assert context.chain == ("agent:ticket@1", "client:runtime")
    assert (context.run_id, context.call_id) == ("run-1", "call-1")
    with pytest.raises(HTTPException) as error:
        await verify(token, bridge)
    assert error.value.status_code == 401
    assert error.value.detail == "act assertion replayed"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"obo_token": "replacement-token-same-user"}, {"obo_sub": "bob"}])
async def test_binding_rejection_does_not_consume_valid_assertion(bridge, changes):
    token = assertion()
    with pytest.raises(HTTPException) as error:
        await verify(token, bridge, **changes)
    assert error.value.status_code == 403
    assert (await verify(token, bridge)).verified


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"audience": "other-connector"}, {"now": time.time() - 120}])
async def test_wrong_target_or_expired(bridge, changes):
    with pytest.raises(HTTPException) as error:
        await verify(assertion(**changes), bridge)
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_no_obo_binding_is_rejected(bridge):
    with pytest.raises(HTTPException) as error:
        await verify(assertion(obo_token=None), bridge)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_jwks_failure_closed(bridge):
    token = assertion()

    def unavailable(request):
        raise httpx.ConnectError("synthetic outage", request=request)

    jwks.set_transport_for_tests(httpx.MockTransport(unavailable))
    with pytest.raises(HTTPException) as error:
        await verify(token, bridge)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_authenticated_invoke_to_actual_mcp_verifier(bridge, keycloak, ticket_connector):
    from fastapi.testclient import TestClient

    from capability_gateway import main

    headers = {
        "Authorization": f"Bearer {keycloak.idp.service_token()}",
        "X-AI-User-Token": keycloak.idp.user_token(sub="alice"),
        "X-AI-Act": "agent:forged@99",
        "X-AI-Act-Assertion": "forged-client-assertion",
        "X-AI-Run-Id": "forged-run",
    }
    body = dict(
        tool="ticket.get",
        input={"id": "T-1001"},
        subject="forged-user",
        agent_id="ticket",
        agent_version="1",
        run_id="run-1",
        call_id="call-1",
    )
    with TestClient(main.app) as client:
        result = client.post("/invoke", json=body, headers=headers)
    assert result.status_code == 200
    (request,) = keycloak.connector
    assert request.headers["authorization"] == "Bearer obo-1"
    assert request.headers["x-ai-run-id"] == "run-1"
    context = await verify_act_assertion(
        request.headers["x-ai-act-assertion"],
        obo_token=request.headers["authorization"].removeprefix("Bearer "),
        obo_sub="alice",
        cfg=bridge,
    )
    assert context.verified
    assert context.chain == ("agent:ticket@1", "client:ai-runtime-svc")
    assert (context.run_id, context.call_id) == ("run-1", "call-1")
    assert keycloak.policy[-1]["principal"]["sub"] == "alice"


@pytest.mark.parametrize("failure", ["untrusted", "deny", "policy_down"])
def test_rejected_invocation_never_signs(bridge, keycloak, ticket_connector, monkeypatch, failure):
    from fastapi.testclient import TestClient

    from capability_gateway import main

    signed = []

    def must_not_sign(**kwargs):
        signed.append(kwargs)
        raise AssertionError("Rejected invocation reached signing")

    monkeypatch.setattr(issuer, "sign", must_not_sign)
    service = keycloak.idp.service_token()
    if failure == "untrusted":
        service = keycloak.idp.token(sub="u", azp="portal-web", aud="capability-gateway")
    elif failure == "deny":
        keycloak.decisions["cap.ticket.get"] = "deny"
    else:
        keycloak.policy_down = True
    with TestClient(main.app) as client:
        result = client.post(
            "/invoke",
            json=dict(tool="ticket.get", input={"id": "T-1001"}, agent_id="ticket", agent_version="1"),
            headers={"Authorization": f"Bearer {service}", "X-AI-User-Token": keycloak.idp.user_token()},
        )
    assert result.status_code == (401 if failure == "untrusted" else 403)
    assert signed == []
    assert keycloak.token_calls == []
    assert keycloak.connector == []
