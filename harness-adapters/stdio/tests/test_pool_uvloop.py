"""进程池在 uvloop 事件循环下可用（S6 集成演练发现）：Runtime 以 uvicorn 运行，默认事件循环是 uvloop，其
subprocess_exec 不支持 ``process_group`` 参数（ValueError: unexpected kwargs）——goose / dsh 的第一个 Run 即失败。
池改用 ``start_new_session=True``（新会话兼新进程组，pgid = 子进程 pid），asyncio 与 uvloop 均支持。"""

from __future__ import annotations

import os
import sys

import pytest

from ai_harness_stdio import PoolConfig, WorkerPool
from ai_harness_stdio.pool import group_alive

uvloop = pytest.importorskip("uvloop")

# 认识 hello 帧的最小 harness：派生一个孙进程，便于确认进程组整组回收
FAKE = r"""
import json, subprocess, sys
kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL)
for line in sys.stdin:
    if json.loads(line)["frame"] == "hello":
        sys.stdout.write(json.dumps({"frame": "ready"}) + "\n"); sys.stdout.flush()
"""


def test_pool_spawns_process_groups_under_uvloop() -> None:
    async def main() -> None:
        pool = WorkerPool([sys.executable, "-c", FAKE], PoolConfig(min_idle=0, max_workers=2, tick_s=0.05, nofile=None))
        try:
            worker = await pool.acquire()
            assert os.getpgid(worker.pid) == worker.pgid == worker.pid  # 自成进程组
            assert os.getpgid(worker.pid) != os.getpgid(0)
            await pool.release(worker)
            assert not group_alive(worker.pgid)  # 组长与孙进程一并回收
        finally:
            await pool.aclose()

    uvloop.run(main())
