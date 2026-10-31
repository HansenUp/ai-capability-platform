"""Signed test identities, disposable SQLite, no services or business effects."""

import uuid

import pytest
import test_s7_run_api as authkit
from test_auth import bearer

from runtime.api import isolated_views as views
from runtime.state import db


@pytest.fixture(name="idp")
def signed_provider(monkeypatch):
    yield from authkit.idp.__wrapped__(monkeypatch)


@pytest.fixture
def lab(client, monkeypatch):
    from runtime.main import app

    if not any(getattr(r, "path", "") == "/v1/isolated-views" for r in app.routes):
        app.include_router(views.router)
    monkeypatch.setitem(app.state._state, "isolated_views", views.ENABLED)
    monkeypatch.setitem(app.state._state, "isolated_view_source", {"view_sha256": "a" * 64, "source_sha256": "b" * 64})

    async def create():
        async with db.engine.begin() as connection:
            await connection.run_sync(views.Base.metadata.create_all)

    client.portal.call(create)
    return client


ROOT = "/v1/isolated-views"


def who(provider, sub, roles=(), tenant="default"):
    return bearer(provider.token({"sub": sub, "tenant": tenant, "realm_access": {"roles": list(roles)}}))


def command(action, target=None, proposal=None):
    return {
        "attempt_id": str(uuid.uuid4()),
        "action": action,
        "registration_id": target["id"] if target else None,
        "expected_revision": target["revision"] if target else 0,
        "proposal": proposal,
    }


def proposal():
    return {"version": "v-" + uuid.uuid4().hex, "columns": ["item", "secret"], "filter_item": True, "limit": 10}


def prepare(client, headers, cmd):
    response = client.post(ROOT + "/prepare", headers=headers, json=cmd)
    assert response.status_code == 200, response.text
    return response.json()


def evidence(row):
    return {"fingerprint": row["fingerprint"], "revision": row["revision"]}


def execute(client, headers, row):
    response = client.post(ROOT + f"/{row['attempt_id']}/execute", headers=headers, json=evidence(row))
    assert response.status_code == 200, response.text
    return response.json()


def ack(client, headers, row):
    response = client.post(ROOT + f"/{row['attempt_id']}/acknowledge", headers=headers, json=evidence(row))
    assert response.status_code == 200, response.text


def act(client, headers, cmd):
    row = execute(client, headers, prepare(client, headers, cmd))
    assert row["status"] == "COMMITTED", row
    ack(client, headers, row)
    return row["evidence"]["snapshot"]


def test_registration_roles_two_approvals_and_owner_publish(lab, idp):
    owner = who(idp, "f4-owner", [views.MAINTAINER, views.REVIEWER])
    a = who(idp, "f4-a", [views.REVIEWER])
    b = who(idp, "f4-b", [views.REVIEWER])
    admin = who(idp, "f4-admin", ["platform-admin"])
    assert lab.get(ROOT, headers=admin).status_code == 403
    assert lab.get(ROOT, headers={"X-AI-Subject": "f4-owner"}).status_code == 401
    row = act(lab, owner, command("draft", proposal=proposal()))
    row = act(lab, owner, command("submit", row))
    assert lab.post(ROOT + "/prepare", headers=owner, json=command("approve", row)).status_code == 403
    assert lab.post(ROOT + "/prepare", headers=owner, json=command("publish", row)).status_code == 409
    row = act(lab, a, command("approve", row))
    assert lab.post(ROOT + "/prepare", headers=a, json=command("approve", row)).status_code == 403
    assert lab.post(ROOT + "/prepare", headers=owner, json=command("publish", row)).status_code == 409
    row = act(lab, b, command("approve", row))
    assert lab.post(ROOT + "/prepare", headers=b, json=command("publish", row)).status_code == 403
    other = who(idp, "f4-other", [views.MAINTAINER])
    assert lab.post(ROOT + "/prepare", headers=other, json=command("publish", row)).status_code == 403
    foreign = who(idp, "f4-owner", [views.MAINTAINER], "foreign")
    assert lab.post(ROOT + "/prepare", headers=foreign, json=command("publish", row)).status_code == 404
    row = act(lab, owner, command("publish", row))
    assert row["state"] == "active" and row["generation"] >= 1
    row = act(lab, owner, command("revoke", row))
    assert row["state"] == "revoked"
    assert lab.post(ROOT + "/prepare", headers=owner, json=command("publish", row)).status_code == 409


