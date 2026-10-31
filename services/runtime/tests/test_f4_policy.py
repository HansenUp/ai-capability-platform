"""Policy lifecycle against disposable SQLite and signed synthetic identities only."""

import uuid

import pytest
import test_f4_registration as registry

from runtime.api import isolated_policies as policies
from runtime.state import db


@pytest.fixture(name="idp")
def signed_provider(monkeypatch):
    yield from registry.signed_provider.__wrapped__(monkeypatch)


@pytest.fixture
def lab(client, monkeypatch):
    registry.lab.__wrapped__(client, monkeypatch)
    from runtime.main import app

    if not any(getattr(r, "path", "") == "/v1/isolated-policies" for r in app.routes):
        app.include_router(policies.router)
    monkeypatch.setitem(app.state._state, "isolated_policies", policies.ENABLED)

    async def create():
        async with db.engine.begin() as connection:
            await connection.run_sync(policies.Base.metadata.create_all)

    client.portal.call(create)
    return client


ROOT = "/v1/isolated-policies"


def publish_manifest(client, provider):
    owner = registry.who(provider, "manifest-owner", [registry.views.MAINTAINER])
    row = registry.act(client, owner, registry.command("draft", proposal=registry.proposal()))
    row = registry.act(client, owner, registry.command("submit", row))
    for sub in ("manifest-review-a", "manifest-review-b"):
        row = registry.act(client, registry.who(provider, sub, [registry.views.REVIEWER]), registry.command("approve", row))
    return registry.act(client, owner, registry.command("publish", row))


def proposal(manifest):
    return {
        "version": "p-" + uuid.uuid4().hex,
        "manifest_id": manifest["id"],
        "recipient": "alice",
        "columns": ["item"],
        "filter_item": True,
        "limit": 2,
    }


def step(client, headers, action, target=None, data=None):
    response = client.post(ROOT + "/prepare", headers=headers, json=registry.command(action, target, data))
    assert response.status_code == 200, response.text
    prepared = response.json()
    response = client.post(ROOT + f"/{prepared['attempt_id']}/execute", headers=headers, json=registry.evidence(prepared))
    assert response.status_code == 200, response.text
    done = response.json()
    assert done["status"] == "COMMITTED", done
    assert client.post(ROOT + f"/{done['attempt_id']}/acknowledge", headers=headers, json=registry.evidence(done)).status_code == 200
    return done["evidence"]["snapshot"]


def actors(idp):
    return [
        registry.who(idp, name, [role])
        for name, role in (("policy-owner", policies.MAINTAINER), ("policy-a", policies.REVIEWER), ("policy-b", policies.REVIEWER))
    ]


def approved(lab, idp, manifest):
    owner, a, b = actors(idp)
    row = step(lab, owner, "draft", data=proposal(manifest))
    row = step(lab, owner, "submit", row)
    row = step(lab, a, "approve", row)
    return step(lab, b, "approve", row)


def test_policy_separation_narrowing_and_lifecycle(lab, idp):
    manifest = publish_manifest(lab, idp)
    owner, a, b = actors(idp)
    for role in ("platform-admin", registry.views.MAINTAINER, registry.views.REVIEWER):
        assert lab.get(ROOT, headers=registry.who(idp, "other", [role])).status_code == 403
    for change in ({"limit": 11}, {"recipient": "mallory"}, {"columns": ["tenant"]}):
        assert lab.post(
            ROOT + "/prepare", headers=owner, json=registry.command("draft", proposal=proposal(manifest) | change)
        ).status_code in (403, 422)
    row = step(lab, owner, "draft", data=proposal(manifest))
    row = step(lab, owner, "submit", row)
    for actor in (
        registry.who(idp, "policy-owner", [policies.MAINTAINER, policies.REVIEWER]),
        registry.who(idp, "alice", [policies.REVIEWER]),
    ):
        assert lab.post(ROOT + "/prepare", headers=actor, json=registry.command("approve", row)).status_code == 403
    assert lab.post(ROOT + "/prepare", headers=owner, json=registry.command("publish", row)).status_code == 409
    row = step(lab, a, "approve", row)
    assert lab.post(ROOT + "/prepare", headers=a, json=registry.command("approve", row)).status_code == 403
    row = step(lab, b, "approve", row)
    assert (
        lab.post(
            ROOT + "/prepare", headers=registry.who(idp, "other", [policies.MAINTAINER]), json=registry.command("publish", row)
        ).status_code
        == 403
    )
    row = step(lab, owner, "publish", row)
    pair = lab.portal.call(policies.current_pair, "default")
    assert pair["policy"]["id"] == row["id"] and pair["manifest"]["id"] == manifest["id"]
    assert pair["policy"]["recipient_binding"]["subject"] == "alice"
    row = step(lab, owner, "revoke", row)
    with pytest.raises(Exception, match="current policy pair unavailable"):
        lab.portal.call(policies.current_pair, "default")
    assert lab.post(ROOT + "/prepare", headers=owner, json=registry.command("publish", row)).status_code == 409
    fresh = step(lab, owner, "publish", approved(lab, idp, manifest))
    assert fresh["generation"] > row["generation"]


