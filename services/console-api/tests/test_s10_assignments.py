from fastapi.testclient import TestClient

from console_api.main import app

TENANT = {"X-Console-Tenant": "assignment-a", "X-Console-Role": "TENANT_ADMIN", "X-Console-User": "owner"}


def test_assignment_replace_revoke_and_restart_persistence(c):
    assert c.post("/agents", headers=TENANT, json={"agent_id": "assigned-agent", "version": "1"}).status_code == 201
    response = c.put("/agents/assigned-agent/admins", headers=TENANT, json={"users": ["bob", "alice", "bob"]})
    assert response.status_code == 200 and response.json()["users"] == ["alice", "bob"]
    alice = TENANT | {"X-Console-Role": "ADMIN", "X-Console-User": "alice"}
    bob = TENANT | {"X-Console-Role": "ADMIN", "X-Console-User": "bob"}
    with TestClient(app) as restarted:
        assert restarted.get("/agents/assigned-agent/admins", headers=TENANT).json()["users"] == ["alice", "bob"]
        assert any(agent["agent_id"] == "assigned-agent" for agent in restarted.get("/agents", headers=alice).json())
        assert restarted.get("/agents/assigned-agent/eval-gate", headers=alice).status_code == 200
        assert restarted.put("/agents/assigned-agent/admins", headers=bob, json={"users": ["bob"]}).status_code == 403
    assert c.put("/agents/assigned-agent/admins", headers=TENANT, json={"users": []}).status_code == 200
    assert c.get("/agents", headers=alice).json() == []
    assert c.get("/agents/assigned-agent/eval-gate", headers=alice).status_code == 403
    audit = next(row for row in reversed(c.get("/audit", headers=TENANT).json()) if row["action"] == "agent.assignment")
    assert audit["detail"] == {"users": []}


def test_assignments_do_not_cross_tenants_or_grant_roles(c):
    other = TENANT | {"X-Console-Tenant": "assignment-b"}
    assert c.post("/agents", headers=other, json={"agent_id": "assigned-agent", "version": "1"}).status_code == 201
    assert c.put("/agents/assigned-agent/admins", headers=other, json={"users": ["other-user"]}).status_code == 200
    assert c.get("/agents/assigned-agent/admins", headers=other).json()["users"] == ["other-user"]
    assert c.get("/agents/assigned-agent/admins", headers=TENANT).json()["users"] == []
    user = TENANT | {"X-Console-Role": "USER", "X-Console-User": "other-user"}
    assert c.get("/agents", headers=user).status_code == 403
    assert c.put("/agents/assigned-agent/admins", headers=user, json={"users": ["other-user"]}).status_code == 403
    assert c.get("/agents/assigned-agent/admins", headers=user).status_code == 403
    assert c.put("/agents/missing/admins", headers=TENANT, json={"users": []}).status_code == 404
    assert c.get("/agents/missing/admins", headers=TENANT).status_code == 404


def test_assignment_validation_preserves_previous_state(c):
    path = "/agents/assigned-agent/admins"
    assert c.put(path, headers=TENANT, json={"users": ["safe"]}).status_code == 200
    for users in ([""], [" leading"], ["trailing "], ["new\nline"], ["null\x00byte"], [1], ["x" * 129], ["u"] * 1001):
        assert c.put(path, headers=TENANT, json={"users": users}).status_code == 422
    assert c.put(path, headers=TENANT, json={"users": ["safe"], "role": "PLATFORM_ADMIN"}).status_code == 422
    assert c.get(path, headers=TENANT).json()["users"] == ["safe"]
