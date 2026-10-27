import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from console_api.db import Node, NodeBinding, SessionLocal
from console_api.main import app

NODE_TOKEN = {"Authorization": "Bearer test-node-token"}
ADMIN = {"X-Console-Role": "PLATFORM_ADMIN", "X-Console-User": "platform"}
TENANT = {"X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": "s10"}


def test_routing_snapshot_identifies_authorized_tenant_not_query_parameter(c):
    for tenant in ("s10-cache-a", "s10-cache-b"):
        headers = TENANT | {"X-Console-Tenant": tenant}
        response = c.get("/routing?tenant_id=untrusted&tenant=untrusted", headers=headers)
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["tenant_id"] == tenant
        assert snapshot["default_group"] == "general"
        assert isinstance(snapshot["generated_at"], (int, float))
    assert c.get("/routing", headers={"X-Console-Role": "USER"}).status_code == 403


def test_nodes_groups_and_bindings_survive_new_app_lifespan(c):
    assert c.post("/node-groups", json={"name": "managed", "description": "管理分组"}, headers=ADMIN).status_code == 201
    assert c.post("/nodes/register", json={"node_id": "s10-persistent", "labels": ["initial"]}, headers=NODE_TOKEN).status_code == 200
    assert c.put("/nodes/s10-persistent/groups", json={"labels": ["managed", "managed"]}, headers=TENANT).status_code == 403
    response = c.put("/nodes/s10-persistent/groups", json={"labels": ["managed", "managed"]}, headers=ADMIN)
    assert response.status_code == 200 and response.json()["labels"] == ["managed"]
    assert c.post("/nodes/register", json={"node_id": "s10-persistent", "labels": ["initial"]}, headers=NODE_TOKEN).status_code == 200
    assert c.post("/nodes/s10-persistent/heartbeat", json={"load": 0.5, "qps": 3, "version": "v2"}, headers=NODE_TOKEN).status_code == 200
    assert c.put("/agents/same-name/bindings", json=[{"node_group": "managed", "weight": 60}], headers=TENANT).status_code == 200
    with TestClient(app) as restarted:
        nodes = restarted.get("/nodes", headers=TENANT).json()
        node = next(item for item in nodes if item["node_id"] == "s10-persistent")
        assert node["labels"] == ["managed"] and node["load"] == 0.5 and node["version"] == "v2" and node["healthy"]
        assert "s10-persistent" in restarted.get("/node-groups", headers=TENANT).json()["managed"]
        assert restarted.get("/routing", headers=TENANT).json()["bindings"]["same-name"] == {"managed": 60}
        assert "same-name" not in restarted.get("/routing", headers=TENANT | {"X-Console-Tenant": "s10-other"}).json()["bindings"]


def test_expired_heartbeat_excluded_without_deleting_registry(c):
    assert c.post("/nodes/register", json={"node_id": "s10-expired", "labels": ["expired-only"]}, headers=NODE_TOKEN).status_code == 200

    async def expire():
        async with SessionLocal() as session:
            node = await session.get(Node, "s10-expired")
            node.last_seen = datetime.now(UTC) - timedelta(hours=1)
            await session.commit()

    asyncio.run(expire())
    assert "expired-only" not in c.get("/routing").json()["groups"]
    assert next(item for item in c.get("/nodes", headers=TENANT).json() if item["node_id"] == "s10-expired")["healthy"] is False
    assert c.post("/nodes/s10-expired/heartbeat", json={}, headers=NODE_TOKEN).status_code == 200
    assert "expired-only" in c.get("/routing").json()["groups"]


def test_binding_overwrite_is_tenant_scoped_and_validated(c):
    for tenant, weight in (("s10-a", 10), ("s10-b", 90)):
        headers = TENANT | {"X-Console-Tenant": tenant}
        assert c.put("/agents/shared/bindings", json=[{"node_group": "general", "weight": weight}], headers=headers).status_code == 200
    assert c.put("/agents/shared/bindings", json=[], headers=TENANT | {"X-Console-Tenant": "s10-a"}).status_code == 200
    assert c.get("/routing", headers=TENANT | {"X-Console-Tenant": "s10-b"}).json()["bindings"]["shared"] == {"general": 90}
    assert c.put("/agents/shared/bindings", json=[{"node_group": "g", "weight": -1}], headers=TENANT).status_code == 422
    assert c.put("/agents/shared/bindings", json=[{"node_group": "g"}, {"node_group": "g"}], headers=TENANT).status_code == 422
    assert c.get("/nodes", headers={"X-Console-Role": "USER"}).status_code == 403
    assert c.get("/routing", headers={"X-Console-Role": "USER"}).status_code == 403
    assert c.post("/nodes/unknown/heartbeat", json={}, headers=NODE_TOKEN).status_code == 404
    assert c.put("/nodes/unknown/groups", json={"labels": ["g"]}, headers=ADMIN).status_code == 404

    async def stored():
        async with SessionLocal() as session:
            return (await session.execute(select(NodeBinding).where(NodeBinding.tenant_id == "s10-a"))).scalar_one().weights

    assert asyncio.run(stored()) == {}


