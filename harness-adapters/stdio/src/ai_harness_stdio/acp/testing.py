"""ACP 桥的契约探针工厂（S5 §1.3 约定）：真实 StdioHarness + 真实 ACP 桥子进程 + 真实（或 fake）ACP agent，
只有模型是假的——桥以 ``--script`` 启动 shim 的脚本模式（探针假模型），每次模型调用"实际看到"的消息由 shim
反向规范化后追加到 calls.ndjson（调用序号即行数，跨 restart() 连续）。

leaks() 同时检查桥进程与 agent 进程（桥把 agent pid 记在 ``--pid-dir``）。需要 ``ai-harness-stdio[acp,probe]``。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ai_contract import HarnessAdapter
from ai_harness_probe import ModelScript, ProbeTarget

from .. import PoolConfig, WorkerPool
from .bridge import AcpStdioHarness, bridge_command


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AcpProbeTarget:
    """ProbeTarget：当前 AcpStdioHarness 实例 + calls.ndjson + 全部桥 / agent 进程的残留检查。"""

    def __init__(self, profile: str, args: list[str], calls: Path, pid_dir: Path, name: str | None, pool: WorkerPool | None = None) -> None:
        self.profile, self.args, self.calls, self.pid_dir, self.name = profile, args, calls, pid_dir, name
        self.pool = pool  # 池模式：各实例共享同一个池（热 worker 及其 agent 不计入泄漏）
        self.instances: list[AcpStdioHarness] = [self._make()]

    def _make(self) -> AcpStdioHarness:
        kwargs: dict[str, Any] = {"name": self.name} if self.name else {}
        return AcpStdioHarness(self.profile, *self.args, pool=self.pool, **kwargs)

    @property
    def adapter(self) -> HarnessAdapter:
        return self.instances[-1]

    def received(self) -> list[list[dict[str, Any]]]:
        lines = self.calls.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def leaks(self) -> list[str]:
        out = [f"acp bridge pid {pid} still running" for h in self.instances for pid in h.running_pids()]
        for f in sorted(self.pid_dir.glob("agent-*.pid")):
            pid = int(f.read_text() or 0)
            if pid and _alive(pid) and not self._warm(pid):
                out.append(f"acp agent pid {pid} still running")
        return out

    def _warm(self, pid: int) -> bool:
        """池里尚未租出的热 worker 的 agent（与桥同一进程组）。"""
        if self.pool is None:
            return False
        try:
            return os.getpgid(pid) in self.pool.idle_pids()
        except ProcessLookupError:
            return False

    async def restart(self) -> HarnessAdapter:
        await self.instances[-1].close_all()
        self.instances.append(self._make())
        return self.instances[-1]

    async def aclose(self) -> None:
        for h in self.instances:
            await h.close_all()
        if self.pool is not None:
            await self.pool.aclose()


@asynccontextmanager
async def acp_probe_factory(
    script: ModelScript,
    *,
    profile: str,
    agent_argv: Sequence[str] = (),
    name: str | None = None,
    pool_config: PoolConfig | None = None,
) -> AsyncIterator[ProbeTarget]:
    """通用探针工厂：``profile`` 为 goose / dsh / cmd（cmd 时 agent_argv 为 agent 命令）；给出 pool_config 时经
    WorkerPool 运行（hello→ready 预热握手、进程组回收）。退出时回收全部进程与临时目录。"""
    with tempfile.TemporaryDirectory(prefix="acp-probe-") as tmp:
        script_path, calls, pid_dir = Path(tmp) / "script.json", Path(tmp) / "calls.ndjson", Path(tmp) / "pids"
        script_path.write_text(script.model_dump_json(), encoding="utf-8")
        calls.touch()
        pid_dir.mkdir()
        args = ["--script", str(script_path), "--calls", str(calls), "--pid-dir", str(pid_dir)]
        if agent_argv:
            args += ["--", *agent_argv]
        pool = WorkerPool(bridge_command(profile, *args), pool_config) if pool_config is not None else None
        target = AcpProbeTarget(profile, args, calls, pid_dir, name, pool)
        try:
            yield target
        finally:
            await target.aclose()
