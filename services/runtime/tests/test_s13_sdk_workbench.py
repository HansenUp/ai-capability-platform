"""SDK against the real ASGI Runtime, temporary SQLite and signed synthetic users."""

from typing import Any
from uuid import uuid4

import httpx
import pytest
from test_s7_run_api import _admin, _log, _publish, _user, _wait
from test_s7_run_api import idp as idp

from ai_platform_sdk.workbench_session import UnknownWorkbenchWrite, WorkbenchSession


@pytest.mark.parametrize("lost", ["create-attempts", "execute", "acknowledge"])
def test_sdk_create_recovery_against_temporary_runtime(client: Any, idp: Any, lost: str) -> None:
    user = _user(idp, "s13-sdk-alice")
    agent = _publish(client, {"tools": []}, _admin(idp))
    paths: list[str] = []

    class LostResponse(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.inner = httpx.ASGITransport(app=client.app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await self.inner.handle_async_request(request)
            await response.aread()
            if request.method == "POST":
                paths.append(request.url.path)
                if request.url.path.endswith(lost):
                    assert response.status_code == 200
                    await response.aclose()
                    raise httpx.ReadError("synthetic response loss after real Runtime commit")
            return response

        async def aclose(self) -> None:
            await self.inner.aclose()

    async def scenario() -> str:
        sdk = WorkbenchSession("http://testserver", user["Authorization"].removeprefix("Bearer "), LostResponse())
        try:
            if lost == "create-attempts":
                with pytest.raises(UnknownWorkbenchWrite):
                    await sdk.prepare_create(agent, "isolated SDK message", str(uuid4()))
                prepared = await sdk.recover()
            else:
                prepared = await sdk.prepare_create(agent, "isolated SDK message", str(uuid4()))
            if lost == "execute":
                with pytest.raises(UnknownWorkbenchWrite):
                    await sdk.execute("isolated SDK message")
                committed = await sdk.recover()
                assert committed.status == "COMMITTED"
                with pytest.raises(ValueError):
                    await sdk.execute("isolated SDK message")
            else:
                committed = await sdk.execute("isolated SDK message")
            assert committed.run_id == prepared.run_id
            if lost == "acknowledge":
                with pytest.raises(UnknownWorkbenchWrite):
                    await sdk.acknowledge()
                assert (await sdk.recover()).acknowledged
                sdk.confirm_closed()
            else:
                await sdk.acknowledge()
            assert sdk.pending is None
            return committed.run_id
        finally:
            await sdk.aclose()

    run_id = client.portal.call(scenario)
    _wait(client, run_id, user, "completed")
    assert len([row for row in _log(client, run_id, user) if row["type"] == "run.created"]) == 1
    assert len(paths) == 3
    assert sum(path.endswith("/execute") for path in paths) == 1


@pytest.mark.auto_resume
@pytest.mark.parametrize("action", ["approve", "reject", "answer", "credential"])
def test_sdk_existing_actions_use_real_runtime_contract(client: Any, idp: Any, monkeypatch: Any, action: str) -> None:
    import json
    import time

    from test_s7_run_api import SCHEMA, _create

    from runtime import credentials
    from runtime.loop import toolbroker
    from runtime.state import db

    executions: list[dict[str, Any]] = []

    async def synthetic(tool: Any, arguments: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return {"impact_preview": {"simulation": True}}
        executions.append(arguments)
        return {"external_effect": False}

    monkeypatch.setattr(toolbroker, "_dev_execute", synthetic)
    alice, bob = _user(idp, "s13-action-alice"), _user(idp, "s13-action-bob")
    if action in {"approve", "reject"}:
        run_id = _create(client, "demo", '!tool ticket.close_batch {"ids":["SYNTHETIC"]}', alice)
        _wait(client, run_id, alice, "auth-required")
        decision = next(row["payload"] for row in _log(client, run_id, alice) if row["type"] == "tool.decision")
        user = bob
    else:
        agent = _publish(client, {"tools": ["echo"], "user_input": True}, _admin(idp))
        run_id = _create(client, agent, "!tool request_user_input " + json.dumps({"prompt": "synthetic ID", "schema": SCHEMA}), alice)
        _wait(client, run_id, alice, "input-required")
        asked = next(row["payload"] for row in _log(client, run_id, alice) if row["type"] == "input.requested")
        user = alice
        if action == "credential":
            expired = _user(idp, "s13-action-alice", exp=int(time.time()) - 120)["Authorization"].removeprefix("Bearer ")

            owner = client.get(f"/v1/runs/{run_id}", headers=alice).json()["subject"]

            async def expire() -> None:
                async with db.SessionLocal() as session:
                    await credentials.store_credential(session, run_id, owner, expired)
                    await session.commit()

            client.portal.call(expire)
            response = client.post(
                f"/v1/runs/{run_id}/input",
                headers=alice,
                json={"kind": "answer", "call_id": asked["call_id"], "value": {"id": "SYNTHETIC"}},
            )
            assert response.status_code == 200
            _wait(client, run_id, alice, "input-required")
            assert [row["payload"]["kind"] for row in _log(client, run_id, alice) if row["type"] == "input.requested"] == [
                "answer",
                "credential",
            ]

    async def scenario() -> None:
        sdk = WorkbenchSession("http://testserver", user["Authorization"].removeprefix("Bearer "), httpx.ASGITransport(app=client.app))
        try:
            attempt_id = str(uuid4())
            if action in {"approve", "reject"}:
                row = await sdk.prepare_approval(run_id, decision["call_id"], decision["params_hash"], action == "approve", attempt_id)
            elif action == "answer":
                row = await sdk.prepare_answer(run_id, asked["call_id"], {"id": "SYNTHETIC"}, attempt_id)
            else:
                row = await sdk.prepare_credential(run_id, attempt_id)
            assert row.action == action and row.status == "PREPARED"
            committed = await sdk.execute({"id": "SYNTHETIC"}) if action == "answer" else await sdk.execute()
            assert committed.status == "COMMITTED" and committed.run_id == run_id
            assert (await sdk.acknowledge()).acknowledged
            assert sdk.pending is None
        finally:
            await sdk.aclose()

    client.portal.call(scenario)
    _wait(client, run_id, alice, "failed" if action == "reject" else "completed")
    history = _log(client, run_id, alice)
    if action in {"approve", "reject"}:
        event = "tool.approved" if action == "approve" else "tool.rejected"
        assert len([row for row in history if row["type"] == event]) == 1
        assert len(executions) == (1 if action == "approve" else 0)
    else:
        assert len([row for row in history if row["type"] == "input.provided" and row["payload"]["kind"] == action]) == 1
