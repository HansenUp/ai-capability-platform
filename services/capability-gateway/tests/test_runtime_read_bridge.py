"""Temporary Runtime ledger -> signed Gateway -> real MCP verifier, no sockets."""

from __future__ import annotations

import socket
from dataclasses import replace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from gateway_testkit import ISSUER, JWKS_URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from capability_gateway import act, config, main
from mcp_template import jwks
from mcp_template.act import ActConfig, replay_cache, verify_act_assertion


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("real network is forbidden in B3 isolation")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture
async def ledger(tmp_path, monkeypatch, keycloak, ticket_connector):
    # Runtime test conftest must establish its environment before Settings/DB
    # are imported. Cross-service collection must not freeze default settings.
    from runtime.api import auth
    from runtime.config import settings
    from runtime.guard.gate import params_hash
    from runtime.state.db import AgentVersion, Base, Run, RunEventRow
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'read.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(
        auth, "settings", replace(settings, jwt_issuer=ISSUER, jwt_audience="ai-runtime", jwks_url=JWKS_URL, jwt_tenant_claim="tenant")
    )
    from runtime import net

    monkeypatch.setattr(net, "settings", replace(settings, egress_allowlist=("http://idp.test",)))
    auth.set_transport_for_tests(httpx.MockTransport(keycloak))
    act.reset_for_tests(ec.generate_private_key(ec.SECP256R1()))
    jwks.set_transport_for_tests(httpx.MockTransport(lambda request: httpx.Response(200, json=act.jwks())))
    replay_cache.clear()
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(
                AgentVersion(agent_id="ticket", version="1", tenant_id="default", status="published", config={"tools": ["ticket.get"]})
            )
            session.add(
                Run(
                    run_id="r1",
                    agent_id="ticket",
                    agent_version="1",
                    tenant_id="default",
                    model_alias="mock",
                    subject="human:user-1",
                    state="working",
                    last_seq=3,
                )
            )
            fingerprint = params_hash({"id": "T-1001"})
            session.add_all(
                [
                    RunEventRow(
                        run_id="r1",
                        seq=1,
                        type="tool.requested",
                        payload={"call_id": "c1", "tool": "ticket.get", "input": {"id": "T-1001"}, "risk_level": "read"},
                    ),
                    RunEventRow(
                        run_id="r1", seq=2, type="tool.decision", payload={"call_id": "c1", "decision": "allow", "params_hash": fingerprint}
                    ),
                    RunEventRow(
                        run_id="r1",
                        seq=3,
                        type="tool.executing",
                        payload={"call_id": "c1", "tool": "ticket.get", "params_hash": fingerprint},
                    ),
                ]
            )
            await session.commit()
            yield session
    finally:
        auth.set_transport_for_tests(None)
        jwks.set_transport_for_tests(None)
        replay_cache.clear()
        act.reset_for_tests()
        await engine.dispose()
        (tmp_path / "read.db").unlink(missing_ok=True)


async def execute(ledger, keycloak, **changes):
    from runtime.isolated_read import invoke_read

    return await invoke_read(
        ledger,
        **(
            {
                "run_id": "r1",
                "call_id": "c1",
                "user_token": keycloak.idp.user_token(),
                "service_token": keycloak.idp.service_token(),
                "gateway": httpx.ASGITransport(app=main.app),
            }
            | changes
        ),
    )


async def test_persisted_context_to_signed_assertion(ledger, keycloak):
    result = await execute(ledger, keycloak)
    assert result["ok"] and result["output"]["id"] == "T-1001"
    (request,) = keycloak.connector
    verified = await verify_act_assertion(
        request.headers[act.HEADER],
        cfg=ActConfig(jwks_url="http://test.invalid/jwks", issuer=config.ACT_ISSUER, audience="mcp-ticket"),
        obo_token=request.headers["authorization"].removeprefix("Bearer "),
        obo_sub="user-1",
    )
    assert verified.verified and verified.chain == ("agent:ticket@1", "client:ai-runtime-svc")
    assert (verified.run_id, verified.call_id) == ("r1", "c1")
    assert keycloak.policy[-1]["context"]["taint"] == ["confidential"]
    with pytest.raises(HTTPException) as error:
        await verify_act_assertion(
            request.headers[act.HEADER],
            cfg=ActConfig(jwks_url="http://test.invalid/jwks", issuer=config.ACT_ISSUER, audience="mcp-ticket"),
            obo_token=request.headers["authorization"].removeprefix("Bearer "),
            obo_sub="user-1",
        )
    assert error.value.status_code == 401


