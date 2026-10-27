"""stdio worker 进程池（S6 §3，D99/D100）。

- worker = 一个进程组：子进程以 `start_new_session=True` 启动（新会话兼新进程组，pgid = 子进程 pid），回收时 killpg 整组，
  harness 派生的孙进程（如 ACP 桥下的 goose / dsh）随之回收，不再泄漏；
- 预热：保持 `min_idle` 个已完成握手（hello → ready）、尚未 open 的 worker；`handshake=False` 时
  "已启动"即视为就绪（兼容不认识 hello 帧的 harness）；
- 上限：总进程（空闲 + 租出 + 启动中 + 回收中）≤ `max_workers`；无可用 worker 时 acquire 等待 `acquire_timeout_s`，
  超时抛 PoolExhausted（code=POOL_EXHAUSTED）；
- 只在会话内复用：worker 被一个会话租用直到会话结束，release 即回收进程组（不回空闲队列、不跨会话/租户），
  随后后台补足热 worker（D26：interrupt → close → 回收 → 补热）；
- 超时：turn 内无帧（由 StdioHarness 执行，见 PoolConfig.turn_frame_timeout_s）、会话空闲、会话墙钟、热 worker TTL；
- 资源：启动器 `ai_harness_stdio.limits`（setrlimit 后 execv：NOFILE、CPU，Linux 加 NPROC）+
  按进程组汇总 RSS 的看门狗，超限 killpg。RSS 采样 Linux 读 /proc，其他平台（macOS）用 `ps`，不引入 psutil。
- 环境与临时目录（S6 评审）：worker 不继承创建者（Runtime）的环境，只带 `INHERIT_ENV` 白名单 + `PoolConfig.env`；
  `PoolConfig.secret_env` 的值逐个写进 worker 根目录下的 0600 文件，环境变量只传路径（`<NAME>_FILE`，子进程读完即删）；
  每个 worker 有自己的根目录（`STDIO_WORKER_DIR`），回收时（含强制 killpg）整个删除。

被强制回收的 worker 记录 `kill_reason`（下列 *_TIMEOUT / RSS_LIMIT_EXCEEDED 码），StdioHarness 据此给出 ErrorEvent.code。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .limits import launcher_argv

log = logging.getLogger(__name__)

TURN_FRAME_TIMEOUT = "TURN_FRAME_TIMEOUT"  # turn 进行中，子进程超过 turn_frame_timeout_s 没有任何帧
SESSION_IDLE_TIMEOUT = "SESSION_IDLE_TIMEOUT"  # 会话两次 turn 之间空闲超过 session_idle_timeout_s
SESSION_WALL_TIMEOUT = "SESSION_WALL_TIMEOUT"  # 会话自租用起超过 session_wall_timeout_s
RSS_LIMIT_EXCEEDED = "RSS_LIMIT_EXCEEDED"  # 进程组 RSS 合计超过 rss_limit_mb
WARM_TTL_EXPIRED = "WARM_TTL_EXPIRED"  # 热 worker 存活超过 warm_ttl_s（池内部回收，不对外报错）
WORKER_EXITED = "WORKER_EXITED"  # 空闲 worker 自行退出
HANDSHAKE_FAILED = "HANDSHAKE_FAILED"  # 启动后 hello/ready 握手失败或超时

WORKER_DIR_ENV = "STDIO_WORKER_DIR"  # 池为每个 worker 分配的根目录（子进程的临时文件放在其下，回收时整个删除）
# worker 从创建者继承的环境变量白名单（其余一律不继承，Runtime 环境里的平台密钥因此不会落到 worker）。
INHERIT_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SYSTEMROOT",
    "PYTHONPATH",
    "GOOSE_BIN",
    "DSH_BIN",
    "DSH_NODE",
    "ACP_BRIDGE_LOG",
)


class PoolExhausted(RuntimeError):
    """在 acquire_timeout_s 内拿不到 worker（已达 max_workers 且无空闲）。"""

    code = "POOL_EXHAUSTED"


@dataclass(frozen=True)
class PoolConfig:
    """池参数；超时 / 限制取 None 表示不启用。"""

    min_idle: int = 1  # 预热的空闲 worker 数
    max_workers: int = 8  # 进程（组）总数上限：空闲 + 租出 + 启动中
    acquire_timeout_s: float = 10.0  # 达到上限时 acquire 的最长等待
    handshake: bool = True  # 预热时发 hello 帧并等待 ready（False：已启动即就绪）
    handshake_timeout_s: float = 30.0
    warm_ttl_s: float | None = 1800.0  # 热 worker 自启动起的最长存活，到期回收并补新
    turn_frame_timeout_s: float | None = 300.0  # turn 内（及 open 时）等待下一帧的最长时间
    session_idle_timeout_s: float | None = 900.0  # 会话不在 turn 中的最长空闲
    session_wall_timeout_s: float | None = 3600.0  # 会话自租用起的最长墙钟时间
    rss_limit_mb: int | None = 2048  # 进程组 RSS 合计上限（看门狗）
    nofile: int | None = 4096  # RLIMIT_NOFILE
    cpu_s: int | None = None  # RLIMIT_CPU（每进程累计 CPU 秒）
    nproc: int | None = None  # RLIMIT_NPROC（仅 Linux，按 UID 计）
    tick_s: float = 1.0  # 维护循环（TTL / 会话超时 / RSS 采样）间隔
    close_grace_s: float = 2.0  # release 时 close 帧后等待子进程自行退出的时长，超时 killpg
    env: Mapping[str, str] | None = None  # 追加到子进程环境变量（白名单 INHERIT_ENV 之外，不得含密钥）
    # 密钥：每个 worker 写一份 0600 文件（worker 根目录下），子进程环境只有 <NAME>_FILE=路径；不进 repr
    secret_env: Mapping[str, str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.max_workers < 1 or not 0 <= self.min_idle <= self.max_workers:
            raise ValueError(f"need 0 <= min_idle ({self.min_idle}) <= max_workers ({self.max_workers}) and max_workers >= 1")


WorkerState = Literal["warming", "idle", "leased", "dead"]


class Worker:
    """池中的一个 worker：子进程（进程组组长）及其租用状态。"""

    def __init__(self, proc: asyncio.subprocess.Process, root: str | None = None) -> None:
        self.proc = proc
        self.root = root  # 池分配的 worker 根目录（回收时删除）
        self.pgid = proc.pid  # start_new_session=True：组长 pid 即 pgid
        self.created = time.monotonic()
        self.leased_at: float | None = None
        self.last_active = self.created
        self.busy = False  # 会话正处于 turn 中（空闲超时不计）
        self.state: WorkerState = "warming"
        self.kill_reason: str | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid

    def touch(self) -> None:
        self.last_active = time.monotonic()


async def _send(proc: asyncio.subprocess.Process, frame: dict[str, Any]) -> None:
    assert proc.stdin
    proc.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode())
    await proc.stdin.drain()


async def _recv(proc: asyncio.subprocess.Process) -> dict[str, Any]:
    assert proc.stdout
    line = await proc.stdout.readline()
    if not line:
        raise RuntimeError("harness process closed stdout")
    frame: dict[str, Any] = json.loads(line)
    return frame


def _killpg(pgid: int, sig: int = signal.SIGKILL) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def group_alive(pgid: int) -> bool:
    """进程组里是否还有进程（含尚未回收的僵尸）。"""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _scan_proc() -> list[tuple[int, int, int]]:
    page = os.sysconf("SC_PAGE_SIZE")
    rows: list[tuple[int, int, int]] = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat", encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2 :].split()  # 从第 3 个字段（state）开始
        if len(fields) < 22 or fields[0] == "Z":
            continue
        rows.append((int(d), int(fields[2]), int(fields[21]) * page))
    return rows


def _scan_ps() -> list[tuple[int, int, int]]:
    out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,pgid=,rss=,stat="], capture_output=True, text=True, timeout=10, check=False)
    rows: list[tuple[int, int, int]] = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[3].startswith("Z"):
            continue
        with contextlib.suppress(ValueError):
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]) * 1024))
    return rows


def scan_processes() -> list[tuple[int, int, int]]:
    """全部存活（非僵尸）进程的 (pid, pgid, rss_bytes)。Linux 读 /proc，其他平台调用 ps。"""
    if sys.platform.startswith("linux") and os.path.isdir("/proc"):
        return _scan_proc()
    return _scan_ps()


def group_pids(pgids: Iterable[int]) -> list[int]:
    """给定进程组里仍存活的全部 pid（先用 killpg(pgid, 0) 过滤掉已空的组，组都空时不扫描）。"""
    live = {g for g in pgids if group_alive(g)}
    if not live:
        return []
    return [pid for pid, pgid, _ in scan_processes() if pgid in live]


class WorkerPool:
    """按固定命令启动 worker 的进程池。可被多个 StdioHarness 实例共享；由创建者负责 aclose()。"""

    def __init__(self, cmd: list[str], config: PoolConfig | None = None) -> None:
        self.cmd = list(cmd)
        self.config = config or PoolConfig()
        self._idle: deque[Worker] = deque()
        self._leased: set[Worker] = set()
        self._retiring: dict[Worker, asyncio.Task[None]] = {}
        self._waiters = 0
        self._starting = 0  # 正在启动 / 握手的 worker（含预热中）
        self._warming = 0  # 其中为补足空闲而启动的
        self._cond = asyncio.Condition()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._failures = 0  # 连续预热失败次数（指数退避）
        self._retry_at = 0.0
        self.spawned = 0
        self.reaped: dict[str, int] = {}  # 强制回收原因 → 次数

    # ---- 对外接口 ----

    def stats(self) -> dict[str, Any]:
        return {
            "idle": len(self._idle),
            "leased": len(self._leased),
            "starting": self._starting,
            "retiring": len(self._retiring),
            "total": self._total(),
            "spawned": self.spawned,
            "reaped": dict(self.reaped),
        }

    def idle_pids(self) -> list[int]:
        return [w.pid for w in self._idle]

    async def start(self) -> None:
        """启动维护循环并开始预热（acquire 时也会自动调用）。"""
        if self._closed:
            raise RuntimeError("pool is closed")
        if self._task is None:
            self._task = asyncio.create_task(self._maintain(), name="stdio-pool-maintain")
        self._wake.set()

    async def wait_warm(self, count: int | None = None, timeout_s: float = 30.0) -> None:
        """等到至少 count（默认 min_idle）个热 worker 就绪。"""
        await self.start()
        need = self.config.min_idle if count is None else count
        async with asyncio.timeout(timeout_s), self._cond:
            await self._cond.wait_for(lambda: len(self._idle) >= need)

    async def acquire(self) -> Worker:
        """租出一个 worker（优先热 worker，其次在上限内冷启动，否则等待）；超时抛 PoolExhausted。"""
        await self.start()
        cfg = self.config
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cfg.acquire_timeout_s
        worker: Worker | None = None
        async with self._cond:
            self._waiters += 1
            try:
                while True:
                    if self._closed:
                        raise RuntimeError("pool is closed")
                    worker = self._pop_idle()
                    if worker is not None or self._total() < cfg.max_workers:
                        break
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise PoolExhausted(f"stdio pool exhausted: {cfg.max_workers} workers busy for {cfg.acquire_timeout_s}s")
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(remaining):
                            await self._cond.wait()
                if worker is None:
                    self._starting += 1
            finally:
                self._waiters -= 1
                self._wake.set()
        if worker is None:  # 冷启动（锁外进行，不阻塞其他 acquire / release）
            try:
                worker = await self._spawn()
            finally:
                self._starting -= 1
                if worker is None:
                    await self._notify()
        if self._closed:
            await self.release(worker)
            raise RuntimeError("pool is closed")
        worker.state, worker.leased_at, worker.busy = "leased", time.monotonic(), False
        worker.touch()
        self._leased.add(worker)
        self._wake.set()  # 补热
        return worker

    async def release(self, worker: Worker) -> None:
        """会话结束：close 帧 → 等待自行退出（至多 close_grace_s）→ killpg 整组；随后补热。幂等。"""
        if worker.state == "dead" and worker not in self._retiring:
            return
        await asyncio.shield(self._retire(worker, graceful=True))

    async def kill(self, worker: Worker, reason: str) -> None:
        """强制回收（超时 / 超限）：记录原因，killpg 整组；物理回收完成后释放名额。"""
        if worker.state == "dead" and worker not in self._retiring:
            return
        if worker not in self._retiring:
            worker.kill_reason = reason
            self.reaped[reason] = self.reaped.get(reason, 0) + 1
        await asyncio.shield(self._retire(worker, graceful=False))

    async def aclose(self) -> None:
        """关闭池：停止维护循环，回收全部 worker（含租出中的）。"""
        self._closed = True
        await self._notify()
        tasks = [t for t in (self._task, *self._bg) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        workers = [*self._idle, *self._leased]
        for w in workers:
            self._retire(w, graceful=True)
        await asyncio.shield(asyncio.gather(*self._retiring.values()))

    # ---- 内部 ----

    def _total(self) -> int:
        return len(self._idle) + len(self._leased) + self._starting + len(self._retiring)

    def _retire(self, worker: Worker, *, graceful: bool) -> asyncio.Task[None]:
        if worker in self._retiring:
            return self._retiring[worker]
        self._leased.discard(worker)
        with contextlib.suppress(ValueError):
            self._idle.remove(worker)

        async def finish() -> None:
            try:
                await self._terminate(worker, graceful=graceful)
            finally:
                self._retiring.pop(worker)
                await self._notify()
                self._wake.set()

        task = asyncio.create_task(finish(), name="stdio-pool-retire")
        self._retiring[worker] = task
        return task

    def _pop_idle(self) -> Worker | None:
        while self._idle:
            w = self._idle.popleft()
            if w.proc.returncode is None:
                return w
            self._retire(w, graceful=False)
        return None

    def _spawn_bg(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    def _argv(self) -> list[str]:
        cfg = self.config
        return launcher_argv(self.cmd, nofile=cfg.nofile, cpu_s=cfg.cpu_s, nproc=cfg.nproc)

    def _worker_env(self, root: str) -> dict[str, str]:
        """worker 的环境：白名单继承 + cfg.env + 根目录；密钥写成根目录下的 0600 文件，只传路径。"""
        cfg = self.config
        env = {k: os.environ[k] for k in INHERIT_ENV if k in os.environ} | dict(cfg.env or {}) | {WORKER_DIR_ENV: root}
        for name, value in (cfg.secret_env or {}).items():
            path = os.path.join(root, f".{name.lower()}")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(value)
            env[f"{name}_FILE"] = path
        return env

    async def _spawn(self) -> Worker:
        """启动一个 worker（新进程组、独立根目录）并完成握手。"""
        cfg = self.config
        root = tempfile.mkdtemp(prefix="stdio-worker-")  # 0700
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=self._worker_env(root),
            )
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise
        self.spawned += 1
        worker = Worker(proc, root)
        if cfg.handshake:
            try:
                async with asyncio.timeout(cfg.handshake_timeout_s):
                    await _send(proc, {"frame": "hello"})
                    while (await _recv(proc)).get("frame") != "ready":
                        pass
            except BaseException:
                worker.kill_reason = HANDSHAKE_FAILED
                await self._terminate(worker, graceful=False)
                raise
        return worker

    async def _terminate(self, worker: Worker, *, graceful: bool) -> None:
        if worker.state == "dead":
            return
        worker.state = "dead"
        proc = worker.proc
        if proc.returncode is None:
            if graceful:
                with contextlib.suppress(OSError, RuntimeError):
                    await _send(proc, {"frame": "close"})
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(self.config.close_grace_s):
                        await proc.wait()
            if proc.returncode is None:
                _killpg(worker.pgid)
                await proc.wait()
        _killpg(worker.pgid)  # 组长已退出：回收残留的孙进程
        if proc.stdin is not None:
            proc.stdin.close()
        if worker.root is not None:  # 强制回收时子进程来不及自行清理：整个根目录由池删除
            shutil.rmtree(worker.root, ignore_errors=True)

    async def _warm_one(self) -> None:
        worker: Worker | None = None
        try:
            worker = await self._spawn()
        except Exception as exc:
            self._failures += 1
            delay = min(30.0, 0.5 * 2 ** min(self._failures, 6))
            self._retry_at = time.monotonic() + delay
            log.warning("stdio pool: warm worker failed (%s: %s); retry in %.1fs", type(exc).__name__, exc, delay)
        finally:
            self._starting -= 1
            self._warming -= 1
            if worker is not None:
                if self._closed:
                    await self._terminate(worker, graceful=False)
                else:
                    self._failures = 0
                    worker.state = "idle"
                    self._idle.append(worker)
            await self._notify()

    def _replenish(self) -> None:
        """容量等待者优先取得释放的槽位，其冷启动握手使用独立握手期限。"""
        cfg = self.config
        if self._closed or self._waiters or time.monotonic() < self._retry_at:
            return
        need = min(cfg.min_idle - len(self._idle) - self._warming, cfg.max_workers - self._total())
        for _ in range(max(0, need)):
            self._starting += 1
            self._warming += 1
            self._spawn_bg(self._warm_one())

    async def _enforce(self) -> None:
        cfg = self.config
        now = time.monotonic()
        for w in list(self._idle):
            if w.state != "idle" or w not in self._idle:  # 前一次 kill 的 await 期间可能已被 acquire 租出
                continue
            if w.proc.returncode is not None:
                await self.kill(w, WORKER_EXITED)
            elif cfg.warm_ttl_s is not None and now - w.created > cfg.warm_ttl_s:
                await self.kill(w, WARM_TTL_EXPIRED)
        for w in list(self._leased):
            if w.state != "leased":
                continue
            if cfg.session_wall_timeout_s is not None and w.leased_at is not None and now - w.leased_at > cfg.session_wall_timeout_s:
                await self.kill(w, SESSION_WALL_TIMEOUT)
            elif cfg.session_idle_timeout_s is not None and not w.busy and now - w.last_active > cfg.session_idle_timeout_s:
                await self.kill(w, SESSION_IDLE_TIMEOUT)
        workers = [*self._idle, *self._leased]
        if cfg.rss_limit_mb is None or not workers:
            return
        rows = await asyncio.to_thread(scan_processes)
        rss: dict[int, int] = {}
        for _, pgid, b in rows:
            rss[pgid] = rss.get(pgid, 0) + b
        limit = cfg.rss_limit_mb * 1024 * 1024
        for w in workers:
            if rss.get(w.pgid, 0) > limit and w.state != "dead":
                log.warning("stdio pool: worker pgid %s RSS %d MiB > %d MiB, killing", w.pgid, rss[w.pgid] >> 20, cfg.rss_limit_mb)
                await self.kill(w, RSS_LIMIT_EXCEEDED)

    async def _maintain(self) -> None:
        while not self._closed:
            self._wake.clear()
            try:
                await self._enforce()
                self._replenish()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("stdio pool maintenance failed")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.config.tick_s):
                    await self._wake.wait()
