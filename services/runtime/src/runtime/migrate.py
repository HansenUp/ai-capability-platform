"""独立迁移入口（S7，D115）：`python -m runtime.migrate` 把 DATABASE_URL 指向的引擎库升级到最新迁移。

多节点部署时设 RUNTIME_MIGRATE_ON_START=false 关闭启动迁移，改由本入口作为独立 Job 执行；
MySQL 下与启动迁移共用 GET_LOCK('ai_runtime_migrate') 串行化，二者同时发生也只迁移一次。
"""

from __future__ import annotations

import asyncio
import logging

from runtime.state import db


async def main() -> None:
    """执行迁移（不看 RUNTIME_MIGRATE_ON_START），结束后释放连接池。"""
    try:
        await db.migrate()
    finally:
        await db.engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
    logging.getLogger(__name__).info("runtime 引擎库已升级到最新迁移")