def test_manifest_replacement_and_approval_invalidation(lab, idp):
    manifest = publish_manifest(lab, idp)
    owner, a, b = actors(idp)
    row = approved(lab, idp, manifest)
    row = step(lab, owner, "withdraw", row)
    assert row["reviewers"] == {}
    row = step(lab, owner, "edit", row, row["proposal"] | {"recipient": "bob"})
    row = step(lab, owner, "submit", row)
    assert lab.post(ROOT + "/prepare", headers=owner, json=registry.command("publish", row)).status_code == 409
    row = step(lab, a, "approve", row)
    row = step(lab, b, "approve", row)
    prepared = lab.post(ROOT + "/prepare", headers=owner, json=registry.command("publish", row)).json()
    publish_manifest(lab, idp)
    rejected = lab.post(ROOT + f"/{prepared['attempt_id']}/execute", headers=owner, json=registry.evidence(prepared)).json()
    assert rejected["status"] == "REJECTED_NO_EFFECT"
    assert lab.post(ROOT + f"/{rejected['attempt_id']}/acknowledge", headers=owner, json=registry.evidence(rejected)).status_code == 200
    with pytest.raises(Exception, match="current policy pair unavailable"):
        lab.portal.call(policies.current_pair, "default")


def test_policy_shared_lock_atomic_failure_and_role_loss(lab, idp, monkeypatch):
    manifest = publish_manifest(lab, idp)
    owner, a, b = actors(idp)
    cmd = registry.command("draft", proposal=proposal(manifest))
    prepared = lab.post(ROOT + "/prepare", headers=owner, json=cmd).json()
    assert lab.post(ROOT + "/prepare", headers=owner, json=cmd).json() == prepared
    assert lab.get(registry.ROOT + "/ledger", headers=owner).json()["blocked_elsewhere"] is True
    assert lab.post("/v1/runs", headers=owner, json={"agent_id": "demo", "input": [{"role": "user", "content": "test"}]}).status_code == 409

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    with monkeypatch.context() as patch:
        patch.setattr(db, "Audit", fail)
        with pytest.raises(RuntimeError, match="synthetic audit failure"):
            lab.post(ROOT + f"/{prepared['attempt_id']}/execute", headers=owner, json=registry.evidence(prepared))
    assert lab.get(ROOT + "/ledger", headers=owner).json()["active"]["status"] == "PREPARED"
    assert not any(item["version"] == cmd["proposal"]["version"] for item in lab.get(ROOT, headers=owner).json()["items"])
    lost = registry.who(idp, "policy-owner")
    rejected = lab.post(ROOT + f"/{prepared['attempt_id']}/execute", headers=lost, json=registry.evidence(prepared)).json()
    assert rejected["status"] == "REJECTED_NO_EFFECT"
    assert lab.post(ROOT + f"/{prepared['attempt_id']}/execute", headers=lost, json=registry.evidence(prepared)).json() == rejected
    assert lab.post(ROOT + f"/{prepared['attempt_id']}/acknowledge", headers=lost, json=registry.evidence(prepared)).status_code == 409
    assert lab.post(ROOT + f"/{prepared['attempt_id']}/acknowledge", headers=lost, json=registry.evidence(rejected)).status_code == 200
    foreign = registry.who(idp, "policy-owner", [policies.MAINTAINER], "foreign")
    assert lab.get(ROOT, headers=foreign).json()["items"] == []
    assert lab.post(ROOT + f"/{prepared['attempt_id']}/execute", headers=foreign, json=registry.evidence(prepared)).status_code == 404


def test_competing_policy_publish_requires_reapproval_and_replaces(lab, idp):
    manifest = publish_manifest(lab, idp)
    owner, a, b = actors(idp)
    first = approved(lab, idp, manifest)
    second = step(lab, owner, "draft", data=proposal(manifest))
    second = step(lab, owner, "submit", second)
    pending = lab.post(ROOT + "/prepare", headers=owner, json=registry.command("publish", first)).json()
    # Different reviewer can mutate its own scope; stale per-policy revision fails.
    stale = lab.post(ROOT + "/prepare", headers=a, json=registry.command("reject", second)).json()
    second = step(lab, b, "reject", second)
    rejected = lab.post(ROOT + f"/{stale['attempt_id']}/execute", headers=a, json=registry.evidence(stale)).json()
    assert rejected["status"] == "REJECTED_NO_EFFECT"
    assert lab.post(ROOT + f"/{stale['attempt_id']}/acknowledge", headers=a, json=registry.evidence(rejected)).status_code == 200
    done = lab.post(ROOT + f"/{pending['attempt_id']}/execute", headers=owner, json=registry.evidence(pending)).json()
    assert done["status"] == "COMMITTED"
    assert lab.post(ROOT + f"/{done['attempt_id']}/acknowledge", headers=owner, json=registry.evidence(done)).status_code == 200
    second = step(lab, owner, "withdraw", second)
    second = step(lab, owner, "edit", second, second["proposal"] | {"recipient": "bob"})
    second = step(lab, owner, "submit", second)
    second = step(lab, a, "approve", second)
    second = step(lab, b, "approve", second)
    second = step(lab, owner, "publish", second)
    pair = lab.portal.call(policies.current_pair, "default")
    assert pair["policy"]["recipient_binding"]["subject"] == "bob"
    rows = lab.get(ROOT, headers=owner).json()["items"]
    assert next(r for r in rows if r["id"] == first["id"])["state"] == "superseded"


def test_competing_approved_drafts_cannot_silently_overwrite(lab, idp):
    manifest = publish_manifest(lab, idp)
    owner, a, b = actors(idp)
    first = approved(lab, idp, manifest)
    second = approved(lab, idp, manifest)
    first = step(lab, owner, "publish", first)
    assert lab.post(ROOT + "/prepare", headers=owner, json=registry.command("publish", second)).status_code == 409
    pair = lab.portal.call(policies.current_pair, "default")
    assert pair["policy"]["id"] == first["id"]
