"""纯 Worker 进程入口：``python -m runtime.worker``（RUNTIME_ROLE 强制为 worker，须配置 REDIS_URL）。

启动顺序同 API 节点的 lifespan：安全基线校验 → Tracing → 迁移（RUNTIME_MIGRATE_ON_START=false 时跳过）→
默认 harness 可加载 → 启动 Worker / reaper / 取消订阅；SIGTERM / SIGINT 时取消在途 Run 并释放租约后退出
（未 ACK 的消息由其他 Worker 接管）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import signal

from runtime.config import settings, validate_for_production
from runtime.harness import registry
from runtime.observe import tracing
from runtime.state.db import init_db
from runtime.worker.settings import validate, worker_settings
from runtime.worker.worker import start_background

logger = logging.getLogger("runtime.worker")


async def main() -> None:
    ws = dataclasses.replace(worker_settings, role="worker")
    validate_for_production(settings)
    validate(ws, settings.runtime_env)
    tracing.setup_tracing()
    await init_db()
    registry.get(settings.harness)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    bg = await start_background(ws)
    logger.info("runtime worker started (node=%s, concurrency=%d)", settings.node_id, ws.concurrency)
    try:
        await stop.wait()
    finally:
        await bg.stop()
        tracing.force_flush()
        logger.info("runtime worker stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