def test_shared_lock_recovery_once_and_exact_ack(lab, idp):
    owner = who(idp, "f4-lock", [views.MAINTAINER])
    other = who(idp, "f4-wrong", [views.MAINTAINER])
    cmd = command("draft", proposal=proposal())
    prepared = prepare(lab, owner, cmd)
    assert lab.post(ROOT + "/prepare", headers=owner, json=command("draft", proposal=proposal())).status_code == 409
    assert (
        lab.post("/v1/runs", headers=owner, json={"agent_id": "demo", "input": [{"role": "user", "content": "synthetic"}]}).status_code
        == 409
    )
    assert lab.get(ROOT + "/ledger", headers=owner).json()["active"] == prepared
    assert lab.get(ROOT + "/ledger", headers=other).json()["active"] is None
    assert lab.post(ROOT + f"/{prepared['attempt_id']}/execute", headers=other, json=evidence(prepared)).status_code == 404
    done = execute(lab, owner, prepared)
    assert done["status"] == "COMMITTED"
    assert execute(lab, owner, prepared) == done
    assert lab.post(ROOT + f"/{done['attempt_id']}/acknowledge", headers=owner, json=evidence(prepared)).status_code == 409
    bad = evidence(done) | {"fingerprint": "0" * 64}
    assert lab.post(ROOT + f"/{done['attempt_id']}/acknowledge", headers=owner, json=bad).status_code == 409
    ack(lab, owner, done)
    assert lab.get(ROOT + "/ledger", headers=owner).json()["active"] is None


def test_stale_execution_role_loss_and_withdraw_invalidates_approvals(lab, idp):
    owner = who(idp, "f4-edit", [views.MAINTAINER])
    a = who(idp, "f4-review1", [views.REVIEWER])
    b = who(idp, "f4-review2", [views.REVIEWER])
    row = act(lab, owner, command("draft", proposal=proposal()))
    row = act(lab, owner, command("submit", row))
    stale = prepare(lab, a, command("approve", row))
    row = act(lab, b, command("approve", row))
    rejected = execute(lab, a, stale)
    assert rejected["status"] == "REJECTED_NO_EFFECT"
    ack(lab, a, rejected)
    row = act(lab, owner, command("withdraw", row))
    assert row["reviewers"] == {}
    row = act(lab, owner, command("edit", row, row["proposal"] | {"limit": 1}))
    row = act(lab, owner, command("submit", row))
    pending = prepare(lab, a, command("approve", row))
    lost = who(idp, "f4-review1")
    rejected = execute(lab, lost, pending)
    assert rejected["status"] == "REJECTED_NO_EFFECT"
    ack(lab, lost, rejected)
    assert lab.post(ROOT + "/prepare", headers=owner, json=command("publish", row)).status_code == 409


def test_source_change_rejected_without_effect(lab, idp, monkeypatch):
    from runtime.main import app

    owner = who(idp, "f4-source", [views.MAINTAINER])
    pending = prepare(lab, owner, command("draft", proposal=proposal()))
    monkeypatch.setitem(app.state._state, "isolated_view_source", {"view_sha256": "c" * 64, "source_sha256": "b" * 64})
    rejected = execute(lab, owner, pending)
    assert rejected["status"] == "REJECTED_NO_EFFECT"
    ack(lab, owner, rejected)
    assert not any(r["version"] == pending["command"]["proposal"]["version"] for r in lab.get(ROOT, headers=owner).json()["items"])


def test_transaction_fault_rolls_back_and_other_module_lock(lab, idp, monkeypatch):
    owner = who(idp, "f4-fault", [views.MAINTAINER])
    pending = prepare(lab, owner, command("draft", proposal=proposal()))

    def fail_audit(*args, **kwargs):
        raise RuntimeError("synthetic pre-commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(db, "Audit", fail_audit)
        with pytest.raises(RuntimeError, match="synthetic pre-commit failure"):
            execute(lab, owner, pending)
    assert lab.get(ROOT + "/ledger", headers=owner).json()["active"]["status"] == "PREPARED"
    assert not any(r["version"] == pending["command"]["proposal"]["version"] for r in lab.get(ROOT, headers=owner).json()["items"])
    done = execute(lab, owner, pending)
    assert done["status"] == "COMMITTED"
    ack(lab, owner, done)
    prepared = lab.post(
        "/v1/workbench/create-attempts", headers=owner, json={"attempt_id": str(uuid.uuid4()), "agent_id": "demo", "text": "synthetic"}
    )
    assert prepared.status_code == 200
    assert lab.get(ROOT + "/ledger", headers=owner).json()["blocked_elsewhere"] is True
    assert lab.post(ROOT + "/prepare", headers=owner, json=command("draft", proposal=proposal())).status_code == 409
    row = prepared.json()
    done = lab.post(
        f"/v1/workbench/attempts/{row['attempt_id']}/execute", headers=owner, json=evidence(row) | {"value": "synthetic"}
    ).json()
    assert done["status"] == "COMMITTED"
    assert lab.post(f"/v1/workbench/attempts/{row['attempt_id']}/acknowledge", headers=owner, json=evidence(done)).status_code == 200


def test_disabled_and_tenant_scopes(lab, idp, monkeypatch):
    from runtime.main import app

    owner = who(idp, "f4-isolation", [views.MAINTAINER])
    row = act(lab, owner, command("draft", proposal=proposal()))
    foreign = who(idp, "f4-isolation", [views.MAINTAINER], "foreign")
    assert row["id"] not in [r["id"] for r in lab.get(ROOT, headers=foreign).json()["items"]]
    monkeypatch.setitem(app.state._state, "isolated_views", None)
    assert lab.get(ROOT, headers=owner).status_code == 503
