import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from runtime.api.messages import _load_agent
from runtime.config import settings
from runtime.loop import runner
from runtime.node.agent import NodeAgent
from runtime.node.lifecycle import AdmissionMiddleware, NodeLifecycle
from runtime.node.models import switch
from runtime.state.db import AgentVersion, NodeModelRoute, Run, SessionLocal


@pytest.fixture
async def versions(monkeypatch):
    from runtime import llm

    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(resolve_alias=AsyncMock()))
    name = "switch-" + uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        for version, alias in (("1", "model-a"), ("2", "model-b")):
            session.add(
                AgentVersion(
                    agent_id=name,
                    version=version,
                    tenant_id="default",
                    status="published",
                    model_alias=alias,
                    system_prompt="same",
                    config={},
                    harness="mock",
                    rollout_percent=100,
                )
            )
        await session.commit()
    return {
        "id": str(uuid.uuid4()),
        "type": "switch_model",
        "tenant_id": "default",
        "args": {"agent_id": name, "base_version": "2", "expected_version": "2", "target_version": "1", "model_alias": "model-a"},
    }


async def test_model_switch_freezes_runs_and_pinned_versions(versions):
    name = versions["args"]["agent_id"]
    old, _ = await _load_agent(name, None, "default")
    old_id = await runner.create_run(old, "human:test", None)
    await switch(settings.node_id, versions)
    selected, reason = await _load_agent(name, None, "default")
    assert selected.version == "1" and reason == "node_model_switch"
    new_id = await runner.create_run(selected, "human:test", None)
    pinned, _ = await _load_agent(name, "2", "default")
    assert pinned.model_alias == "model-b"
    async with SessionLocal() as session:
        assert (await session.get(Run, old_id)).model_alias == "model-b"
        assert (await session.get(Run, new_id)).model_alias == "model-a"
        base = (await session.execute(select(AgentVersion).where(AgentVersion.agent_id == name, AgentVersion.version == "2"))).scalar_one()
        assert base.model_alias == "model-b"
    await switch(settings.node_id, versions)
    changed = versions | {"args": versions["args"] | {"model_alias": "forged"}}
    with pytest.raises(ValueError, match="reused"):
        await switch(settings.node_id, changed)


async def test_model_switch_rejects_cross_tenant_draft_snapshot_drift_and_stale_cas(versions):
    with pytest.raises(ValueError):
        await switch(settings.node_id, versions | {"tenant_id": "another-tenant"})
    with pytest.raises(ValueError):
        await switch(settings.node_id, versions | {"args": versions["args"] | {"expected_version": "1"}})
    async with SessionLocal() as session:
        target = (
            await session.execute(
                select(AgentVersion).where(AgentVersion.agent_id == versions["args"]["agent_id"], AgentVersion.version == "1")
            )
        ).scalar_one()
        target.status = "draft"
        await session.commit()
    with pytest.raises(ValueError):
        await switch(settings.node_id, versions)
    async with SessionLocal() as session:
        target = (
            await session.execute(
                select(AgentVersion).where(AgentVersion.agent_id == versions["args"]["agent_id"], AgentVersion.version == "1")
            )
        ).scalar_one()
        target.status = "published"
        target.system_prompt = "changed"
        await session.commit()
    with pytest.raises(ValueError, match="non-model"):
        await switch(settings.node_id, versions)


async def test_model_route_is_durable_retirement_fails_closed_and_old_receipt_does_not_undo_new_command(versions):
    await switch(settings.node_id, versions)
    newer = versions | {
        "id": str(uuid.uuid4()),
        "args": versions["args"] | {"expected_version": "1", "target_version": "2", "model_alias": "model-b"},
    }
    await switch(settings.node_id, newer)
    await switch(settings.node_id, versions)
    async with SessionLocal() as session:
        route = await session.get(NodeModelRoute, (settings.node_id, "default", versions["args"]["agent_id"]))
        assert route.target_version == "2"
    await switch(settings.node_id, versions | {"id": str(uuid.uuid4())})
    async with SessionLocal() as session:
        target = (
            await session.execute(
                select(AgentVersion).where(AgentVersion.agent_id == versions["args"]["agent_id"], AgentVersion.version == "1")
            )
        ).scalar_one()
        target.status = "retired"
        await session.commit()
    with pytest.raises(HTTPException):
        await _load_agent(versions["args"]["agent_id"], None, "default")