def test_concurrent_registration_and_binding_are_atomic(c):
    async def concurrent_requests():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            responses = await asyncio.gather(
                *[client.post("/nodes/register", json={"node_id": "s10-concurrent"}, headers=NODE_TOKEN) for _ in range(8)]
            )
            assert all(response.status_code == 200 for response in responses)
            responses = await asyncio.gather(
                *[
                    client.put("/agents/s10-concurrent/bindings", json=[{"node_group": "general", "weight": 50}], headers=TENANT)
                    for _ in range(8)
                ]
            )
            assert all(response.status_code == 200 for response in responses)
        async with SessionLocal() as session:
            nodes = (await session.execute(select(Node).where(Node.node_id == "s10-concurrent"))).scalars().all()
            bindings = (await session.execute(select(NodeBinding).where(NodeBinding.agent_id == "s10-concurrent"))).scalars().all()
            assert len(nodes) == len(bindings) == 1

    asyncio.run(concurrent_requests())


def test_group_metadata_lifecycle_permissions_and_audit(c):
    assert c.post("/node-groups", json={"name": "s10-empty"}, headers=TENANT).status_code == 403
    assert c.post("/node-groups", json={"name": "s10-empty", "description": "空组"}, headers=ADMIN).status_code == 201
    assert c.post("/node-groups", json={"name": "s10-empty"}, headers=ADMIN).status_code == 409
    assert c.get("/node-groups", headers=TENANT).json()["s10-empty"] == []
    assert c.put("/node-groups/s10-empty", json={"description": "修改说明"}, headers=ADMIN).status_code == 200
    with TestClient(app) as restarted:
        group = next(item for item in restarted.get("/node-groups/details", headers=TENANT).json() if item["name"] == "s10-empty")
        assert group["description"] == "修改说明" and group["nodes"] == []
    assert c.delete("/node-groups/s10-empty", headers=TENANT).status_code == 403
    assert c.delete("/node-groups/s10-empty", headers=ADMIN).status_code == 200
    assert "s10-empty" not in c.get("/node-groups", headers=TENANT).json()
    actions = [item["action"] for item in c.get("/audit", headers=ADMIN).json() if item["object"] == "s10-empty"]
    assert actions == ["node_group.create", "node_group.update", "node_group.delete"]
    assert c.delete("/node-groups/general", headers=ADMIN).status_code == 409
    assert c.get("/node-groups/details", headers={"X-Console-Role": "USER"}).status_code == 403


def test_group_delete_refuses_nodes_and_other_tenant_bindings(c):
    assert c.post("/node-groups", json={"name": "s10-referenced"}, headers=ADMIN).status_code == 201
    assert (
        c.post("/nodes/register", json={"node_id": "s10-reference-node", "labels": ["s10-referenced"]}, headers=NODE_TOKEN).status_code
        == 200
    )
    assert c.delete("/node-groups/s10-referenced", headers=ADMIN).status_code == 409
    assert c.put("/nodes/s10-reference-node/groups", json={"labels": ["general"]}, headers=ADMIN).status_code == 200
    other_tenant = TENANT | {"X-Console-Tenant": "s10-reference-tenant"}
    assert c.put("/agents/group-ref/bindings", json=[{"node_group": "s10-referenced"}], headers=other_tenant).status_code == 200
    assert c.delete("/node-groups/s10-referenced", headers=ADMIN).status_code == 409
    assert c.put("/agents/group-ref/bindings", json=[], headers=other_tenant).status_code == 200
    assert c.delete("/node-groups/s10-referenced", headers=ADMIN).status_code == 200
    assert c.put("/nodes/s10-reference-node/groups", json={"labels": ["s10-referenced"]}, headers=ADMIN).status_code == 409
    assert c.put("/agents/group-ref/bindings", json=[{"node_group": "s10-referenced"}], headers=other_tenant).status_code == 409
    assert c.get("/routing", headers=other_tenant).json()["bindings"]["group-ref"] == {}


