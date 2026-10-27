"""stdio worker 进程池（S6 §3）：预热、上限与 POOL_EXHAUSTED、各类超时回收、进程组回收（无孙进程残留）、
RSS 看门狗、rlimit 启动器、热 acquire 延迟，以及池模式下的五项契约探针。"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ai_contract import HookSet, SessionSpec, TurnInput
from ai_harness_stdio import PoolConfig, PoolExhausted, StdioHarness, WorkerPool
from ai_harness_stdio.pool import Worker, group_alive, group_pids

SPEC = SessionSpec(run_id="r1", agent_id="a", agent_version="1", model_alias="m", system_prompt="s", subject="human:t")
ECHO = [sys.executable, "-m", "ai_harness_stdio.echo_harness"]

# 测试替身：最小 NDJSON harness。模式（argv[1]）：
#   fork     —— open 时派生一个长睡眠的孙进程，close 时不回收它（模拟孙进程泄漏）
#   hang     —— turn 帧不作任何应答
#   alloc    —— turn 时孙进程与自身各分配约 80 MiB 后挂起（两者各自不超限，按进程组合计超限）
#   no-hello —— 不认识 hello 帧（不回 ready）
#   env      —— open 时把自身环境变量名、TOK_FILE 指向的密钥文件内容 / 权限、STDIO_WORKER_DIR 编进 session_id，并删除密钥文件
# （resume 帧一律不应答，用于 resume 超时用例）
FAKE = r"""
import json, os, subprocess, sys
mode = sys.argv[1] if len(sys.argv) > 1 else ""
kids, blob = [], None
def out(f):
    sys.stdout.write(json.dumps(f) + "\n"); sys.stdout.flush()
def spawn(code):
    kids.append(subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL))
for line in sys.stdin:
    f = json.loads(line); k = f["frame"]
    if k == "hello" and mode != "no-hello":
        out({"frame": "ready"})
    elif k == "open":
        if mode == "fork":
            spawn("import time; time.sleep(120)")
        sid = "fake"
        if mode == "env":
            p = os.environ["TOK_FILE"]
            info = {"keys": sorted(os.environ), "secret": open(p).read(), "mode": oct(os.stat(p).st_mode & 0o777)}
            os.unlink(p)
            sid = json.dumps(info | {"dir": os.environ.get("STDIO_WORKER_DIR")})
        out({"frame": "opened", "session_id": sid, "capabilities": {"name": "fake", "version": "0", "transport": "stdio"}})
    elif k == "turn":
        if mode == "hang":
            continue
        if mode == "alloc":
            spawn("import os, time; b = os.urandom(80 << 20); time.sleep(120)")
            blob = os.urandom(80 << 20)
            continue
        out({"frame": "event", "event": {"type": "text", "delta": "ok"}})
        out({"frame": "event", "event": {"type": "usage"}})
        out({"frame": "event", "event": {"type": "done", "run_id": "r1", "stop_reason": "end_turn"}})
    elif k == "close":
        break
