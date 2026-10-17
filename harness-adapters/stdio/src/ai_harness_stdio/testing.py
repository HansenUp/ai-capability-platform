"""stdio 适配器的契约探针工厂（S5 §1.3）：真实 StdioHarness + 脚本模式的 echo harness 子进程。

假模型在子进程里：脚本写入临时目录的 script.json，每次模型调用收到的历史由子进程追加到 calls.ndjson，
调用序号即该文件行数，因此跨 restart()（新适配器、新子进程）连续。需要可选依赖 ai-harness-stdio[probe]。
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ai_contract import HarnessAdapter
from ai_harness_probe import ModelScript, ProbeTarget

from . import StdioHarness


class StdioProbeTarget:
    """ProbeTarget：当前 StdioHarness 实例 + calls.ndjson 中的模型调用记录 + 全部实例的残留子进程。"""

    def __init__(self, cmd: list[str], calls: Path) -> None:
        self.cmd = cmd
        self.calls = calls
        self.instances: list[StdioHarness] = [StdioHarness(cmd, name="stdio")]

    @property
    def adapter(self) -> HarnessAdapter:
        return self.instances[-1]

    def received(self) -> list[list[dict[str, Any]]]:
        lines = self.calls.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def leaks(self) -> list[str]:
        return [f"stdio harness subprocess pid {pid} still running" for h in self.instances for pid in h.running_pids()]

    async def restart(self) -> HarnessAdapter:
        await self.instances[-1].close_all()  # 模拟平台进程退出：旧实例的子进程随之结束
        self.instances.append(StdioHarness(self.cmd, name="stdio"))
        return self.instances[-1]

    async def aclose(self) -> None:
        for h in self.instances:
            await h.close_all()


@asynccontextmanager
async def probe_factory(script: ModelScript) -> AsyncIterator[ProbeTarget]:
    """stdio 的探针工厂：echo harness 脚本模式子进程，退出时关闭全部子进程并删除临时目录。"""
    with tempfile.TemporaryDirectory(prefix="stdio-probe-") as tmp:
        script_path, calls = Path(tmp) / "script.json", Path(tmp) / "calls.ndjson"
        script_path.write_text(script.model_dump_json(), encoding="utf-8")
        calls.touch()
        cmd = [sys.executable, "-m", "ai_harness_stdio.echo_harness", "--script", str(script_path), "--calls", str(calls)]
        target = StdioProbeTarget(cmd, calls)
        try:
            yield target
        finally:
            await target.aclose()