@pytest.mark.parametrize(
    "failure",
    [
        "subject",
        "tenant",
        "retired",
        "terminal",
        "input",
        "write",
        "denied",
        "resolved",
        "untrusted",
        "policy",
        "approval",
        "expired",
        "policy_down",
        "gateway_write",
        "invalid_signature",
        "duplicate",
    ],
)
async def test_fail_closed(ledger, keycloak, failure):
    from runtime.state.db import AgentVersion, Run, RunEventRow

    changes = {}
    if failure in {"subject", "tenant"}:
        changes["user_token"] = keycloak.idp.user_token(**({"sub": "other"} if failure == "subject" else {"tenant": "other"}))
    elif failure == "invalid_signature":
        from gateway_testkit import Idp

        changes["user_token"] = Idp().user_token()
    elif failure == "policy_down":
        keycloak.policy_down = True
    elif failure == "gateway_write":
        from capability_gateway import state

        cap = state.catalog._caps["cap.ticket.get"]
        state.catalog._caps["cap.ticket.get"] = cap.model_copy(update={"risk_level": "write_high"})
    elif failure == "duplicate":
        event = await ledger.get(RunEventRow, ("r1", 1))
        ledger.add(RunEventRow(run_id="r1", seq=4, type="tool.requested", payload=event.payload))
    elif failure == "expired":
        changes["user_token"] = keycloak.idp.user_token(exp=1)
    elif failure == "untrusted":
        changes["service_token"] = keycloak.idp.service_token(azp="portal-web")
    elif failure in {"policy", "approval"}:
        keycloak.decisions["cap.ticket.get"] = "deny" if failure == "policy" else "require_approval"
    elif failure == "retired":
        from sqlalchemy import select

        agent = await ledger.scalar(select(AgentVersion))
        agent.status = "retired"
    elif failure == "terminal":
        (await ledger.get(Run, "r1")).state = "completed"
    else:
        seq = 2 if failure == "denied" else 1
        event = await ledger.get(RunEventRow, ("r1", seq))
        if failure == "input":
            event.payload = event.payload | {"input": {"id": "replacement"}}
        elif failure == "write":
            event.payload = event.payload | {"risk_level": "write_high"}
        elif failure == "denied":
            event.payload = event.payload | {"decision": "deny"}
        elif failure == "resolved":
            ledger.add(RunEventRow(run_id="r1", seq=4, type="tool.result", payload={"call_id": "c1"}))
    await ledger.commit()
    with pytest.raises((HTTPException, httpx.HTTPStatusError)):
        await execute(ledger, keycloak, **changes)
    assert not keycloak.connector and not keycloak.token_calls


async def test_browser_context_cannot_be_supplied(ledger, keycloak):
    with pytest.raises(TypeError):
        await execute(ledger, keycloak, agent_id="forged", approved=True)
    assert not keycloak.connector


async def test_business_failure_is_not_success_and_not_retried(ledger, keycloak):
    keycloak.connector_status = 503
    result = await execute(ledger, keycloak)
    assert result["ok"] is False and result["status_code"] == 503
    assert len(keycloak.connector) == 1


async def test_bridge_obeys_runtime_egress(ledger, keycloak, monkeypatch):
    from runtime import net

    monkeypatch.setattr(net, "allowed_origins", lambda: frozenset({net.origin("http://idp.test")}))
    with pytest.raises(net.EgressDenied):
        await execute(ledger, keycloak)
    assert not keycloak.connector
