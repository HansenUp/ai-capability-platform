"""在宿主机以子进程启停 mock 连接器（``python -m mcp_mocks <name>``，只绑 127.0.0.1），供 e2e 用例与 verify-s8 复用。

用法::

    with mock_stack({"MCP_KNOWLEDGE_DEV_AUTH": "true", ...}, log_dir=tmp) as mocks:
        httpx.get(f"{mocks['mail'].url}/_mock/outbox")

``ports`` 缺省为 knowledge 8092 / crm 8093 / mail 8094；测试里传空闲端口避免与本机常驻进程冲突。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from mcp_mocks import PORTS

READY_TIMEOUT_S = 20.0


@dataclass
class RunningMock:
    name: str
    port: int
    proc: subprocess.Popen[bytes]
    log: Path

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def free_port() -> int:
    """取一个本机空闲端口（关闭后立即交给子进程，存在极小竞争窗口，测试可接受）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _wait_ready(mock: RunningMock) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if mock.proc.poll() is not None:
            raise RuntimeError(f"mock {mock.name} exited early (code {mock.proc.returncode}); see {mock.log}")
        try:
            if httpx.get(f"{mock.url}/healthz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"mock {mock.name} not ready on {mock.url}; see {mock.log}")


@contextmanager
def mock_stack(env: dict[str, str], *, log_dir: Path, ports: dict[str, int] | None = None) -> Iterator[dict[str, RunningMock]]:
    """启动全部 mock 并等待就绪；退出时终止子进程。``env`` 叠加在当前环境变量之上。"""
    ports = ports or dict(PORTS)
    log_dir.mkdir(parents=True, exist_ok=True)
    running: dict[str, RunningMock] = {}
    with ExitStack() as stack:
        try:
            for name, port in ports.items():
                log = log_dir / f"mock-{name}.log"
                out = stack.enter_context(log.open("wb"))
                proc = subprocess.Popen(  # noqa: S603 —— 固定参数：本仓库的 mock 入口
                    [sys.executable, "-m", "mcp_mocks", name, "--port", str(port)],
                    env=os.environ | env,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
                running[name] = RunningMock(name, port, proc, log)
            for mock in running.values():
                _wait_ready(mock)
            yield running
        finally:
            for mock in running.values():
                mock.proc.terminate()
            for mock in running.values():
                try:
                    mock.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    mock.proc.kill()