def test_group_input_validation(c):
    for name in ("", "  ", "bad/name", "x" * 65):
        assert c.post("/node-groups", json={"name": name}, headers=ADMIN).status_code == 422
    assert c.post("/node-groups", json={"name": "valid", "description": "x" * 501}, headers=ADMIN).status_code == 422
    assert c.put("/node-groups/missing-group", json={"description": "x"}, headers=ADMIN).status_code == 409


def test_group_delete_and_binding_race_cannot_leave_dangling_reference(c):
    assert c.post("/node-groups", json={"name": "s10-race"}, headers=ADMIN).status_code == 201

    async def race():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            deletion, binding = await asyncio.gather(
                client.delete("/node-groups/s10-race", headers=ADMIN),
                client.put("/agents/race-agent/bindings", json=[{"node_group": "s10-race"}], headers=TENANT),
            )
            assert (deletion.status_code, binding.status_code) in ((200, 409), (409, 200))
            groups = (await client.get("/node-groups", headers=TENANT)).json()
            weights = (await client.get("/routing", headers=TENANT)).json()["bindings"].get("race-agent", {})
            assert "s10-race" in groups or "s10-race" not in weights

    asyncio.run(race())


def test_group_migration_backfills_existing_labels_and_bindings():
    from alembic import command
    from sqlalchemy import JSON, DateTime, column, create_engine, insert, table

    from console_api.db import alembic_config

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        config = alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0003")
        nodes = table(
            "t_console_node",
            column("node_id"),
            column("topology"),
            column("labels", JSON),
            column("version"),
            column("last_seen", DateTime(timezone=True)),
            column("load"),
            column("qps"),
        )
        connection.execute(
            insert(nodes).values(
                node_id="legacy", topology="central", labels=["legacy-group"], version="1", last_seen=datetime.now(UTC), load=0, qps=0
            )
        )
        bindings = table("t_console_node_binding", column("tenant_id"), column("agent_id"), column("weights", JSON))
        connection.execute(insert(bindings).values(tenant_id="legacy", agent_id="legacy", weights={"empty-legacy": 100}))
        command.upgrade(config, "head")
        groups = table("t_console_node_group", column("name"))
        assert set(connection.execute(select(groups.c.name)).scalars()) == {"general", "legacy-group", "empty-legacy"}
        command.downgrade(config, "0003")
        assert connection.execute(select(nodes.c.labels)).scalar_one() == ["legacy-group"]
        command.upgrade(config, "head")
    engine.dispose()


def test_draining_readiness_and_restart_authority_are_persisted(c):
    assert c.post("/nodes/register", json={"node_id": "s10-draining", "labels": ["s10-drain-group"]}, headers=NODE_TOKEN).status_code == 200
    assert c.post("/nodes/s10-draining/heartbeat", json={"ready": False}, headers=NODE_TOKEN).status_code == 200
    assert "s10-drain-group" not in c.get("/routing", headers=TENANT).json()["groups"]
    assert c.post("/nodes/s10-draining/commands", json={"type": "graceful_restart"}, headers=TENANT).status_code == 403
    response = c.post("/nodes/s10-draining/commands", json={"type": "graceful_restart"}, headers=ADMIN)
    assert response.status_code == 202
    command_id = response.json()["id"]
    pulled = c.get("/nodes/s10-draining/commands", headers=NODE_TOKEN).json()
    assert pulled[0]["authorization_role"] == "PLATFORM_ADMIN" and pulled[0]["tenant_id"] == "default"
    detail = {"phase": "drained_restart_requested"}
    assert (
        c.post(f"/nodes/s10-draining/commands/{command_id}/ack", json={"status": "ok", "detail": detail}, headers=NODE_TOKEN).status_code
        == 200
    )
    receipt = c.get(f"/nodes/s10-draining/commands/{command_id}/receipt", headers=NODE_TOKEN)
    assert receipt.status_code == 200 and receipt.json()["result"] == detail
    assert c.get(f"/nodes/s10-draining/commands/{command_id}/receipt").status_code == 401
    assert c.post("/nodes/register", json={"node_id": "s10-draining", "ready": True}, headers=NODE_TOKEN).status_code == 200
    assert "s10-drain-group" in c.get("/routing", headers=TENANT).json()["groups"]
