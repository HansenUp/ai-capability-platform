"""S7 §6 故障注入。

默认段（CI 常规测试）：SQLite + fakeredis，同进程两个真实 Worker（runner.drive_durable + mock harness），TTL 1 s；
"被杀"的 Worker 以冻结心跳 + 冻结驱动模拟（不续期、不释放、不写入，消息不 ACK）：
- 接管完成：另一个 Worker 经 XAUTOCLAIM 接管（epoch 1→2）完成 Run，事件 seq 连续，每个 operation_id 只有一条 TOOL_EXECUTED、
  工具只执行一次；旧 Worker 恢复后写入抛 LeaseLost；
- 写工具 EXECUTING 期间被杀：接管者不重试，转对账卡（input-required）；旧 Worker 的执行结果写回登记行但事件写入 LeaseLost；
  发起人经 POST /v1/runs/{id}/input 对账 done 后入队续跑完成。

进程级段（marker ``failover``，默认跳过；``pytest services/runtime/tests/test_failover.py -m failover``）：需要
``FAILOVER_REDIS_URL``（专用逻辑库，会被 FLUSHDB）与 ``FAILOVER_DATABASE_URL``（MySQL，库名须含 test，会被清空）。
起 API×1（RUNTIME_ROLE=api）+ Worker×2（``python -m runtime.worker``，mock harness ``MOCK_TURN_DELAY_S``），按 t_run.lease_owner
找到宿主 Worker 进程 SIGKILL，断言 60 s 内由另一个 Worker 完成并输出实际耗时。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import MetaData, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ai_contract import Event, RunEventType, RunState, ToolResultEvent
from runtime.loop import runner, toolbroker
from runtime.state import db
from runtime.state.db import Run, ToolOperation
from runtime.state.eventlog import EventLog, LeaseLost, lease_info
from runtime.worker import queue as queue_mod
from runtime.worker.queue import Delivery, RedisStreamQueue
from runtime.worker.settings import WorkerSettings
from runtime.worker.worker import Worker

fakeredis = pytest.importorskip("fakeredis")

DEV = {"X-AI-Subject": "human:s7-failover"}
WS = WorkerSettings(
    redis_url=None,
    role="worker",
    concurrency=2,
    lease_ttl_s=1,
    heartbeat_s=0.2,
    claim_idle_s=1.2,
    claim_interval_s=0.2,
    reaper_s=1e6,  # 周期 reaper 关闭（会扫到其他用例遗留的持久化 Run）；接管走 XAUTOCLAIM
    max_deliveries=5,
    isolate_grace_s=0.3,
)
# "被杀"的节点：不续期、不刷新消息空闲、不接管；并发 1，冻结的在途 Run 占住唯一槽位，不再领取新消息
FROZEN = dataclasses.replace(WS, concurrency=1, heartbeat_s=1000, claim_interval_s=1000)


class SpyQueue(RedisStreamQueue):
    def __init__(self, client: Any) -> None:
        super().__init__(client)
        self.acked: list[str] = []

    async def ack(self, delivery: Delivery) -> None:
        self.acked.append(delivery.run_id)
        await super().ack(delivery)


class FreezableDrive:
    """runner.drive_durable 的包装：产出满足 freeze_on 的事件后冻结（模拟进程被暂停 / 杀死），thaw 后继续——
    此时租约早已易主，下一次写入抛 LeaseLost。记录驱动中抛出的异常。"""

    def __init__(self, freeze_on: Callable[[Event], bool] | None = None) -> None:
        self.freeze_on = freeze_on
        self.frozen = asyncio.Event()
        self.thaw = asyncio.Event()
        self.started = asyncio.Event()
        self.errors: list[BaseException] = []

    async def __call__(self, run_id: str) -> AsyncIterator[Any]:
        self.started.set()
        try:
            async for ev in runner.drive_durable(run_id):
                yield ev
                if self.freeze_on is not None and self.freeze_on(ev) and not self.frozen.is_set():
                    self.frozen.set()
                    await self.thaw.wait()
        except BaseException as e:
            self.errors.append(e)
            raise


class SlowExec:
    """替换进程内开发执行器：第一次执行阻塞到 release（"被杀"的节点卡在写工具里），之后立即返回。"""

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, tool: Any, tool_input: dict[str, Any], dry_run: bool = False) -> Any:
        if dry_run:
            return {"impact_preview": {"n": 1}}
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
        return {"sent": True, "n": self.calls}


@pytest.fixture
def api_queue() -> Iterator[Any]:
    """API 侧入队改投到 fakeredis（与测试里的 Worker 同一个服务器）；会话级 TestClient 的进程内 Worker 只消费
    InMemoryQueue，不参与。用后恢复。"""
    server = fakeredis.FakeServer()
    saved = (queue_mod._queue, queue_mod._redis)
    api_client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    queue_mod.configure(RedisStreamQueue(api_client), api_client)
    try:
        yield server
    finally:
        queue_mod.configure(*saved)


def _redis(server: Any) -> Any:
    return fakeredis.FakeAsyncRedis(server=server, decode_responses=True)


def _publish(client: Any, tools: list[str]) -> str:
    name = f"s7fo-{uuid.uuid4().hex[:8]}"
    body = {"agent_id": name, "version": "1", "system_prompt": "x", "config": {"tools": tools}}
    assert client.post("/admin/agents", json=body).status_code == 201
    assert client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True}).status_code == 200
    return name


async def _create(client: Any, agent: str, content: str) -> str:
    body = {"agent_id": agent, "input": [{"role": "user", "content": content}]}
    r = await asyncio.to_thread(client.post, "/v1/runs", json=body, headers=DEV)
    assert r.status_code == 202, r.text
    return str(r.json()["run_id"])


async def _state(run_id: str) -> str:
    async with db.SessionLocal() as s:
        run = await s.get(Run, run_id)
    assert run is not None
    return run.state


async def _log(run_id: str) -> list[Any]:
    async with db.SessionLocal() as s:
        return await EventLog(s).list(run_id)


async def _until(cond: Callable[[], Any], within: float = 15.0) -> None:
    deadline = time.monotonic() + within
    while not await cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def _running(*workers: Worker) -> AsyncIterator[None]:
    tasks = [asyncio.create_task(w.run()) for w in workers]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _assert_seq_continuous(log: list[Any]) -> None:
    seqs = [e.seq for e in log]
    assert seqs == list(range(1, len(seqs) + 1)), seqs


# ---------------- 默认段：同进程两 Worker ----------------


async def test_takeover_completes_after_holder_dies(client: Any, api_queue: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """持有者在写工具完成后"被杀"：接管者重放续跑完成，工具只执行一次、每 operation_id 一条 EXECUTED，旧持有者写入 LeaseLost。"""
    execs = SlowExec()
    execs.release.set()  # 不阻塞
    monkeypatch.setattr(toolbroker, "_dev_execute", execs)
    agent = _publish(client, ["mail.send"])
    qa, qb = SpyQueue(_redis(api_queue)), SpyQueue(_redis(api_queue))
    drive_a = FreezableDrive(freeze_on=lambda ev: isinstance(ev, ToolResultEvent))
    run_id = await _create(client, agent, '!tool mail.send {"to": "ops@example.com"}')
    async with _running(Worker(qa, FROZEN, drive=drive_a, consumer="node-a")):
        await asyncio.wait_for(drive_a.frozen.wait(), 10)  # A 已执行写工具并写下结果，随后"被杀"
        owner, epoch_a, _ = await lease_info(run_id)
        assert owner == "node-a" and epoch_a == 1
        t0 = time.monotonic()
        async with _running(Worker(qb, WS, consumer="node-b")):

            async def completed() -> bool:
                return await _state(run_id) == RunState.COMPLETED.value and qb.acked == [run_id]

            await _until(completed)
            took = time.monotonic() - t0
            drive_a.thaw.set()  # A "复活"，继续往下写
            await _until(lambda: _async(bool(drive_a.errors)))
    assert isinstance(drive_a.errors[0], LeaseLost)
    assert qa.acked == []  # 失租者不 ACK
    log = await _log(run_id)
    _assert_seq_continuous(log)
    executed = [e for e in log if e.type == RunEventType.TOOL_EXECUTED]
    assert len(executed) == 1 and execs.calls == 1  # 接管者重放，不重复执行
    ops = {e.payload["operation_id"] for e in log if e.type == RunEventType.TOOL_EXECUTING}
    assert ops == {executed[0].idempotency_key}
    assert (await lease_info(run_id))[1] == 2  # 接管 epoch 递增
    print(f"\n[failover] takeover completed in {took:.2f}s (TTL 1 s)")


async def _async(v: bool) -> bool:
    return v


async def test_killed_during_executing_goes_to_reconciliation_then_input(
    client: Any, api_queue: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """持有者卡在写工具执行中"被杀"：接管者不重试，转对账卡；旧持有者执行结果写回登记行、事件写入 LeaseLost；
    发起人经 /input 对账 done → 入队续跑完成，全程只执行一次、只有一条 TOOL_EXECUTED。"""
    execs = SlowExec()
    monkeypatch.setattr(toolbroker, "_dev_execute", execs)
    agent = _publish(client, ["mail.send"])
    qa, qb = SpyQueue(_redis(api_queue)), SpyQueue(_redis(api_queue))
    drive_a = FreezableDrive()
    run_id = await _create(client, agent, '!tool mail.send {"to": "ops@example.com"}')
    async with _running(Worker(qa, FROZEN, drive=drive_a, consumer="node-a")):
        await asyncio.wait_for(execs.started.wait(), 10)
        async with _running(Worker(qb, WS, consumer="node-b")):

            async def reconciling() -> bool:
                return await _state(run_id) == RunState.INPUT_REQUIRED.value and qb.acked == [run_id]

            await _until(reconciling)
            log = await _log(run_id)
            err = next(e for e in log if e.type == RunEventType.RUN_ERROR)
            assert err.payload["code"] == "UNKNOWN_TOOL_RESULT"
            call_id, op_id = err.payload["call_id"], err.payload["operation_id"]
            assert not [e for e in log if e.type == RunEventType.TOOL_EXECUTED]

            body = {"kind": "reconcile", "call_id": call_id, "action": "done"}
            r = await asyncio.to_thread(client.post, f"/v1/runs/{run_id}/input", json=body, headers=DEV)
            assert r.status_code == 200, r.text

            async def completed() -> bool:
                return await _state(run_id) == RunState.COMPLETED.value and qb.acked.count(run_id) == 2

            await _until(completed)

            execs.release.set()  # 旧持有者的写工具此时才返回：结果写回登记行，事件写入因失租失败
            await _until(lambda: _async(bool(drive_a.errors)))
            assert isinstance(drive_a.errors[0], LeaseLost)
            async with db.SessionLocal() as s:
                op = await s.get(ToolOperation, op_id)
            assert op is not None and op.status == "executed" and op.result_hash and op.epoch == 1
    log = await _log(run_id)
    _assert_seq_continuous(log)
    executed = [e for e in log if e.type == RunEventType.TOOL_EXECUTED]
    assert len(executed) == 1 and executed[0].idempotency_key == op_id and execs.calls == 1
    assert qa.acked == []


# ---------------- 进程级段（-m failover） ----------------

ROOT = Path(__file__).resolve().parents[3]
PY = sys.executable


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _reset_mysql(url: str) -> None:
    u = make_url(url)
    name = u.database or ""
    if u.get_backend_name() != "mysql" or "test" not in name or not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise RuntimeError(f"FAILOVER_DATABASE_URL 须为 MySQL 且库名只含字母数字下划线并包含 'test'，当前为 {name!r}")
    server = create_async_engine(u._replace(database=None), poolclass=NullPool)
    try:
        async with server.begin() as conn:
            await conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4"))
    finally:
        await server.dispose()
    eng = create_async_engine(u, poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            meta = MetaData()
            await conn.run_sync(meta.reflect)
            await conn.run_sync(meta.drop_all)
    finally:
        await eng.dispose()


async def _lease_row(url: str, run_id: str) -> tuple[str, str | None, int, int]:
    eng = create_async_engine(url, poolclass=NullPool)
    try:
        async with eng.connect() as conn:
            row = (
                await conn.execute(text("SELECT state, lease_owner, lease_epoch, last_seq FROM t_run WHERE run_id = :r"), {"r": run_id})
            ).one()
    finally:
        await eng.dispose()
    return str(row[0]), row[1], int(row[2]), int(row[3])


@pytest.mark.failover
async def test_process_level_sigkill_takeover_within_60s(request: pytest.FixtureRequest) -> None:
    if "failover" not in (request.config.getoption("-m") or ""):
        pytest.skip("进程级故障注入：用 -m failover 运行")
    redis_url, db_url = os.environ.get("FAILOVER_REDIS_URL"), os.environ.get("FAILOVER_DATABASE_URL")
    if not redis_url or not db_url:
        pytest.skip("需要 FAILOVER_REDIS_URL 与 FAILOVER_DATABASE_URL（MySQL）")
    import redis.asyncio as aioredis

    await _reset_mysql(db_url)
    r = aioredis.from_url(redis_url)
    await r.flushdb()  # 只清本用例专用的逻辑库
    await r.aclose()
    tmp = Path(tempfile.mkdtemp(prefix="s7-failover-"))
    port = _free_port()
    base_env = os.environ.copy() | {
        "DATABASE_URL": db_url,
        "REDIS_URL": redis_url,
        "WORKSPACE_DIR": str(tmp / "ws"),
        "HARNESS": "mock",
        "AUTH_REQUIRED": "false",
        "MOCK_TURN_DELAY_S": os.environ.get("FAILOVER_TURN_DELAY_S", "4"),
        "POLICY_URL": "http://127.0.0.1:1",
        "CAPABILITY_GATEWAY_URL": "http://127.0.0.1:1",
        "REAPER_S": "10",  # conftest 为进程内用例调大了 REAPER_S；子进程恢复默认
    }
    procs: list[subprocess.Popen[bytes]] = []
    logs = []

    def start(name: str, args: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
        log = open(tmp / f"{name}.log", "wb")  # noqa: SIM115
        logs.append(log)
        p = subprocess.Popen([PY, *args], env=env, stdout=log, stderr=log, cwd=ROOT)  # noqa: S603
        procs.append(p)
        return p

    try:
        start(
            "api",
            ["-m", "uvicorn", "runtime.main:app", "--port", str(port), "--app-dir", str(ROOT / "services/runtime/src")],
            base_env | {"RUNTIME_ROLE": "api", "NODE_ID": "fo-api"},
        )
        api = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=api, timeout=10) as http:
            for _ in range(120):
                with contextlib.suppress(httpx.HTTPError):
                    if (await http.get("/healthz")).status_code == 200:
                        break
                await asyncio.sleep(0.5)
            else:
                raise AssertionError(f"API 未启动，见 {tmp / 'api.log'}")
            workers = {
                start(f"worker{i}", ["-m", "runtime.worker"], base_env | {"RUNTIME_ROLE": "worker", "NODE_ID": f"fo-w{i}"}).pid
                for i in (1, 2)
            }
            name = f"fo-{uuid.uuid4().hex[:6]}"
            body = {"agent_id": name, "version": "1", "system_prompt": "x", "config": {"tools": ["echo"]}}
            assert (await http.post("/admin/agents", json=body)).status_code == 201
            assert (await http.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True})).status_code == 200
            run = {"agent_id": name, "input": [{"role": "user", "content": '!tool echo {"text": "failover"}'}]}
            resp = await http.post("/v1/runs", json=run, headers=DEV)
            assert resp.status_code == 202, resp.text
            run_id = resp.json()["run_id"]
            owner = None
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                state, owner, epoch, _ = await _lease_row(db_url, run_id)
                if owner and state == "working":
                    break
                await asyncio.sleep(0.2)
            assert owner, "没有 Worker 抢到租约"
            victim = int(owner.split(":")[1])
            assert victim in workers, (owner, workers)
            await asyncio.sleep(1)  # 让持有者进入模型轮次（MOCK_TURN_DELAY_S）
            os.kill(victim, signal.SIGKILL)
            t0 = time.monotonic()
            state = ""
            while time.monotonic() - t0 < 90:
                state = (await http.get(f"/v1/runs/{run_id}", headers=DEV)).json()["state"]
                if state in ("completed", "failed", "canceled"):
                    break
                await asyncio.sleep(0.5)
            took = time.monotonic() - t0
            print(f"\n[failover] SIGKILL lease holder pid={victim} → run {state} in {took:.1f}s")
            assert state == "completed" and took <= 60, (state, took)
            log = (await http.get(f"/v1/runs/{run_id}/events", headers=DEV)).json()
            seqs = [e["seq"] for e in log]
            assert seqs == list(range(1, len(seqs) + 1))
            _, owner_after, epoch_after, _ = await _lease_row(db_url, run_id)
            assert owner_after is None and epoch_after >= 2
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        for log in logs:
            log.close()
        with contextlib.suppress(Exception):
            await _reset_mysql(db_url)
