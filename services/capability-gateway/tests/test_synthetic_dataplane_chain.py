"""Synthetic data-plane composition; does not execute or emulate agentgateway itself."""

import time

import jwt
import pytest
from fastapi.testclient import TestClient
from gateway_testkit import ISSUER, Upstream

from capability_gateway import config, main, state
from capability_gateway.catalog import ConnectorVersion


@pytest.fixture
async def data_plane(up: Upstream, monkeypatch: pytest.MonkeyPatch):
    await state.catalog.put_connector(
        ConnectorVersion(name="mcp-ticket", endpoint="http://mcp.test/mcp", protocol="mcp", system="ticket", status="active")
    )
    monkeypatch.setattr(config, "GATEWAY_AUTHZ_TOKEN", "synthetic-authz-key")
    return SyntheticDataPlane(up)


class SyntheticDataPlane:
    """Test harness, not agentgateway, a production middleware or a trust boundary."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.callback = TestClient(main.app)
        self.forwarded = []

    def invoke(self, token, *, client_headers=None, callback_key="synthetic-authz-key", callback_down=False):
        try:
            claims = jwt.decode(
                token,
                self.upstream.idp.key.public_key(),
                algorithms=["ES256"],
                issuer=ISSUER,
                audience="mcp-ticket",
                options={"require": ["sub", "tenant", "iss", "aud", "exp", "iat"]},
            )
        except jwt.PyJWTError:
            return "identity-denied"
        if not isinstance(claims["tenant"], str) or not claims["tenant"] or not isinstance(claims["sub"], str) or not claims["sub"]:
            return "identity-denied"
        if callback_down:
            return "callback-unavailable"
        response = self.callback.post(
            "/authz/mcp-check",
            headers={"X-Gateway-Authz-Token": callback_key},
            json={
                "server": "mcp-ticket",
                "tool": "ticket.search",
                "subject": claims["sub"],
                "tenant": claims["tenant"],
                "agent": "test-agent@1",
            },
        )
        if response.status_code != 200:
            return "authorization-denied"
        if response.json().get("decision") != "allow":
            return "approval-required"
        # Synthetic forwarding drops arbitrary client headers. This harness cannot prove agentgateway does so.
        self.forwarded.append({"authorization": "Bearer " + token, "subject": claims["sub"], "tenant": claims["tenant"]})
        return "forwarded"


def token(plane, **claims):
    return plane.upstream.idp.token(**({"sub": "alice", "tenant": "tenant-a", "aud": "mcp-ticket"} | claims))


def test_signed_identity_reaches_actual_callback_and_only_test_upstream(data_plane):
    t = token(data_plane)
    assert data_plane.invoke(t, client_headers={"x-ai-dev-sub": "attacker", "x-ai-act": "attacker"}) == "forwarded"
    assert data_plane.forwarded == [{"authorization": "Bearer " + t, "subject": "alice", "tenant": "tenant-a"}]
    request = data_plane.upstream.policy[-1]
    assert request["principal"] == {"sub": "alice", "tenant": "tenant-a", "roles": []}
    assert request["context"]["agent"] == {"id": "test-agent", "version": "1", "tenant": "tenant-a"}


@pytest.mark.parametrize(
    "mode", ["expired", "wrong-audience", "tampered", "no-tenant", "callback-token", "callback-down", "policy-down", "deny", "approval"]
)
def test_each_failure_blocks_synthetic_upstream(data_plane, mode):
    t = token(data_plane)
    kwargs = {}
    if mode == "expired":
        t = token(data_plane, exp=int(time.time()) - 100)
    elif mode == "wrong-audience":
        t = token(data_plane, aud="another-service")
    elif mode == "tampered":
        head, body, signature = t.split(".")
        signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        t = ".".join((head, body, signature))
    elif mode == "no-tenant":
        t = token(data_plane, tenant="")
    elif mode == "callback-token":
        kwargs["callback_key"] = "wrong"
    elif mode == "callback-down":
        kwargs["callback_down"] = True
    elif mode == "policy-down":
        data_plane.upstream.policy_down = True
    elif mode in {"deny", "approval"}:
        data_plane.upstream.decisions["cap.ticket.search"] = "deny" if mode == "deny" else "require_approval"
    outcome = data_plane.invoke(t, **kwargs)
    expected = {
        "expired": "identity-denied",
        "wrong-audience": "identity-denied",
        "tampered": "identity-denied",
        "no-tenant": "identity-denied",
        "callback-token": "authorization-denied",
        "callback-down": "callback-unavailable",
        "policy-down": "authorization-denied",
        "deny": "authorization-denied",
        "approval": "approval-required",
    }
    assert outcome == expected[mode]
    assert data_plane.forwarded == []
