import asyncio
from datetime import UTC, datetime, timedelta

from console_api.db import Audit, SessionLocal

ADMIN = {"X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": "operations-a", "X-Console-User": "ops-owner"}


def test_audit_search_filters_paginates_and_does_not_change_legacy_shape(c):
    async def seed():
        async with SessionLocal() as session:
            for index in range(5):
                session.add(
                    Audit(
                        tenant_id="operations-a",
                        actor="ops-user",
                        action="ops.test",
                        object=f"object-{index}",
                        ts=datetime(2026, 10, 18, tzinfo=UTC) + timedelta(seconds=index),
                        detail={"index": index},
                    )
                )
            session.add(
                Audit(tenant_id="operations-b", actor="ops-user", action="ops.test", object="other", detail={"secret": "other-tenant"})
            )
            await session.commit()

    asyncio.run(seed())
    seen, cursor = [], None
    while True:
        params = {"limit": 2, "actor": "ops-user", "action": "ops.test", "tenant": "operations-b"}
        if cursor:
            params["before_id"] = cursor
        response = c.get("/audit/search", headers=ADMIN, params=params)
        assert response.status_code == 200
        page = response.json()
        assert page["order"] == "newest_first"
        assert all(row["tenant"] == "operations-a" for row in page["items"])
        seen.extend(row["detail"]["index"] for row in page["items"])
        cursor = page["next_before_id"]
        if cursor is None:
            break
    assert seen == [4, 3, 2, 1, 0]
    filtered = c.get("/audit/search", headers=ADMIN, params={"object": "object-2"}).json()["items"]
    assert len(filtered) == 1 and filtered[0]["object"] == "object-2"
    timed = c.get("/audit/search", headers=ADMIN, params={"since": "2026-10-18T08:00:01+08:00", "until": "2026-10-18T00:00:03Z"}).json()
    assert [row["detail"]["index"] for row in timed["items"]] == [3, 2, 1]
    assert isinstance(c.get("/audit", headers=ADMIN).json(), list)


def test_audit_search_validates_boundaries_and_preserves_role_contract(c):
    for params in (
        {"limit": 0},
        {"limit": 1001},
        {"before_id": 0},
        {"before_id": 2**63},
        {"actor": ""},
        {"since": "2026-10-18T00:00:00"},
        {"since": "2026-10-19T00:00:00Z", "until": "2026-10-18T00:00:00Z"},
    ):
        assert c.get("/audit/search", headers=ADMIN, params=params).status_code == 422
    assert c.get("/audit/search", headers=ADMIN | {"X-Console-Role": "USER"}).status_code == 403
    assert c.get("/audit/search", headers=ADMIN | {"X-Console-Role": "ADMIN"}).status_code == 200
    assert c.get("/audit/search", headers=ADMIN, params={"actor": "' OR 1=1 --"}).json()["items"] == []


def test_operations_metrics_are_tenant_control_plane_not_runtime_measurements(c):
    for version in ("1", "2"):
        assert c.post("/agents", headers=ADMIN, json={"agent_id": "ops-agent", "version": version}).status_code == 201
    assert (
        c.post(
            "/agents/ops-agent/versions/1/publish", headers=ADMIN, json={"canary_percent": 0, "canary_allowlist": ["human:evaluator"]}
        ).status_code
        == 200
    )
    assert c.put("/agents/ops-agent/admins", headers=ADMIN, json={"users": ["one", "two"]}).status_code == 200
    metrics = c.get("/metrics/operations?tenant=operations-b", headers=ADMIN).json()
    assert metrics["tenant_id"] == "operations-a" and metrics["scope"] == "console_control_plane"
    assert metrics["agents"] == 1 and metrics["versions"] == {"draft": 1, "published": 1}
    assert metrics["pre_release_versions"] == 1
    assert metrics["assignment_relations"] == metrics["assigned_principals"] == 2
    assert metrics["runtime_execution"] == {"available": False, "reason": "not_collected_by_console"}
    empty = c.get("/metrics/operations", headers=ADMIN | {"X-Console-Tenant": "empty-operations"}).json()
    assert empty["agents"] == empty["assigned_principals"] == empty["audit_events"] == 0
    assert empty["versions"] == empty["commands"] == {}
    for role in ("USER", "ADMIN"):
        assert c.get("/metrics/operations", headers=ADMIN | {"X-Console-Role": role}).status_code == 403
