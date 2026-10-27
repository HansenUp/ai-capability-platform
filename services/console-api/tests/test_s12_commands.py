from uuid import uuid4

import pytest


@pytest.mark.parametrize(
    "kind,status", [("reload_config", "ok"), ("reload_skills", "ok"), ("switch_model", "error"), ("graceful_restart", "unsupported")]
)
def test_isolated_enqueue_pull_ack_without_runtime_execution(c, runtime, kind, status):
    tenant = f"s12-command-{uuid4().hex[:10]}"
    admin = {"X-Console-Role": "PLATFORM_ADMIN", "X-Console-Tenant": tenant, "X-Console-User": "local-tester"}
    node = {"Authorization": "Bearer test-node-token"}
    node_id = f"simulated-{tenant}"
    assert c.post("/nodes/register", headers=node, json={"node_id": node_id}).status_code == 200
    calls = len(runtime.calls)
    response = c.post(f"/nodes/{node_id}/commands", headers=admin, json={"type": kind, "args": {"synthetic": True}})
    assert response.status_code == 202
    command_id = response.json()["id"]
    path = f"/nodes/{node_id}/commands/{command_id}"
    queued = c.get(path, headers=admin).json()
    assert queued["status"] == "pending" and queued["tenant_id"] == tenant and queued["authorization_role"] == "PLATFORM_ADMIN"
    pending = c.get(f"/nodes/{node_id}/commands", headers=node).json()
    assert [command["id"] for command in pending] == [command_id]
    assert c.get(path, headers=admin | {"X-Console-Tenant": "foreign"}).status_code == 404
    assert c.post(path + "/ack", headers=admin, json={"status": "ok"}).status_code == 401
    result = c.post(path + "/ack", headers=node, json={"status": status, "detail": {"simulation": True, "executed": False}}).json()
    assert result["status"] == status and result["acked_at"] is not None
    assert c.post(path + "/ack", headers=node, json={"status": "error", "detail": {"changed": True}}).json() == result
    assert c.get(path, headers=admin).json() == result
    assert c.get(f"/nodes/{node_id}/commands", headers=node).json() == []
    assert len(runtime.calls) == calls


def test_restart_forged_args_do_not_bypass_console_role_gate(c):
    admin = {"X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": "s12-denied"}
    response = c.post(
        "/nodes/s12-not-running/commands",
        headers=admin,
        json={"type": "graceful_restart", "args": {"timeout_s": 30, "authorization_role": "PLATFORM_ADMIN"}},
    )
    assert response.status_code == 403
    assert c.get("/metrics/operations", headers=admin).json()["commands"] == {}


def test_concurrent_conflicting_receipts_preserve_first_terminal_result(c, monkeypatch, runtime):
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession

    from console_api import main

    node_id = "simulated-race-" + uuid4().hex
    headers = {"X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": "receipt-race"}
    command_id = c.post(f"/nodes/{node_id}/commands", headers=headers, json={"type": "reload_config"}).json()["id"]
    calls = len(runtime.calls)

    async def exercise():
        original = AsyncSession.execute
        entered = 0
        barrier = asyncio.Event()

        async def interleave(session, statement, *args, **kwargs):
            nonlocal entered
            sql = str(statement)
            if "t_console_node_command" not in sql or entered >= 2:
                return await original(session, statement, *args, **kwargs)
            entered += 1
            result = await original(session, statement, *args, **kwargs) if statement.is_select else None
            if entered == 2:
                barrier.set()
            await barrier.wait()
            return result if statement.is_select else await original(session, statement, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "execute", interleave)
        results = await asyncio.gather(
            main.ack_command(node_id, command_id, main.AckIn(status="ok", detail={"simulation": "first"}), {}),
            main.ack_command(node_id, command_id, main.AckIn(status="error", detail={"simulation": "second"}), {}),
        )
        assert entered == 2
        assert results[0] == results[1]
        return results[0]

    result = c.portal.call(exercise)
    assert result["status"] in ("ok", "error") and result["acked_at"] is not None
    assert c.get(f"/nodes/{node_id}/commands/{command_id}", headers=headers).json() == result
    assert len(runtime.calls) == calls