"""


def fake(mode: str = "") -> list[str]:
    return [sys.executable, "-c", FAKE, mode]


def _isdir(path: str) -> bool:
    return os.path.isdir(path)


def _listdir(path: str) -> list[str]:
    return os.listdir(path)


def cfg(**kw: object) -> PoolConfig:
    base: dict[str, object] = {"min_idle": 1, "max_workers": 4, "tick_s": 0.05, "acquire_timeout_s": 2.0}
    return PoolConfig(**(base | kw))  # type: ignore[arg-type]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def turn(h: StdioHarness, s, text: str = "hi"):  # noqa: ANN001, ANN201
    return [e async for e in h.run_turn(s, TurnInput(messages=[{"role": "user", "content": text}]), [], [], HookSet())]


async def eventually(pred, timeout_s: float = 5.0) -> None:  # noqa: ANN001
    async with asyncio.timeout(timeout_s):
        while not pred():  # noqa: ASYNC110 —— 轮询外部进程状态，没有可等待的事件
            await asyncio.sleep(0.02)


async def test_prewarm_min_idle_handshaken_workers():
    pool = WorkerPool(ECHO, cfg(min_idle=2))
    try:
        await pool.wait_warm()
        st = pool.stats()
        assert st["idle"] == 2 and st["leased"] == 0 and st["total"] == 2
        for pid in pool.idle_pids():
            assert os.getpgid(pid) == pid  # 每个 worker 自成进程组
        h = StdioHarness(ECHO, pool=pool)
        s = await h.open_session(SPEC)
        evs = await turn(h, s)
        assert evs[-1].type == "done" and evs[-1].stop_reason == "end_turn"
        await h.close(s)
        await pool.wait_warm()
        assert pool.stats()["idle"] == 2 and h.running_pids() == []
    finally:
        await pool.aclose()


async def test_harness_without_pool_unchanged_and_pool_cmd_must_match():
    h = StdioHarness(ECHO)
    assert h.pool is None
    s = await h.open_session(SPEC)
    assert (await turn(h, s))[-1].stop_reason == "end_turn"
    await h.close_all()
    assert h.running_pids() == []
    with pytest.raises(ValueError):
        StdioHarness(fake(), pool=WorkerPool(ECHO, cfg()))


async def test_max_workers_and_pool_exhausted():
    pool = WorkerPool(ECHO, cfg(min_idle=1, max_workers=2, acquire_timeout_s=0.4))
    try:
        h = StdioHarness(ECHO, pool=pool)
        s1, s2 = await h.open_session(SPEC), await h.open_session(SPEC)
        assert pool.stats()["total"] == 2  # 补热也不突破上限
        t0 = time.monotonic()
        with pytest.raises(PoolExhausted) as ei:
            await h.open_session(SPEC)
        assert ei.value.code == "POOL_EXHAUSTED" and 0.35 <= time.monotonic() - t0 < 2.0
        # 等待中的 acquire 在有会话结束后拿到 worker
        waiter = asyncio.create_task(h.open_session(SPEC))
        await asyncio.sleep(0.1)
        assert not waiter.done()
        await h.close(s1)
        s3 = await waiter
        assert pool.stats()["total"] <= 2
        await h.close(s2)
        await h.close(s3)
        assert h.running_pids() == []
    finally:
        await pool.aclose()


async def test_released_capacity_is_reserved_for_waiting_acquire(monkeypatch: pytest.MonkeyPatch):
    """释放中的槽位不能被慢预热抢占；容量等待期限不应变成后台握手期限。"""
    pool = WorkerPool(ECHO, cfg(min_idle=0, max_workers=2, acquire_timeout_s=0.4))
    waiting, retiring, allow_retire, allow_warm = (asyncio.Event() for _ in range(4))
    waiter = release = None
    try:
        first, second = await pool.acquire(), await pool.acquire()
        assert pool._task is not None
        pool._task.cancel()
        await asyncio.gather(pool._task, return_exceptions=True)
        pool.config = cfg(min_idle=1, max_workers=2, acquire_timeout_s=0.4)
        original_wait, original_terminate, original_spawn = pool._cond.wait, pool._terminate, pool._spawn
        original_notify = pool._notify

        async def observed_wait() -> bool:
            waiting.set()
            return await original_wait()

        async def held_terminate(worker: Worker, *, graceful: bool) -> None:
            if worker is first:
                retiring.set()
                await allow_retire.wait()
            await original_terminate(worker, graceful=graceful)

        async def delayed_background_spawn() -> Worker:
            if asyncio.current_task() in pool._bg:
                await allow_warm.wait()
            return await original_spawn()

        async def replenish_before_notify() -> None:
            pool._replenish()
            await original_notify()

        monkeypatch.setattr(pool._cond, "wait", observed_wait)
        monkeypatch.setattr(pool, "_terminate", held_terminate)
        monkeypatch.setattr(pool, "_spawn", delayed_background_spawn)
        monkeypatch.setattr(pool, "_notify", replenish_before_notify)
        waiter = asyncio.create_task(pool.acquire())
        await asyncio.wait_for(waiting.wait(), 5)
        release = asyncio.create_task(pool.release(first))
        await asyncio.wait_for(retiring.wait(), 5)
        pool._replenish()
        allow_retire.set()
        await release
        replacement = await waiter
        assert replacement is not first and pool.stats()["total"] == 2
        await pool.release(second)
        await pool.release(replacement)
    finally:
        allow_retire.set()
        allow_warm.set()
        for task in (waiter, release):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (waiter, release) if task is not None), return_exceptions=True)
        await pool.aclose()


async def test_retiring_worker_still_counts_toward_process_limit(monkeypatch: pytest.MonkeyPatch):
    """进程真正回收之前不得补热，物理进程数也须满足 max_workers。"""
    pool = WorkerPool(ECHO, cfg(min_idle=0, max_workers=1))
    entered, finish = asyncio.Event(), asyncio.Event()
    release = None
    try:
        worker = await pool.acquire()
        assert pool._task is not None
        pool._task.cancel()
        await asyncio.gather(pool._task, return_exceptions=True)
        pool.config = cfg(min_idle=1, max_workers=1)
        original = pool._terminate

        async def held_terminate(current: Worker, *, graceful: bool) -> None:
            entered.set()
            await finish.wait()
            await original(current, graceful=graceful)

        monkeypatch.setattr(pool, "_terminate", held_terminate)
        release = asyncio.create_task(pool.release(worker))
        await asyncio.wait_for(entered.wait(), 5)
        assert pool.stats()["total"] == 1 and group_alive(worker.pgid)
        pool._replenish()
        assert pool.stats()["starting"] == 0
        finish.set()
        await release
        assert pool.stats()["total"] == 0 and not group_alive(worker.pgid)
    finally:
        finish.set()
        if release is not None:
            await asyncio.gather(release, return_exceptions=True)
        await pool.aclose()


async def test_cancelled_waiter_does_not_block_replenishment():
    """容量等待取消后清除等待登记，后续正常补热。"""
    pool = WorkerPool(ECHO, cfg(min_idle=1, max_workers=1, acquire_timeout_s=0.4))
    waiter = None
    try:
        await pool.wait_warm()
        worker = await pool.acquire()
        waiter = asyncio.create_task(pool.acquire())
        await eventually(lambda: pool._waiters == 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert pool._waiters == 0
        await pool.release(worker)
        await pool.wait_warm()
        assert pool.stats()["total"] == pool.stats()["idle"] == 1
    finally:
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await pool.aclose()


async def test_cancelled_release_remains_counted_and_pool_close_awaits_cleanup(monkeypatch: pytest.MonkeyPatch):
    """release 调用者取消不能丢掉物理进程的容量登记与回收任务。"""
    pool = WorkerPool(ECHO, cfg(min_idle=0, max_workers=1))
    entered, finish = asyncio.Event(), asyncio.Event()
    release = closing = None
    try:
        worker = await pool.acquire()
        original = pool._terminate

        async def held_terminate(current: Worker, *, graceful: bool) -> None:
            entered.set()
            await finish.wait()
            await original(current, graceful=graceful)

        monkeypatch.setattr(pool, "_terminate", held_terminate)
        release = asyncio.create_task(pool.release(worker))
        await asyncio.wait_for(entered.wait(), 5)
        release.cancel()
        with pytest.raises(asyncio.CancelledError):
            await release
        assert pool.stats()["retiring"] == pool.stats()["total"] == 1
        closing = asyncio.create_task(pool.aclose())
        await eventually(lambda: pool._closed)
        assert not closing.done() and group_alive(worker.pgid)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert pool.stats()["retiring"] == pool.stats()["total"] == 1
        finish.set()
        await pool.aclose()
        assert pool.stats()["total"] == 0 and not group_alive(worker.pgid)
        assert worker.root is not None and not _isdir(worker.root)
    finally:
        finish.set()
        for task in (release, closing):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        await pool.aclose()


async def test_pool_close_wakes_capacity_waiters():
    """关闭池立即终止容量等待，不等超时后再误报耗尽。"""
    pool = WorkerPool(ECHO, cfg(min_idle=0, max_workers=1, acquire_timeout_s=0.4))
    waiter = None
    try:
        await pool.acquire()
        waiter = asyncio.create_task(pool.acquire())
        await eventually(lambda: pool._waiters == 1)
        await pool.aclose()
        with pytest.raises(RuntimeError, match="pool is closed"):
            await waiter
        assert pool._waiters == 0 and pool.stats()["total"] == 0
    finally:
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await pool.aclose()


async def test_interrupt_reclaims_and_rewarms_without_cross_session_reuse():
    """D26：interrupt → close → 回收进程组 → 补热；新会话拿到的是另一个 worker。"""
    pool = WorkerPool(ECHO, cfg(min_idle=1))
    try:
        await pool.wait_warm()
        h = StdioHarness(ECHO, pool=pool)
        s1 = await h.open_session(SPEC)
        pid1 = h._workers[s1.session_id].pid
        await turn(h, s1)
        await turn(h, s1)  # 会话内复用同一 worker
        assert h._workers[s1.session_id].pid == pid1
        await h.interrupt(s1, "r1")
        assert not group_alive(pid1) and h.running_pids() == []
        await pool.wait_warm()
        assert pool.stats()["idle"] == 1
        s2 = await h.open_session(SPEC)
        assert h._workers[s2.session_id].pid != pid1
        await h.close(s2)
    finally:
        await pool.aclose()


async def test_no_grandchild_leak_on_close_and_pool_close():
    pool = WorkerPool(fake("fork"), cfg(min_idle=0))
    h = StdioHarness(fake("fork"), pool=pool)
    try:
        w = await pool.acquire()
        await h._send(w.proc, {"frame": "open", "session": SPEC.model_dump(mode="json")})
        await h._recv(w.proc)  # opened（孙进程已派生）
        (kid,) = [p for p in group_pids([w.pgid]) if p != w.pid]  # 孙进程在 worker 的进程组里
        await pool.release(w)  # 组长正常退出、未回收孙进程 → killpg 兜底
        await eventually(lambda: not pid_alive(kid) or _is_zombie(kid))
        # 经 StdioHarness：running_pids 按进程组统计（含孙进程），close 后清零
        s = await h.open_session(SPEC)
        await eventually(lambda: len(h.running_pids()) == 2)
        await h.close(s)
        assert h.running_pids() == []
        s = await h.open_session(SPEC)
        await eventually(lambda: len(h.running_pids()) == 2)
        pgid = h._workers[s.session_id].pgid
    finally:
        await pool.aclose()
    await eventually(lambda: not group_alive(pgid))  # 被 SIGKILL 的孙进程由 init/launchd 收尸后组即消失
    assert h.running_pids() == []


def _ps(field: str, pid: int) -> str:
    """ps 查询单个进程的一个字段（测试自己构造的参数，非外部输入）。"""
    return subprocess.run(["/bin/ps", "-ww", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout  # noqa: S603


def _is_zombie(pid: int) -> bool:
    return _ps("stat", pid).strip().startswith("Z")


async def test_turn_frame_timeout_kills_group():
    pool = WorkerPool(fake("hang"), cfg(min_idle=0, turn_frame_timeout_s=0.3))
    try:
        h = StdioHarness(fake("hang"), pool=pool)
        s = await h.open_session(SPEC)
        pgid, root = h._workers[s.session_id].pgid, h._workers[s.session_id].root
        assert root is not None and _isdir(root)
        evs = await turn(h, s)
        assert [e.type for e in evs] == ["error", "usage", "done"] and evs[0].code == "TURN_FRAME_TIMEOUT"
        await eventually(lambda: not group_alive(pgid))
        await eventually(lambda: not os.path.exists(root))  # 强制回收（killpg）后 worker 根目录由池删除
        await h.close(s)
        assert pool.stats()["total"] == 0
    finally:
        await pool.aclose()


async def test_session_idle_timeout():
    pool = WorkerPool(ECHO, cfg(min_idle=1, session_idle_timeout_s=0.3))
    try:
        h = StdioHarness(ECHO, pool=pool)
        s = await h.open_session(SPEC)
        pgid = h._workers[s.session_id].pgid
        assert (await turn(h, s))[-1].stop_reason == "end_turn"
        await eventually(lambda: not group_alive(pgid))
        evs = await turn(h, s)
        assert evs[0].type == "error" and evs[0].code == "SESSION_IDLE_TIMEOUT" and evs[-1].stop_reason == "error"
        assert pool.reaped == {"SESSION_IDLE_TIMEOUT": 1}
        await h.close(s)
        await pool.wait_warm()  # 回收后补回 min_idle
    finally:
        await pool.aclose()


async def test_session_wall_timeout_during_turn():
    pool = WorkerPool(fake("hang"), cfg(min_idle=0, session_wall_timeout_s=0.5, turn_frame_timeout_s=None))
    try:
        h = StdioHarness(fake("hang"), pool=pool)
        s = await h.open_session(SPEC)
        t0 = time.monotonic()
        evs = await turn(h, s)  # turn 中（busy）不受空闲超时影响，但受墙钟约束
        assert evs[0].code == "SESSION_WALL_TIMEOUT" and 0.4 < time.monotonic() - t0 < 3
        await h.close(s)
        assert h.running_pids() == []
    finally:
        await pool.aclose()


async def test_warm_worker_ttl_recycles():
    pool = WorkerPool(ECHO, cfg(min_idle=1, warm_ttl_s=0.4))
    try:
        await pool.wait_warm()
        (old,) = pool.idle_pids()
        await eventually(lambda: pool.idle_pids() not in ([], [old]), timeout_s=10)
        assert not group_alive(old) and pool.reaped.get("WARM_TTL_EXPIRED", 0) >= 1
    finally:
        await pool.aclose()


async def test_rss_watchdog_kills_group_over_limit():
    """孙进程与组长各约 90 MiB，单个都不超 150 MiB，按进程组合计超限 → killpg。"""
    pool = WorkerPool(fake("alloc"), cfg(min_idle=0, rss_limit_mb=150, tick_s=0.1, turn_frame_timeout_s=None))
    try:
        h = StdioHarness(fake("alloc"), pool=pool)
        s = await h.open_session(SPEC)
        pgid, root = h._workers[s.session_id].pgid, h._workers[s.session_id].root
        async with asyncio.timeout(20):
            evs = await turn(h, s)
        assert evs[0].code == "RSS_LIMIT_EXCEEDED" and evs[-1].stop_reason == "error"
        await eventually(lambda: not group_alive(pgid))
        assert root is not None
        await eventually(lambda: not os.path.exists(root))
        await h.close(s)
        assert h.running_pids() == []
    finally:
        await pool.aclose()


async def test_worker_env_is_allowlisted_and_secrets_come_as_files(monkeypatch: pytest.MonkeyPatch):
    """worker 不继承创建者的环境（平台密钥不外泄）；密钥只以 0600 文件路径传入（不进环境、不进 repr）；根目录随回收删除。"""
    monkeypatch.setenv("SOME_PLATFORM_SECRET", "leak-me")
    pool = WorkerPool(fake("env"), cfg(min_idle=0, env={"EXTRA": "1"}, secret_env={"TOK": "s3cret"}))
    try:
        assert "s3cret" not in repr(pool.config)
        h = StdioHarness(fake("env"), pool=pool)
        s = await h.open_session(SPEC)
        info = json.loads(s.native_ref or "{}")
        assert "SOME_PLATFORM_SECRET" not in info["keys"] and "TOK" not in info["keys"]
        assert {"EXTRA", "TOK_FILE", "PATH", "STDIO_WORKER_DIR"} <= set(info["keys"])
        assert info["secret"] == "s3cret" and info["mode"] == "0o600"  # noqa: S105 - 测试用假密钥
        root = info["dir"]
        assert root == h._workers[s.session_id].root and _isdir(root) and _listdir(root) == []  # 密钥文件已被子进程删除
        await h.close(s)
        assert not _isdir(root)
    finally:
        await pool.aclose()


async def test_enforce_skips_idle_worker_leased_during_sweep():
    """维护循环在 kill 的 await 期间，另一个空闲 worker 被 acquire 租出：不得再按 WARM_TTL 杀掉它。"""
    pool = WorkerPool(ECHO, cfg(min_idle=2, warm_ttl_s=3600))
    try:
        await pool.wait_warm()
        assert pool._task is not None
        pool._task.cancel()  # 停掉后台维护循环，手动驱动一次 _enforce
        await asyncio.gather(pool._task, return_exceptions=True)
        for w in pool._idle:
            w.created -= 7200  # 两个都已超过热 worker TTL
        real_kill, leased = pool.kill, []

        async def kill(w, reason):  # noqa: ANN001, ANN202
            await real_kill(w, reason)
            if not leased:
                leased.append(await pool.acquire())

        pool.kill = kill  # type: ignore[method-assign]
        await pool._enforce()
        (w,) = leased
        assert w.state == "leased" and w.kill_reason is None and w.proc.returncode is None
        assert pool.reaped == {"WARM_TTL_EXPIRED": 1}
        await pool.release(w)
    finally:
        await pool.aclose()


async def test_resume_without_ack_times_out_and_releases_worker():
    """resume 帧得不到 resumed 确认：在 turn_frame_timeout_s 内失败，会话关闭、worker 释放，无残留进程。"""
    pool = WorkerPool(fake(), cfg(min_idle=0, turn_frame_timeout_s=0.3))
    try:
        h = StdioHarness(fake(), pool=pool)
        with pytest.raises(TimeoutError):
            await h.resume(SPEC, [])
        assert h._procs == {} and h._workers == {} and pool.stats()["leased"] == 0
        await eventually(lambda: h.running_pids() == [])
    finally:
        await pool.aclose()


async def test_handshake_modes():
    """不回 ready 的 harness：handshake=True 时冷启动失败且不残留；handshake=False 时可正常使用。"""
    pool = WorkerPool(fake("no-hello"), cfg(min_idle=0, handshake_timeout_s=0.3))
    try:
        with pytest.raises(TimeoutError):
            await pool.acquire()
        assert pool.stats()["total"] == 0 and pool.reaped == {}
    finally:
        await pool.aclose()
    pool = WorkerPool(fake("no-hello"), cfg(min_idle=1, handshake=False))
    try:
        h = StdioHarness(fake("no-hello"), pool=pool)
        await pool.wait_warm()
        s = await h.open_session(SPEC)
        assert (await turn(h, s))[-1].stop_reason == "end_turn"
        await h.close(s)
    finally:
        await pool.aclose()


def _launch(args: list[str], code: str) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "ai_harness_stdio.limits", *args, "--", sys.executable, "-c", code]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603 —— 测试构造的 argv


def test_limits_launcher_sets_nofile_and_cpu():
    r = _launch(["--nofile", "64", "--cpu", "7"], "import resource as r; print(r.getrlimit(r.RLIMIT_NOFILE), r.getrlimit(r.RLIMIT_CPU))")
    assert r.returncode == 0 and r.stdout.strip() == "(64, 64) (7, 8)", r.stderr
    t0 = time.monotonic()
    r = _launch(["--cpu", "1"], "while True: pass")
    assert r.returncode < 0 and time.monotonic() - t0 < 15  # SIGXCPU / SIGKILL
    assert _launch([], "pass").returncode == 0
    bad = subprocess.run([sys.executable, "-m", "ai_harness_stdio.limits", "--", "/nonexistent/x"], capture_output=True, check=False)
    assert bad.returncode == 127


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_NPROC 仅 Linux 设置")
def test_limits_launcher_sets_nproc_on_linux():
    r = _launch(["--nproc", "4096"], "import resource as r; print(r.getrlimit(r.RLIMIT_NPROC))")
    assert r.stdout.strip() == "(4096, 4096)"


async def test_pool_workers_run_under_limits():
    pool = WorkerPool(ECHO, cfg(min_idle=1, nofile=100))
    try:
        await pool.wait_warm()
        (pid,) = pool.idle_pids()
        if sys.platform.startswith("linux"):
            limits = await asyncio.to_thread(Path(f"/proc/{pid}/limits").read_text, encoding="utf-8")
            assert any(line.startswith("Max open files") and " 100 " in line for line in limits.splitlines())
        # 启动器 exec 后组长即 harness 本身（同一 pid）
        cmdline = _ps("command", pid)
        assert "ai_harness_stdio.echo_harness" in cmdline and "limits" not in cmdline
    finally:
        await pool.aclose()


async def test_warm_acquire_latency_p95():
    """热 acquire（租 worker + open 帧往返）P95 < 50 ms；冷启动仅作对照输出。"""
    pool = WorkerPool(ECHO, cfg(min_idle=3, max_workers=6))
    h = StdioHarness(ECHO, pool=pool)
    warm: list[float] = []
    try:
        await pool.wait_warm()
        for _ in range(20):
            await pool.wait_warm(count=1)
            t0 = time.perf_counter()
            s = await h.open_session(SPEC)
            warm.append((time.perf_counter() - t0) * 1000)
            await h.close(s)
    finally:
        await pool.aclose()
    p95 = statistics.quantiles(warm, n=20)[18]
    cold_h = StdioHarness(ECHO)
    t0 = time.perf_counter()
    s = await cold_h.open_session(SPEC)
    cold = (time.perf_counter() - t0) * 1000
    await cold_h.close(s)
    print(f"warm open P50={statistics.median(warm):.1f}ms P95={p95:.1f}ms max={max(warm):.1f}ms; cold open={cold:.0f}ms")
    assert p95 < 50, warm


async def test_contract_probes_pass_with_pool():
    from ai_harness_probe import run_probes
    from ai_harness_stdio.testing import pooled_probe_factory

    report = await run_probes(pooled_probe_factory)
    assert report.passed, [(r.name, r.failures) for r in report.results]
