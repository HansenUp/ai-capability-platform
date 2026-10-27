"""签名写脚本复用审批、写前登记与结果未知对账的集成回归。"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_s9c_helpers import FakeGateway, Signer, build, tgz, write_jwks

from ai_contract import RunEventType
from runtime import gateway_client, sandbox
from runtime.assemble.assembler import assemble
from runtime.loop import runner, user_input
from runtime.skills import execution, store, tools
from runtime.skills.verify import SkillVerifyError
from runtime.state.db import Run, SessionLocal
from runtime.state.eventlog import EventLog, LeaseLost

STOP_OPERATION = execution.stop_operation


@pytest.fixture
def signed_script(tmp_path, monkeypatch, request):
    monkeypatch.setattr(execution, "engine_identity", AsyncMock(return_value="shared-test-engine"))
    monkeypatch.setattr(execution, "stop_operation", AsyncMock())
    signer = Signer()
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("SKILL_TRUST_KEYS_FILE", str(write_jwks(tmp_path / "trust.json", signer)))
    gateway = FakeGateway()
    level = getattr(request, "param", "workspace-write")
    if level == "full-access":
        monkeypatch.setenv("SANDBOX_EGRESS_PROXY", "http://egress.test:3128")
    package, digest = build(signer, sandbox=level, data_class="internal")
    gateway.packages[("demo-skill", "1.0.0")] = tgz(package)
    gateway_client.set_transport_for_tests(gateway.transport())
    store.revocations.reset()
    lock = [{"name": "demo-skill", "version": "1.0.0", "manifest_sha256": digest}]

    async def with_script(agent, subject, credential=None):
        assembled = await assemble(agent, subject, credential)
        assembled.skills = await store.load_locked(lock, "default")
        assembled.tools += tools.platform_tools(assembled.skills, False)
        return assembled

    monkeypatch.setattr(runner, "assemble", with_script)
    yield SimpleNamespace(gateway=gateway, digest=digest, lock=lock, level=level)
    gateway_client.set_transport_for_tests(None)
    store.revocations.reset()


def start(client):
    name = "script-" + uuid.uuid4().hex[:10]
    assert client.post("/admin/agents", json={"agent_id": name, "version": "1", "config": {"tools": []}}).status_code == 201
    assert client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}).status_code == 200
    arguments = {"name": "demo-skill", "script": "scripts/summarize.py", "stdin": "fixed input"}
    response = client.post(
        "/v1/messages",
        json={"agent_id": name, "messages": [{"role": "user", "content": "!tool run_skill_script " + json.dumps(arguments)}]},
    )
    events = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]
    card = next(event["payload"] for event in events if event.get("type") == "card")
    return response.headers["x-run-id"], card


def approve(client, run_id, card):
    response = client.post(f"/v1/runs/{run_id}/approve", json={"call_id": card["call_id"], "params_hash": card["params_hash"]})
    assert response.status_code == 200, response.text


async def log_of(run_id):
    async with SessionLocal() as session:
        return await EventLog(session).list(run_id)


def resume(client, run_id):
    response = client.post(f"/v1/runs/{run_id}/resume")
    assert response.status_code == 200, response.text
    return [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]


@pytest.mark.parametrize("signed_script", ["workspace-write", "full-access"], indirect=True)
async def test_write_script_approval_and_committed_operation_precede_execution(client, signed_script, monkeypatch):
    executed = []

    async def run(script, level, stdin, workspace, **kwargs):
        run_id = Path(workspace).name
        events = await log_of(run_id)
        executing = next(event for event in events if event.type == RunEventType.TOOL_EXECUTING)
        async with SessionLocal() as session:
            operation = await EventLog(session).get_operation(executing.payload["operation_id"])
            assert operation is not None and operation.status == "executing"
        assert any(event.type == RunEventType.TOOL_APPROVED for event in events)
        executed.append((run_id, level, stdin))
        return sandbox.SandboxResult(0, "written", "", False)

    monkeypatch.setattr(sandbox, "run", run)
    run_id, card = start(client)
    assert executed == []
    assert card["preview"]["script_executed"] is False
    assert card["preview"]["scope"]["manifest_sha256"] == signed_script.digest
    assert client.post(f"/v1/runs/{run_id}/approve", json={"call_id": card["call_id"], "params_hash": "wrong"}).status_code == 409
    approve(client, run_id, card)
    resume(client, run_id)
    assert executed == [(run_id, signed_script.level, "fixed input")]
    events = await log_of(run_id)
    requested = next(event for event in events if event.type == RunEventType.TOOL_REQUESTED)
    assert requested.payload["risk_level"] == "write_high"
    assert requested.payload["effective_input"][execution.BINDING]["sandbox_level"] == signed_script.level
    result = next(event for event in events if event.type == RunEventType.TOOL_RESULT)
    assert result.payload["data_class"] == "internal" and result.payload["skill"]["manifest_sha256"] == signed_script.digest
    resume(client, run_id)
    assert len(executed) == 1


@pytest.mark.parametrize("failure", ["exception", "nonzero", "timeout"])
async def test_partial_write_requires_reconciliation_and_never_retry(client, signed_script, monkeypatch, failure):
    executed = []

    async def run(script, level, stdin, workspace, **kwargs):
        executed.append(workspace)
        if failure == "exception":
            raise RuntimeError("connection lost after write")
        return sandbox.SandboxResult(1 if failure == "nonzero" else None, "partially written", "", failure == "timeout")

    monkeypatch.setattr(sandbox, "run", run)
    run_id, card = start(client)
    approve(client, run_id, card)
    events = resume(client, run_id)
    assert any(event["type"] == "run.error" and event["payload"]["code"] == "UNKNOWN_TOOL_RESULT" for event in events)
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "input-required"
    async with SessionLocal() as session:
        stored = await session.get(Run, run_id)
        subject = stored.subject
    with pytest.raises(ValueError, match="never retry"):
        await user_input.provide_input(run_id, subject, "reconcile", card["call_id"], action="retry")
    await user_input.provide_input(run_id, subject, "reconcile", card["call_id"], action="done", value={"verified": True})
    resume(client, run_id)
    assert len(executed) == 1
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "completed"


async def test_sandbox_configuration_change_invalidates_approved_script(client, signed_script, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("changed execution scope must never execute")

    monkeypatch.setattr(sandbox, "run", unexpected)
    run_id, card = start(client)
    approve(client, run_id, card)
    monkeypatch.setenv("SANDBOX_MEMORY", "256m")
    resume(client, run_id)
    events = await log_of(run_id)
    assert not any(event.type == RunEventType.TOOL_EXECUTING for event in events)
    assert any(event.type == RunEventType.TOOL_RESULT and event.payload["output"].get("code") == "SKILL_VERIFY_FAILED" for event in events)


@pytest.mark.parametrize("action", ["done", "skip"])
async def test_worker_crash_after_write_ahead_is_reconciled_without_reexecution(client, signed_script, monkeypatch, action):
    executed = []

    async def crash(script, level, stdin, workspace, **kwargs):
        executed.append(workspace)
        raise asyncio.CancelledError("worker terminated after writing")

    monkeypatch.setattr(sandbox, "run", crash)
    run_id, card = start(client)
    approve(client, run_id, card)
    with pytest.raises(asyncio.CancelledError):
        [event async for event in runner.drive_durable(run_id)]
    before = await log_of(run_id)
    assert any(event.type == RunEventType.TOOL_EXECUTING for event in before)
    assert not any(event.type == RunEventType.TOOL_EXECUTED for event in before)
    [event async for event in runner.drive_durable(run_id)]
    assert len(executed) == 1
    async with SessionLocal() as session:
        stored = await session.get(Run, run_id)
        assert stored.state == "input-required"
        subject = stored.subject
    await user_input.provide_input(run_id, subject, "reconcile", card["call_id"], action=action)
    [event async for event in runner.drive_durable(run_id)]
    after = await log_of(run_id)
    assert len(executed) == 1
    result = next(event for event in after if event.type == RunEventType.TOOL_RESULT)
    assert result.payload["data_class"] == "internal"
    assert result.payload["skill"]["manifest_sha256"] == signed_script.digest

    operation_id = next(event.payload["operation_id"] for event in before if event.type == RunEventType.TOOL_EXECUTING)
    async with SessionLocal() as session:
        log = EventLog(session)
        operation = await log.get_operation(operation_id)
        settled_status, settled_hash = operation.status, operation.result_hash
        assert settled_status == ("executed" if action == "done" else "skipped")
        assert not await log.update_operation(operation_id, "unknown", pending_only=True)
        assert not await log.update_operation(operation_id, "executed", "late-result", pending_only=True)
        assert (operation.status, operation.result_hash) == (settled_status, settled_hash)
        await session.commit()
    async with SessionLocal() as session:
        operation = await EventLog(session).get_operation(operation_id)
        assert (operation.status, operation.result_hash) == (settled_status, settled_hash)


async def test_expired_approval_requires_new_approval_without_running(client, signed_script, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("expired approval must not start a script")

    monkeypatch.setattr(sandbox, "run", unexpected)
    run_id, card = start(client)
    response = client.post(
        f"/v1/runs/{run_id}/approve", json={"call_id": card["call_id"], "params_hash": card["params_hash"], "ttl_seconds": 1}
    )
    assert response.status_code == 200, response.text
    await asyncio.sleep(1.1)
    resume(client, run_id)
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "auth-required"


async def test_reconciliation_refuses_to_settle_when_sandbox_cannot_be_stopped(client, signed_script, monkeypatch):
    async def partial(*args, **kwargs):
        raise RuntimeError("partial write")

    monkeypatch.setattr(sandbox, "run", partial)
    run_id, card = start(client)
    approve(client, run_id, card)
    resume(client, run_id)
    monkeypatch.setattr(execution, "stop_operation", AsyncMock(side_effect=ValueError("sandbox unavailable")))
    async with SessionLocal() as session:
        stored = await session.get(Run, run_id)
        subject = stored.subject
    with pytest.raises(ValueError, match="sandbox unavailable"):
        await user_input.provide_input(run_id, subject, "reconcile", card["call_id"], action="done")
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "input-required"
    events = await log_of(run_id)
    assert not any(event.type == RunEventType.INPUT_PROVIDED for event in events)


async def test_reconciliation_targets_only_original_operation_containers(client, signed_script, monkeypatch):
    from runtime.sandbox import docker

    _, card = start(client)
    command = AsyncMock(side_effect=[(0, b"abc123\ndef456\n", b""), (0, b"", b"")])
    monkeypatch.setattr(docker, "_exec", command)
    await STOP_OPERATION("operation-for-test", card["preview"]["scope"])
    assert command.await_args_list[0].args[1] == ["ps", "-aq", "--filter", "label=ai-platform.operation=operation-for-test"]
    assert command.await_args_list[1].args[1] == ["rm", "-f", "-v", "abc123", "def456"]
    monkeypatch.setattr(execution, "engine_identity", AsyncMock(return_value="other-engine"))
    with pytest.raises(ValueError, match="original sandbox engine"):
        await STOP_OPERATION("operation-for-test", card["preview"]["scope"])
    assert command.await_count == 2


async def test_sandbox_revalidates_before_start_and_cleans_stale_container(monkeypatch):
    from runtime.sandbox import docker
    from runtime.sandbox.settings import load

    command = AsyncMock(side_effect=[(0, b"sandbox-id", b""), (0, b"", b""), (0, b"", b"")])
    monkeypatch.setattr(docker, "_exec", command)
    start_process = AsyncMock()
    monkeypatch.setattr(docker.asyncio, "create_subprocess_exec", start_process)
    validate = AsyncMock(side_effect=LeaseLost("lease changed while injecting the script"))
    with pytest.raises(LeaseLost):
        await docker.run_container(
            ["python", "/skill/run.py"],
            {"run.py": b"print(1)"},
            "read-only",
            b"",
            None,
            load(),
            10,
            operation_id="stale-operation",
            before_start=validate,
        )
    start_process.assert_not_awaited()
    assert command.await_args_list[-1].args[1] == ["rm", "-f", "-v", "sandbox-id"]


async def test_prepare_rechecks_trust_keys_even_when_manifest_was_cached(signed_script, tmp_path):
    skills = await store.load_locked(signed_script.lock, "default")
    context = SimpleNamespace(skills=skills, run_id="trust-rotation-run", workspace=str(tmp_path))
    arguments = {"name": "demo-skill", "script": "scripts/summarize.py", "stdin": "input"}
    _, prepared = await execution.prepare(context, arguments)
    write_jwks(Path(os.environ["SKILL_TRUST_KEYS_FILE"]), Signer())
    with pytest.raises(SkillVerifyError):
        await execution.prepare(context, prepared, replay=True)


async def test_undeclared_script_preserves_sandbox_denied_contract(signed_script, tmp_path):
    skills = await store.load_locked(signed_script.lock, "default")
    context = SimpleNamespace(skills=skills, run_id="undeclared-script-run", workspace=str(tmp_path))
    with pytest.raises(SkillVerifyError) as failure:
        await execution.prepare(context, {"name": "demo-skill", "script": "scripts/undeclared.py"})
    assert failure.value.code == sandbox.SANDBOX_DENIED
