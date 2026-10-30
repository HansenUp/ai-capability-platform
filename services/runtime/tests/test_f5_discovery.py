"""Signed users + disposable Runtime SQLite; visibility is an injected Console substitute."""

import copy
import uuid

import pytest
import test_s7_run_api as authkit
from test_auth import bearer

from runtime.api import isolated_discovery as discovery
from runtime.state import db


@pytest.fixture(name="idp")
def signed_provider(monkeypatch):
    yield from authkit.idp.__wrapped__(monkeypatch)


@pytest.fixture
def lab(client, monkeypatch):
    from runtime.main import app

    if not any(getattr(r, "path", "") == "/v1/isolated-discovery/agents" for r in app.routes):
        app.include_router(discovery.router)
    monkeypatch.setattr(app.state, "isolated_discovery", discovery.ENABLED, raising=False)
    snapshot = {"tenant": "default", "revision": 1, "rules": []}

    async def read(tenant):
        return copy.deepcopy(snapshot if tenant == "default" else {"tenant": tenant, "revision": 1, "rules": []})

    monkeypatch.setattr(app.state, "isolated_visibility_reader", read, raising=False)
    return client, snapshot


ROOT = "/v1/isolated-discovery/agents"


def who(idp, **claims):
    return bearer(idp.token({"sub": "alice", **claims}))


def seed(client, *, tenant="default", status="published", percent=100, allowlist=None):
    identifier = "e1-" + uuid.uuid4().hex

    async def create():
        async with db.SessionLocal() as session:
            session.add(
                db.AgentVersion(
                    agent_id=identifier,
                    version="1",
                    tenant_id=tenant,
                    status=status,
                    rollout_percent=percent,
                    rollout_allowlist=allowlist or [],
                    system_prompt="DO-NOT-EXPOSE",
                    config={"secret": "DO-NOT-EXPOSE"},
                )
            )
            await session.commit()

    client.portal.call(create)
    return identifier


def rule(agent, **values):
    return {"agent_id": agent, "groups": [], "roles": [], "allowlist": ["human:alice"], **values}


def test_exact_intersection_and_minimal_output(lab, idp):
    client, snapshot = lab
    visible = seed(client)
    draft = seed(client, status="draft")
    foreign = seed(client, tenant="foreign")
    hidden = seed(client)
    unsupported = seed(client)
    empty = seed(client)
    raw = seed(client)
    snapshot["rules"] = [
        rule(visible),
        rule(draft),
        rule(foreign),
        rule(unsupported, roles=["sales"]),
        rule(empty, allowlist=[]),
        rule(raw, allowlist=["alice"]),
    ]
    response = client.get(ROOT, headers=who(idp))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"profile": "isolated-e1a-v1", "items": [{"agent_id": visible, "version": "1"}], "snapshot_guaranteed": False}
    assert "DO-NOT-EXPOSE" not in response.text and hidden not in response.text
    assert client.get(ROOT, headers=who(idp, sub="bob", realm_access={"roles": ["platform-admin"]})).json()["items"] == []
    assert client.get(ROOT, headers=who(idp, tenant="foreign")).json()["items"] == []
    snapshot["rules"] = [rule(visible, groups=["team"])]
    assert client.get(ROOT, headers=who(idp)).json()["items"] == []


def test_direct_user_only_and_disabled(lab, idp, monkeypatch):
    from runtime.config import settings
    from runtime.main import app

    client, _ = lab
    assert client.get(ROOT).status_code == 401
    assert client.get(ROOT, headers={"X-AI-Subject": "human:alice"}).status_code == 401
    for claims in ({"client_id": "service"}, {"act": {"sub": "agent:other"}}, {"azp": "host", "act": {"sub": "app:host"}}, {"act": {}}):
        assert client.get(ROOT, headers=who(idp, **claims)).status_code == 403
    from dataclasses import replace

    from runtime.api import auth

    with monkeypatch.context() as patch:
        patch.setattr(auth, "settings", replace(settings, auth_required=False))
        assert client.get(ROOT, headers={"X-AI-Subject": "human:alice"}).status_code == 403
    with monkeypatch.context() as patch:
        patch.setattr(discovery, "settings", replace(settings, runtime_env="prod"))
        assert client.get(ROOT, headers=who(idp)).status_code == 503
    monkeypatch.setattr(app.state, "isolated_discovery", None)
    assert client.get(ROOT, headers=who(idp)).status_code == 503


def test_withdrawn_rules_and_changed_evidence_fail_closed(lab, idp, monkeypatch):
    from runtime.main import app

    client, snapshot = lab
    visible = seed(client)
    snapshot["rules"] = [rule(visible)]
    assert len(client.get(ROOT, headers=who(idp)).json()["items"]) == 1
    snapshot.update(revision=2, rules=[])
    assert client.get(ROOT, headers=who(idp)).json()["items"] == []
    calls = 0

    async def changed(tenant):
        nonlocal calls
        calls += 1
        return {"tenant": tenant, "revision": calls, "rules": [rule(visible)]}

    monkeypatch.setattr(app.state, "isolated_visibility_reader", changed)
    response = client.get(ROOT, headers=who(idp))
    assert response.status_code == 503
    assert visible not in response.text

    async def broken(tenant):
        raise RuntimeError("private provider diagnostic")

    monkeypatch.setattr(app.state, "isolated_visibility_reader", broken)
    assert "private provider" not in client.get(ROOT, headers=who(idp)).text
    assert client.get(ROOT, headers=who(idp)).status_code == 503


def test_rollout_and_published_change_do_not_grant_access(lab, idp, monkeypatch):
    client, snapshot = lab
    listed = seed(client, percent=0, allowlist=["human:bob"])
    snapshot["rules"] = [rule(listed)]
    assert client.get(ROOT, headers=who(idp)).json()["items"] == []
    visible = seed(client)
    snapshot["rules"] = [rule(visible)]
    original = discovery.candidates
    calls = 0

    async def changed(tenant):
        nonlocal calls
        calls += 1
        return await original(tenant) if calls == 1 else []

    monkeypatch.setattr(discovery, "candidates", changed)
    assert client.get(ROOT, headers=who(idp)).status_code == 503


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"tenant": "foreign", "revision": 1, "rules": []},
        {"tenant": "default", "revision": 1, "rules": [rule("duplicate"), rule("duplicate")]},
    ],
)
def test_invalid_visibility_never_returns_partial_data(lab, idp, monkeypatch, value):
    from runtime.main import app

    client, _ = lab

    async def read(tenant):
        return value

    monkeypatch.setattr(app.state, "isolated_visibility_reader", read)
    assert client.get(ROOT, headers=who(idp)).status_code == 503
