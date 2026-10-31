"""Structured synthetic observations; no original DB or forwarding."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import test_f5_discovery as authkit
from sqlalchemy import select

from runtime.api import isolated_observe as observe
from runtime.state import db


@pytest.fixture(name="idp")
def signed_provider(monkeypatch):
    yield from authkit.signed_provider.__wrapped__(monkeypatch)


@pytest.fixture
def lab(client, idp, monkeypatch):
    from runtime.api import auth
    from runtime.main import app

    if not any(getattr(r, "path", "") == observe.PREFIX + "/events" for r in app.routes):
        app.include_router(observe.router)
    tenant = "observe-" + uuid4().hex
    monkeypatch.setitem(app.state._state, "isolated_observe", observe.ENABLED)
    monkeypatch.setitem(
        app.state._state,
        "isolated_observe_bindings",
        {(auth.settings.jwt_issuer, "observe-test", "reporter"): (tenant, "synthetic-agent")},
    )

    async def init():
        async with db.engine.begin() as connection:
            await connection.run_sync(observe.Metadata.metadata.create_all)

    client.portal.call(init)
    service = authkit.who(idp, sub="reporter", client_id="observe-test", tenant=tenant)
    admin = authkit.who(idp, tenant=tenant, realm_access={"roles": ["platform-admin"]})
    return client, tenant, service, admin


def event(**patch):
    return {"event_id": uuid4().hex, "kind": "usage", "payload": {"model": "synthetic", "input_tokens": 3, "output_tokens": 2}, **patch}


ROOT = observe.PREFIX + "/events"


def test_receipt_identity_duplicate_conflict_and_tenant_read(lab, idp):
    client, tenant, service, admin = lab
    body = event()
    first = client.post(ROOT, headers=service, json=body)
    assert first.status_code == 200, first.text
    receipt = first.json()
    assert receipt["trust"] == "unverified" and not receipt["billing_eligible"] and not receipt["forwarded"]
    assert receipt["payload"]["cost"] is None
    assert client.post(ROOT, headers=service, json=body).json() == receipt
    assert (
        client.post(ROOT, headers=service, json={**body, "payload": {**body["payload"], "cost": 0, "currency": "CNY"}}).status_code == 409
    )
    assert client.get(ROOT, headers=admin).json()["items"] == [receipt]
    foreign = authkit.who(idp, tenant="foreign", realm_access={"roles": ["platform-admin"]})
    assert client.get(ROOT, headers=foreign).json()["items"] == []
    assert client.get(ROOT, headers=service).status_code == 403
    assert client.get(ROOT, headers=authkit.who(idp, tenant=tenant)).status_code == 403
    assert client.post(ROOT, headers=admin, json=event()).status_code == 403
    assert (
        client.post(ROOT, headers=authkit.who(idp, sub="other", client_id="observe-test", tenant=tenant), json=event()).status_code == 403
    )
    assert (
        client.post(ROOT, headers=authkit.who(idp, sub="reporter", client_id="observe-test", tenant="foreign"), json=event()).status_code
        == 403
    )
    assert (
        client.post(
            ROOT, headers=authkit.who(idp, sub="reporter", client_id="observe-test", tenant=tenant, act={}), json=event()
        ).status_code
        == 403
    )
    assert client.post(ROOT, headers={"X-AI-Subject": "app:observe-test"}, json=event()).status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"model": "x", "input_tokens": -1, "output_tokens": 0},
        {"model": "x", "input_tokens": True, "output_tokens": 0},
        {"model": "x", "input_tokens": 1.5, "output_tokens": 0},
        {"model": "x", "input_tokens": 1, "output_tokens": 0, "cost": 1},
        {"model": "x", "input_tokens": 1, "output_tokens": 0, "prompt": "secret"},
    ],
)
def test_invalid_usage_has_no_record(lab, payload):
    client, _, service, admin = lab
    assert client.post(ROOT, headers=service, json=event(payload=payload)).status_code == 422
    assert client.get(ROOT, headers=admin).json()["items"] == []


def test_no_self_reported_scope_no_batch_limit_and_zero(lab):
    client, _, service, admin = lab
    assert client.post(ROOT, headers=service, json=event(tenant="foreign")).status_code == 422
    assert client.post(ROOT, headers=service, json=[event()]).status_code == 422
    assert client.post(ROOT, headers=service, json=event(kind="logs")).status_code == 422
    assert client.post(ROOT, headers=service, content=b"x" * 8193).status_code == 413
    zero = event(payload={"model": "x", "input_tokens": 0, "output_tokens": 0, "cost": 0, "currency": "USD"})
    assert client.post(ROOT, headers=service, json=zero).json()["payload"]["cost"] == 0
    status = event(kind="status", payload={"instances": 0, "healthy": False})
    assert client.post(ROOT, headers=service, json=status).status_code == 200
    page = client.get(ROOT, headers=admin, params={"limit": 1}).json()
    assert len(page["items"]) == 1 and page["next_after"] is not None
    last = client.get(ROOT, headers=admin, params={"after": page["next_after"], "limit": 1}).json()
    assert last["items"][0]["kind"] == "status" and last["next_after"] is None


def test_concurrent_duplicate_and_commit_rollback(lab, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    client, _, service, admin = lab
    body = event()
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: client.post(ROOT, headers=service, json=body), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert len(client.get(ROOT, headers=admin).json()["items"]) == 1
    broken = event()

    async def fail(session):
        raise RuntimeError("synthetic commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail)
        with pytest.raises(RuntimeError, match="synthetic commit failure"):
            client.post(ROOT, headers=service, json=broken)

    async def absent():
        async with db.SessionLocal() as session:
            assert await session.scalar(select(observe.Observation.id).where(observe.Observation.event_id == broken["event_id"])) is None

    client.portal.call(absent)