async def test_restart_requires_drain_and_durable_ack_and_recovers_lost_ack_response(monkeypatch):
    exit_request = Mock()
    lifecycle = NodeLifecycle(SimpleNamespace(worker=None), exit_request, durable=True)
    agent = NodeAgent(lifecycle=lifecycle)
    monkeypatch.setattr(agent, "heartbeat", AsyncMock())
    command = {"id": "restart-command", "type": "graceful_restart", "authorization_role": "PLATFORM_ADMIN", "args": {"timeout_s": 1.0}}
    pending = [command]
    fail_ack = True

    async def call(method, path, json=None):
        request = httpx.Request(method, "http://console.test" + path)
        if path.endswith("/receipt"):
            return httpx.Response(200, json={"status": "ok", "result": agent._done["restart-command"]["detail"]}, request=request)
        if method == "GET":
            return httpx.Response(200, json=pending, request=request)
        return httpx.Response(503 if fail_ack else 200, request=request)

    monkeypatch.setattr(agent, "_call", call)
    replies = await agent.poll_commands()
    assert replies[0]["status"] == "ok" and not lifecycle.accepting
    exit_request.assert_not_called()
    pending.clear()
    await agent.poll_commands()
    exit_request.assert_called_once()


async def test_restart_timeout_resumes_without_canceling_work():
    idle = asyncio.Event()
    worker = SimpleNamespace(paused=False, idle=idle)
    exit_request = Mock()
    lifecycle = NodeLifecycle(SimpleNamespace(worker=worker), exit_request, durable=True)
    with pytest.raises(TimeoutError):
        await lifecycle.prepare(0.01, AsyncMock())
    assert lifecycle.accepting and not worker.paused and not lifecycle.prepared
    exit_request.assert_not_called()
    lifecycle.request_exit = None
    with pytest.raises(ValueError):
        await lifecycle.prepare(1, AsyncMock())


async def test_restart_refuses_memory_database(monkeypatch):
    from sqlalchemy import make_url

    from runtime.state import db

    monkeypatch.setattr(db, "engine", SimpleNamespace(url=make_url("sqlite+aiosqlite:///:memory:")))
    exit_request = Mock()
    lifecycle = NodeLifecycle(SimpleNamespace(worker=None), exit_request, durable=True)
    with pytest.raises(ValueError, match="persistent Runtime database"):
        await lifecycle.prepare(1, AsyncMock())
    assert lifecycle.accepting
    exit_request.assert_not_called()


async def test_restart_rejects_unprivileged_and_mismatched_ack(monkeypatch):
    exit_request = Mock()
    lifecycle = NodeLifecycle(SimpleNamespace(worker=None), exit_request, durable=True)
    agent = NodeAgent(lifecycle=lifecycle)
    monkeypatch.setattr(agent, "heartbeat", AsyncMock())
    command = {"id": "denied", "type": "graceful_restart", "authorization_role": "TENANT_ADMIN", "args": {}}
    assert (await agent.execute(command))["status"] == "error"
    assert lifecycle.accepting
    command["authorization_role"] = "PLATFORM_ADMIN"

    async def call(method, path, json=None):
        return httpx.Response(
            200,
            json=[command] if method == "GET" else {"status": "error", "result": {}},
            request=httpx.Request(method, "http://console.test" + path),
        )

    monkeypatch.setattr(agent, "_call", call)
    await agent.poll_commands()
    assert lifecycle.accepting and agent._restart_id is None
    exit_request.assert_not_called()


async def test_worker_pause_blocks_new_deliveries_without_cancelling_inflight():
    from runtime.worker.queue import InMemoryQueue
    from runtime.worker.settings import WorkerSettings
    from runtime.worker.worker import Worker

    started = asyncio.Event()
    finished = asyncio.Event()

    async def drive(run_id):
        started.set()
        await finished.wait()
        yield None

    queue = InMemoryQueue()
    worker = Worker(queue, WorkerSettings(redis_url=None, reaper_s=3600), drive=drive)
    worker.paused = True
    await queue.enqueue("not-a-persisted-run", "test")
    task = asyncio.create_task(worker._consume_loop())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(started.wait(), timeout=0.02)
        assert worker.idle.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not started.is_set()


