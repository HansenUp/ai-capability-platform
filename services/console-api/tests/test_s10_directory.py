import json

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text

from console_api import db
from console_api.main import app

HEADERS = {"X-Console-Tenant": "directory-a", "X-Console-Role": "TENANT_ADMIN", "X-Console-User": "owner"}
IDENTITY = {"issuer": "https://idp.example/realm", "subject": "human:alice", "display_name": "Alice", "description": "Support"}


def test_directory_lifecycle_persistence_and_no_revocation(c, runtime):
    headers = HEADERS | {"X-Console-Tenant": "directory-lifecycle"}
    assert c.get("/directory/tenant", headers=headers).json()["metadata"] is None
    assert c.put("/directory/tenant", headers=headers, json={"display_name": "Local tenant"}).status_code == 200
    assert c.put("/directory/tenant", headers=headers, json={"display_name": "Renamed"}).status_code == 200
    assert c.post("/agents", headers=headers, json={"agent_id": "directory-agent", "version": "1"}).status_code == 201
    assert c.put("/agents/directory-agent/admins", headers=headers, json={"users": ["human:alice"]}).status_code == 200
    before = len(runtime.calls)
    created = c.post("/directory/users", headers=headers, json=IDENTITY)
    assert created.status_code == 201
    row_id = created.json()["id"]
    assert c.post("/directory/users", headers=headers, json=IDENTITY).status_code == 409
    assert c.put(f"/directory/users/{row_id}", headers=headers, json={"display_name": "Alice updated"}).status_code == 200
    with TestClient(app) as restarted:
        assert restarted.get("/directory/tenant", headers=headers).json()["metadata"]["display_name"] == "Renamed"
        row = restarted.get("/directory/users", headers=headers).json()["items"][0]
        assert row["subject"] == IDENTITY["subject"] and row["display_name"] == "Alice updated"
    assert c.delete(f"/directory/users/{row_id}", headers=headers).status_code == 204
    assert c.delete(f"/directory/users/{row_id}", headers=headers).status_code == 404
    assert c.get("/directory/users", headers=headers).json()["items"] == []
    alice = headers | {"X-Console-Role": "ADMIN", "X-Console-User": "human:alice"}
    assert c.get("/agents/directory-agent/eval-gate", headers=alice).status_code == 200
    assert c.get("/agents/directory-agent/admins", headers=headers).json()["users"] == ["human:alice"]
    assert len(runtime.calls) == before
    audit = c.get("/audit/search?action=directory.user.delete", headers=headers).json()["items"]
    assert len(audit) == 1 and audit[0]["detail"]["authorization_changed"] is False


def test_directory_isolation_roles_and_pagination(c):
    other = HEADERS | {"X-Console-Tenant": "directory-b"}
    rows = [c.post("/directory/users", headers=HEADERS, json=IDENTITY | {"subject": f"user-{index}"}).json() for index in range(3)]
    assert c.post("/directory/users", headers=other, json=IDENTITY | {"subject": "user-0"}).status_code == 201
    row_id = rows[0]["id"]
    assert c.put(f"/directory/users/{row_id}", headers=other, json={"display_name": "Intruder"}).status_code == 404
    assert c.delete(f"/directory/users/{row_id}", headers=other).status_code == 404
    first = c.get("/directory/users?limit=2&tenant=directory-b", headers=HEADERS).json()
    second = c.get(f"/directory/users?limit=2&after_id={first['next_after_id']}", headers=HEADERS).json()
    assert [row["id"] for row in first["items"] + second["items"]] == [row["id"] for row in rows]
    assert second["next_after_id"] is None
    for role in ("USER", "ADMIN"):
        headers = HEADERS | {"X-Console-Role": role}
        for path in ("/directory/users", "/directory/tenant"):
            assert c.get(path, headers=headers).status_code == 403
        assert c.post("/directory/users", headers=headers, json=IDENTITY).status_code == 403
        assert c.put("/directory/tenant", headers=headers, json={"display_name": "No"}).status_code == 403
        assert c.put(f"/directory/users/{row_id}", headers=headers, json={"display_name": "No"}).status_code == 403
        assert c.delete(f"/directory/users/{row_id}", headers=headers).status_code == 403
    assert c.get("/directory/users", headers=HEADERS | {"X-Console-Role": "PLATFORM_ADMIN"}).status_code == 200


def test_directory_validation_no_identity_rebinding_or_role_grants(c):
    headers = HEADERS | {"X-Console-Tenant": "directory-validation"}
    for change in (
        {"role": "PLATFORM_ADMIN"},
        {"tenant_id": "other"},
        {"enabled": False},
        {"issuer": ""},
        {"subject": "bad\nsubject"},
        {"display_name": " "},
        {"subject": 123},
        {"description": "x" * 501},
        {"subject": "invisible\u202eidentity"},
        {"display_name": "surrogate\ud800"},
    ):
        response = c.post(
            "/directory/users",
            headers=headers | {"Content-Type": "application/json"},
            content=json.dumps(IDENTITY | change, ensure_ascii=True),
        )
        assert response.status_code == 422
    row = c.post("/directory/users", headers=headers, json=IDENTITY).json()
    for change in ({"issuer": "other"}, {"subject": "new"}, {"role": "USER"}, {"tenant_id": "other"}):
        assert c.put(f"/directory/users/{row['id']}", headers=headers, json={"display_name": "Changed"} | change).status_code == 422
    for query in ("limit=0", "limit=1001", "after_id=-1", f"after_id={2**63}"):
        assert c.get(f"/directory/users?{query}", headers=headers).status_code == 422
    user = headers | {"X-Console-User": IDENTITY["subject"], "X-Console-Role": "USER"}
    assert c.get("/agents", headers=user).status_code == 403
    assert c.get("/directory/users", headers=headers).json()["items"][0]["display_name"] == "Alice"


def test_directory_preserves_case_sensitive_external_identity(c):
    headers = HEADERS | {"X-Console-Tenant": "directory-case"}
    for subject in ("Alice", "alice"):
        assert c.post("/directory/users", headers=headers, json=IDENTITY | {"subject": subject}).status_code == 201
    assert {row["subject"] for row in c.get("/directory/users", headers=headers).json()["items"]} == {"Alice", "alice"}


@pytest.mark.asyncio
async def test_directory_upgrade_preserves_existing_agents(tmp_path):
    engine = db.make_engine(f"sqlite+aiosqlite:///{tmp_path}/upgrade.db")

    def upgrade_previous(connection):
        config = db.alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0005")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade_previous)
            await connection.execute(
                text(
                    "INSERT INTO t_console_agent (tenant_id, agent_id, created_by, created_at) "
                    "VALUES ('existing', 'preserved', 'owner', '2026-10-19 00:00:00')"
                )
            )
            await connection.run_sync(db._upgrade_head)
            assert (await connection.execute(text("SELECT agent_id FROM t_console_agent"))).scalar_one() == "preserved"
            assert (await connection.execute(text("SELECT version_num FROM alembic_version_console"))).scalar_one() == "0006"
            assert (await connection.execute(text("SELECT COUNT(*) FROM t_console_directory_user"))).scalar_one() == 0
    finally:
        await engine.dispose()
