from dataclasses import replace
from uuid import uuid4

import pytest
from test_s12_owner_metrics import owner as make_owner

from console_api import owner_auth
from console_api.isolated_alerts import Store


@pytest.fixture
def owner(c, monkeypatch):
    return make_owner.__wrapped__(c, monkeypatch)


@pytest.fixture
def alerts(c, owner):
    store = Store()
    c.app.state.isolated_alert_store = store
    store.observe(owner["tenant"], "incident", "synthetic-1", 1, "abnormal")
    yield store, owner["headers"](realm_access={"roles": ["tenant-admin"]})
    del c.app.state.isolated_alert_store
    store.close()


def action(revision, name="claim", **kwargs):
    return {"operation_id": str(uuid4()), "revision": revision, "action": name, **kwargs}


ROOT = "/isolated-alerts"


def test_flow_and_separated_signal(c, alerts):
    store, headers = alerts
    tenant = c.get(ROOT, headers=headers).json()["tenant_id"]
    claimed = action(1)
    assert c.post(ROOT + "/incident/actions", headers=headers, json=claimed).status_code == 200
    handled = action(2, "handle", reason="investigated", evidence="synthetic-1")
    assert c.post(ROOT + "/incident/actions", headers=headers, json=handled).status_code == 200
    row = c.get(ROOT + "/incident", headers=headers).json()
    assert (row["signal"], row["handling"], len(row["history"])) == ("abnormal", "handled", 2)
    for revision, outcome in enumerate(["simulated_received", "failed", "unknown"], 3):
        store.notification_result = outcome
        assert c.post(ROOT + "/incident/actions", headers=headers, json=action(revision, "notify")).status_code == 200
    row = c.get(ROOT + "/incident", headers=headers).json()
    assert [n["status"] for n in row["notifications"]] == ["simulated_received", "failed", "unknown"]
    store.observe(tenant, "incident", "synthetic-2", 2, "unknown")
    assert c.get(ROOT + "/incident", headers=headers).json()["signal"] == "unknown"
    store.observe(tenant, "incident", "synthetic-3", 3, "recovered")
    with pytest.raises(ValueError):
        store.observe(tenant, "incident", "synthetic-4", 4, "abnormal")
    store.observe(tenant, "new-cycle", "synthetic-4", 4, "abnormal")
    assert c.get(ROOT + "/new-cycle", headers=headers).json()["handling"] == "unclaimed"


def test_repeat_conflict_and_receipt_are_read_only(c, alerts):
    _, headers = alerts
    body = action(1)
    result = c.post(ROOT + "/incident/actions", headers=headers, json=body)
    assert result.status_code == 200
    assert c.post(ROOT + "/incident/actions", headers=headers, json=body).status_code == 409
    assert c.get(ROOT + "/operations/" + body["operation_id"], headers=headers).json() == result.json()
    assert len(c.get(ROOT + "/incident", headers=headers).json()["history"]) == 1
    assert c.get(ROOT + "/operations/" + str(uuid4()), headers=headers).status_code == 404


def test_conflicting_admin_cannot_claim_handle_or_read_receipt(c, alerts, owner):
    _, alice = alerts
    bob = owner["headers"](sub="oidc-bob", realm_access={"roles": ["tenant-admin"]})
    body = action(1)
    assert c.post(ROOT + "/incident/actions", headers=alice, json=body).status_code == 200
    assert c.post(ROOT + "/incident/actions", headers=bob, json=action(1)).status_code == 409
    assert (
        c.post(ROOT + "/incident/actions", headers=bob, json=action(2, "handle", reason="investigated", evidence="synthetic-1")).status_code
        == 403
    )
    assert c.get(ROOT + "/operations/" + body["operation_id"], headers=bob).status_code == 404


@pytest.mark.parametrize("mode", ["missing", "owner_only", "headers", "service", "expired", "tenant", "production", "revoked"])
def test_fail_closed(c, alerts, owner, monkeypatch, mode):
    _, headers = alerts
    if mode == "missing":
        headers = {}
    if mode == "owner_only":
        headers = owner["headers"]()
    if mode == "headers":
        headers |= {"X-Console-Role": "TENANT_ADMIN"}
    if mode == "service":
        headers = owner["headers"](identity_type="service", realm_access={"roles": ["tenant-admin"]})
    if mode == "expired":
        headers = owner["headers"](exp=1, realm_access={"roles": ["tenant-admin"]})
    if mode == "tenant":
        headers = owner["headers"](tenant="foreign", realm_access={"roles": ["tenant-admin"]})
    if mode == "production":
        monkeypatch.setenv("CONSOLE_ENV", "prod")
    if mode == "revoked":
        monkeypatch.setattr(owner_auth, "_profile", replace(owner["profile"], bindings=()))
    assert c.get(ROOT, headers=headers).status_code in {400, 401, 403, 503}
    assert c.post(ROOT + "/incident/actions", headers=headers, json=action(1)).status_code in {400, 401, 403, 503}


def test_seed_dedup_and_evidence_required(c, alerts, owner):
    store, headers = alerts
    store.observe(owner["tenant"], "incident", "synthetic-1", 1, "abnormal")
    with pytest.raises(ValueError):
        store.observe(owner["tenant"], "incident", "changed", 1, "abnormal")
    assert len(c.get(ROOT, headers=headers).json()["items"]) == 1
    assert c.post(ROOT + "/incident/actions", headers=headers, json=action(1)).status_code == 200
    for evidence in (None, "stale"):
        assert (
            c.post(
                ROOT + "/incident/actions", headers=headers, json=action(2, "handle", reason="investigated", evidence=evidence)
            ).status_code
            == 409
        )
    assert c.get(ROOT + "/incident", headers=headers).json()["revision"] == 2


def test_tenant_no_leak(c, alerts):
    store, headers = alerts
    store.observe("foreign", "foreign-only", "synthetic-1", 1, "abnormal")
    assert len(c.get(ROOT, headers=headers).json()["items"]) == 1
    assert c.get(ROOT + "/foreign-only", headers=headers).status_code == 404


def test_concurrent_claim_has_one_winner(c, alerts):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, headers = alerts
    barrier = Barrier(2)

    def send():
        body = action(1)
        barrier.wait()
        return c.post(ROOT + "/incident/actions", headers=headers, json=body).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(send), pool.submit(send)
        assert sorted([a.result(), b.result()]) == [200, 409]
    row = c.get(ROOT + "/incident", headers=headers).json()
    assert row["revision"] == 2 and len(row["history"]) == 1


def test_disabled_without_injected_store(c, alerts):
    store, headers = alerts
    c.app.state.isolated_alert_store = None
    try:
        assert c.get(ROOT, headers=headers).status_code == 503
        assert c.post(ROOT + "/incident/actions", headers=headers, json=action(1)).status_code == 503
    finally:
        c.app.state.isolated_alert_store = store
