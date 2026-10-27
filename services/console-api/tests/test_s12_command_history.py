from uuid import uuid4

import pytest


def test_history_pagination_filters_permissions_and_no_execution(c, runtime):
    tenant = f"s12-history-{uuid4().hex[:10]}"
    headers = {"X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": tenant, "X-Console-User": "history-ops"}
    node = "simulated-history"
    ids = []
    for kind in ["reload_config", "reload_skills", "legacy", "reload_config", "switch_model"]:
        response = c.post(f"/nodes/{node}/commands", headers=headers, json={"type": kind, "args": {"private": "not-in-history"}})
        assert response.status_code == 202
        ids.append(response.json()["id"])
    other = headers | {"X-Console-Tenant": "foreign-history"}
    c.post(f"/nodes/{node}/commands", headers=other, json={"type": "reload_config"})
    path = f"/nodes/{node}/command-history"
    calls = len(runtime.calls)
    audits = c.get("/metrics/operations", headers=headers).json()["audit_events"]
    found, cursor = [], None
    while True:
        params = {"limit": 2, "tenant_id": "foreign-history"}
        if cursor is not None:
            params["before_id"] = cursor
        response = c.get(path, headers=headers, params=params)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        page = response.json()
        assert page["tenant_id"] == tenant and page["node_id"] == node and page["snapshot_guaranteed"] is False
        assert page["order"] == "newest_first"
        for row in page["items"]:
            assert "args" not in row and "result" not in row
            assert row["tenant_id"] == tenant and row["created_by"] == "history-ops"
        found.extend(row["id"] for row in page["items"])
        next_cursor = page["next_before_id"]
        assert next_cursor is None or cursor is None or next_cursor < cursor
        cursor = next_cursor
        if cursor is None:
            break
    assert found == list(reversed(ids))
    assert [row["id"] for row in c.get(path, headers=headers, params={"type": "reload_config"}).json()["items"]] == [ids[3], ids[0]]
    assert c.get(path, headers=headers, params={"status": "ok"}).json()["items"] == []
    assert c.get(path, headers=headers | {"X-Console-Tenant": "empty-history"}).json()["items"] == []
    for role in ["USER", "ADMIN"]:
        assert c.get(path, headers=headers | {"X-Console-Role": role}).status_code == 403
    assert len(runtime.calls) == calls
    assert c.get("/metrics/operations", headers=headers).json()["audit_events"] == audits


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"before_id": 0}, {"before_id": 2**63}, {"status": "bad"}, {"type": ""}])
def test_history_rejects_invalid_boundaries(c, params):
    assert c.get("/nodes/synthetic/command-history", headers={"X-Console-Role": "TENANT_ADMIN"}, params=params).status_code == 422


def test_history_keyset_ignores_newer_insert_between_pages(c, runtime):
    node = "simulated-keyset-" + uuid4().hex
    headers = {"X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": "keyset-tenant"}
    path = f"/nodes/{node}"
    original = [c.post(path + "/commands", headers=headers, json={"type": "reload_config"}).json()["id"] for _ in range(3)]
    first = c.get(path + "/command-history", headers=headers, params={"limit": 1}).json()
    inserted = c.post(path + "/commands", headers=headers, json={"type": "reload_config"}).json()["id"]
    calls = len(runtime.calls)
    found = [first["items"][0]["id"]]
    cursor = first["next_before_id"]
    while cursor is not None:
        page = c.get(path + "/command-history", headers=headers, params={"limit": 1, "before_id": cursor}).json()
        found.extend(row["id"] for row in page["items"])
        assert page["next_before_id"] is None or page["next_before_id"] < cursor
        assert page["snapshot_guaranteed"] is False
        cursor = page["next_before_id"]
    assert found == original[::-1] and inserted not in found
    assert len(runtime.calls) == calls


def test_lost_submission_response_is_located_via_existing_audit_without_resending(c, runtime):
    headers = {"X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": f"lost-{uuid4().hex[:10]}", "X-Console-User": "original-operator"}
    node = "simulated-lost-response"
    calls = len(runtime.calls)
    assert (
        c.post(f"/nodes/{node}/commands", headers=headers, json={"type": "reload_config", "args": {"poll_interval_s": 5}}).status_code
        == 202
    )
    candidates = c.get(
        "/audit/search", headers=headers, params={"action": "node.command", "object": node, "actor": "original-operator"}
    ).json()["items"]
    assert len(candidates) == 1
    command_id = candidates[0]["detail"]["id"]
    path = f"/nodes/{node}/commands/{command_id}"
    assert c.get(path, headers=headers).json()["status"] == "pending"
    token = {"Authorization": "Bearer test-node-token"}
    assert c.post(path + "/ack", headers=token, json={"status": "ok", "detail": {"simulation": True, "executed": False}}).status_code == 200
    assert c.get(path, headers=headers).json()["result"] == {"simulation": True, "executed": False}
    history = c.get(f"/nodes/{node}/command-history", headers=headers).json()["items"]
    assert len(history) == 1 and history[0]["id"] == command_id and history[0]["status"] == "ok"
    assert len(runtime.calls) == calls
