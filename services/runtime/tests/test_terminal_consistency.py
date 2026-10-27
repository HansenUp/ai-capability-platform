from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update

from ai_contract import RunEventType, RunState
from runtime.state import db
from runtime.state.db import Run, RunCredential
from runtime.state.eventlog import EventLog, acquire_lease, fence_scope, lease_info, release_lease, renew_lease
from runtime.worker import lease
from runtime.worker.queue import Delivery, InMemoryQueue
from runtime.worker.settings import WorkerSettings
from runtime.worker.worker import Worker


async def _new_run() -> str:
    run_id = f"terminal-{uuid4().hex[:16]}"
    async with db.SessionLocal() as session:
        session.add(Run(run_id=run_id, agent_id="demo", agent_version="1", model_alias="m", subject="u", state="working"))
        session.add(RunCredential(run_id=run_id, subject="u", ciphertext=b"synthetic"))
        await session.commit()
    return run_id


@pytest.mark.parametrize("state", [RunState.COMPLETED, RunState.FAILED, RunState.CANCELED])
async def test_terminal_commit_clears_lease_and_credential_before_drive_exits(state: RunState) -> None:
    run_id = await _new_run()
    committed = asyncio.Event()
    finish = asyncio.Event()

    async def drive(current: str) -> AsyncIterator[None]:
        async with db.SessionLocal() as session:
            await EventLog(session).transition(current, state, "test")
            await session.commit()
        committed.set()
        await finish.wait()
        yield None

    worker = Worker(InMemoryQueue(), WorkerSettings(heartbeat_s=1000), drive=drive, consumer="terminal-owner")
    task = asyncio.create_task(worker.process(Delivery(msg_id="test", run_id=run_id, reason="test")))
    try:
        await asyncio.wait_for(committed.wait(), 5)
        assert not task.done()
        assert await lease_info(run_id) == (None, 1, None)
        async with db.SessionLocal() as session:
            run = await session.get(Run, run_id)
            assert run is not None and run.state == state.value
            assert await session.get(RunCredential, run_id) is None
        assert await acquire_lease(run_id, "other", 30) is None
        assert not await renew_lease(run_id, "terminal-owner", 1, 30)
    finally:
        finish.set()
        await task
    assert task.result() is True


async def test_terminal_rollback_restores_state_lease_credential_and_events() -> None:
    run_id = await _new_run()
    assert await acquire_lease(run_id, "owner", 30) == 1
    before = await lease_info(run_id)
    with fence_scope(1, run_id):
        async with db.SessionLocal() as session:
            run = await session.get(Run, run_id)
            await EventLog(session).transition(run_id, RunState.COMPLETED)
            assert run is not None and run.lease_owner is None and run.lease_expires_at is None
            assert await session.get(RunCredential, run_id) is None
            await session.rollback()
    assert await lease_info(run_id) == before
    async with db.SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run is not None and run.state == "working"
        assert await session.get(RunCredential, run_id) is not None
        assert await EventLog(session).list(run_id) == []


async def test_terminal_release_is_epoch_guarded_and_allows_cancel_boundary_event() -> None:
    run_id = await _new_run()
    assert await acquire_lease(run_id, "owner", 30) == 1
    async with db.SessionLocal() as session:
        await EventLog(session).transition(run_id, RunState.CANCELED)
        await session.commit()
    assert not await release_lease(run_id, "owner", 0)
    assert await release_lease(run_id, "owner", 1)
    assert await release_lease(run_id, "owner", 1)
    with fence_scope(1, run_id):
        async with db.SessionLocal() as session:
            await EventLog(session).append(run_id, RunEventType.RUN_DONE, {"stop_reason": "cancelled"})
            await session.commit()


@pytest.mark.parametrize("state", [RunState.COMPLETED, RunState.FAILED, RunState.CANCELED])
async def test_heartbeat_recognizes_atomic_terminal_release(state: RunState, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = await _new_run()
    assert await acquire_lease(run_id, "owner", 30) == 1
    async with db.SessionLocal() as session:
        await EventLog(session).transition(run_id, state)
        await session.commit()
    interrupted: list[tuple[str, int | None]] = []

    async def interrupt(current: str, *, epoch: int | None = None) -> bool:
        interrupted.append((current, epoch))
        return True

    monkeypatch.setattr(lease.control, "interrupt_local", interrupt)
    held = lease.Lease(run_id, "owner", 1)
    await asyncio.wait_for(lease._heartbeat(held, WorkerSettings(heartbeat_s=0.01), None), 5)
    assert not held.lost.is_set()
    assert held.canceled.is_set() == (state == RunState.CANCELED)
    assert interrupted == ([(run_id, 1)] if state == RunState.CANCELED else [])


@pytest.mark.parametrize("state", [RunState.AUTH_REQUIRED, RunState.INPUT_REQUIRED])
async def test_suspension_retains_credential_and_releases_on_exit(state: RunState) -> None:
    run_id = await _new_run()
    async with lease.hold(run_id, "owner", ws=WorkerSettings(heartbeat_s=1000)) as held:
        assert held is not None
        async with db.SessionLocal() as session:
            await EventLog(session).transition(run_id, state)
            await session.commit()
            assert await session.get(RunCredential, run_id) is not None
        assert (await lease_info(run_id))[0] == "owner"
    assert held.released
    assert await lease_info(run_id) == (None, 1, None)


async def test_terminal_of_new_epoch_does_not_ack_or_release_for_old_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = await _new_run()
    assert await acquire_lease(run_id, "old", 30) == 1
    async with db.SessionLocal() as session:
        await session.execute(update(Run).where(Run.run_id == run_id).values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()
    assert await acquire_lease(run_id, "new", 30) == 2
    with fence_scope(2, run_id):
        async with db.SessionLocal() as session:
            await EventLog(session).transition(run_id, RunState.COMPLETED)
            await session.commit()
    interrupted: list[int | None] = []

    async def interrupt(current: str, *, epoch: int | None = None) -> bool:
        interrupted.append(epoch)
        return True

    monkeypatch.setattr(lease.control, "interrupt_local", interrupt)
    held = lease.Lease(run_id, "old", 1)
    await asyncio.wait_for(lease._heartbeat(held, WorkerSettings(heartbeat_s=0.01), None), 5)
    assert held.lost.is_set() and interrupted == [1]
    assert not await release_lease(run_id, "old", 1)
    assert await lease_info(run_id) == (None, 2, None)