async def test_model_probe_failure_keeps_route_unmodified(versions, monkeypatch):
    from runtime import llm

    assert (await NodeAgent(serves_requests=False).execute(versions))["detail"]["code"] == "MODEL_SWITCH_REQUIRES_API_NODE"
    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(resolve_alias=AsyncMock(side_effect=llm.UnknownModelAlias("missing"))))
    with pytest.raises(llm.UnknownModelAlias):
        await switch(settings.node_id, versions)
    async with SessionLocal() as session:
        assert await session.get(NodeModelRoute, (settings.node_id, "default", versions["args"]["agent_id"])) is None


async def test_model_switch_can_rebase_after_normal_publication_without_old_receipt_undoing_it(versions):
    await switch(settings.node_id, versions)
    name = versions["args"]["agent_id"]
    async with SessionLocal() as session:
        session.add(
            AgentVersion(
                agent_id=name,
                version="3",
                tenant_id="default",
                status="published",
                model_alias="model-c",
                system_prompt="same",
                config={},
                harness="mock",
                rollout_percent=100,
            )
        )
        await session.commit()
    current, _ = await _load_agent(name, None, "default")
    assert current.version == "3"
    rebased = versions | {"id": str(uuid.uuid4()), "args": versions["args"] | {"base_version": "3", "expected_version": "3"}}
    await switch(settings.node_id, rebased)
    await switch(settings.node_id, versions)
    current, _ = await _load_agent(name, None, "default")
    assert current.version == "1"
    async with SessionLocal() as session:
        route = await session.get(NodeModelRoute, (settings.node_id, "default", name))
        assert route.base_version == "3" and route.target_version == "1"


async def test_worker_drain_waits_for_existing_task_without_cancelling_it(monkeypatch):
    from runtime.worker.queue import Delivery, InMemoryQueue
    from runtime.worker.settings import WorkerSettings
    from runtime.worker.worker import Worker

    worker = Worker(InMemoryQueue(), WorkerSettings(redis_url=None))
    entered, release = asyncio.Event(), asyncio.Event()

    async def handle(delivery):
        entered.set()
        await release.wait()

    monkeypatch.setattr(worker, "_handle", handle)
    worker._spawn(Delivery("test-message", "test-run", "test"))
    await entered.wait()
    lifecycle = NodeLifecycle(SimpleNamespace(worker=worker), Mock(), durable=True)
    notified = asyncio.Event()

    async def notify():
        notified.set()

    preparing = asyncio.create_task(lifecycle.prepare(2, notify))
    await notified.wait()
    assert worker.paused and not preparing.done()
    assert all(not task.cancelled() for task in worker._tasks)
    release.set()
    await preparing
    assert worker.idle.is_set() and not worker._tasks


async def test_restart_pool_shutdown_deduplicates_shared_owned_pools(monkeypatch):
    from runtime.harness import registry

    pool = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(
        registry, "_REGISTRY", {"first": SimpleNamespace(pool=pool), "second": SimpleNamespace(pool=pool), "mock": SimpleNamespace()}
    )
    await registry.shutdown_pools()
    pool.aclose.assert_awaited_once()


async def test_drain_rejects_new_requests_but_allows_existing_request_to_finish():
    app = FastAPI()
    lifecycle = NodeLifecycle(SimpleNamespace(worker=None), Mock(), durable=True)
    app.state.node_lifecycle = lifecycle
    app.add_middleware(AdmissionMiddleware)
    entered, release = asyncio.Event(), asyncio.Event()

    @app.post("/work")
    async def work():
        entered.set()
        await release.wait()
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = asyncio.create_task(client.post("/work"))
        await entered.wait()
        notified = asyncio.Event()

        async def notify():
            notified.set()

        preparing = asyncio.create_task(lifecycle.prepare(2, notify))
        await notified.wait()
        assert (await client.post("/work")).status_code == 503
        assert not preparing.done()
        release.set()
        assert (await first).status_code == 200
        await preparing
    assert lifecycle.prepared and lifecycle.active_requests == 0
