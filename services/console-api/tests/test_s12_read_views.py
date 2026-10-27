from uuid import uuid4


def test_s12_control_metrics_and_receipt_reads_are_tenant_scoped_and_do_not_execute(c, runtime):
    tenant = f"s12-{uuid4().hex[:12]}"
    admin = {"X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": tenant, "X-Console-User": "s12-reader"}
    node_id = f"synthetic-{tenant}"
    node = {"Authorization": "Bearer test-node-token"}
    assert c.post("/nodes/register", headers=node, json={"node_id": node_id}).status_code == 200
    response = c.post(f"/nodes/{node_id}/commands", headers=admin, json={"type": "reload_config", "args": {}})
    assert response.status_code == 202
    command_id = response.json()["id"]
    path = f"/nodes/{node_id}/commands/{command_id}"
    before = c.get("/metrics/operations", headers=admin).json()
    runtime_calls = len(runtime.calls)
    for _ in range(2):
        receipt = c.get(path, headers=admin).json()
        assert receipt["status"] == "pending" and receipt["acked_at"] is None
        assert receipt["node_id"] == node_id and receipt["tenant_id"] == tenant
        metrics = c.get("/metrics/operations", headers=admin).json()
        assert metrics["commands"] == {"pending": 1}
        assert metrics["runtime_execution"]["available"] is False
        assert metrics["audit_events"] == before["audit_events"]
    assert len(runtime.calls) == runtime_calls
    assert c.get(path, headers=admin | {"X-Console-Tenant": "foreign"}).status_code == 404
    assert c.get(path, headers=admin | {"X-Console-Role": "USER"}).status_code == 403
    assert c.get(f"/nodes/{node_id}/commands", headers=admin).status_code == 401
    assert c.get(path + "/receipt", headers=admin).status_code == 401
    assert c.get(f"/nodes/{node_id}/commands", headers=node).json()[0]["id"] == command_id
    ack = c.post(path + "/ack", headers=node, json={"status": "ok", "detail": {"fixture": True}})
    assert ack.status_code == 200
    receipt = c.get(path, headers=admin).json()
    assert receipt["status"] == "ok" and receipt["acked_at"] is not None
    assert c.get("/metrics/operations", headers=admin).json()["commands"] == {"ok": 1}
