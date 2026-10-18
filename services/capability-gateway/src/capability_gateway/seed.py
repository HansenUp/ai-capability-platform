"""导入开发种子：``python -m capability_gateway.seed [--force]``。

对 GATEWAY_DATABASE_URL 指向的目录库先执行迁移，再写入 registry.SEED（能力）、SEED_BINDINGS（绑定）与开发连接器
（MCP_TICKET_URL / MCP_KNOWLEDGE_URL / MCP_CRM_URL / MCP_MAIL_URL）。默认只补缺失项、不覆盖管理员的修改；
``--force`` 覆盖已有能力与连接器地址。每条能力写入都会产生 outbox，由运行中的网关同步到 ES（或随后执行 reindex）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from capability_gateway import config
from capability_gateway.catalog import Catalog, ConnectorVersion
from capability_gateway.registry import SEED, SEED_BINDINGS, seed_connectors

log = logging.getLogger("capability_gateway.seed")


async def seed(catalog: Catalog, *, force: bool = False) -> dict[str, int]:
    """写入种子，返回各类写入条数。"""
    counts = {"capabilities": 0, "bindings": 0, "connectors": 0}
    for cap in SEED:
        if force or await catalog.get(cap.id) is None:
            await catalog.upsert(cap)
            counts["capabilities"] += 1
    existing = {(b.agent_id, b.capability_id, b.kind) for b in await catalog.bindings()}
    for b in SEED_BINDINGS:
        if (b.agent_id, b.capability_id, b.kind) not in existing:
            await catalog.put_binding(b)
            counts["bindings"] += 1
    for s in seed_connectors():
        cv = ConnectorVersion(**s)
        if force or await catalog.get_connector(cv.name, cv.version) is None:
            await catalog.put_connector(cv)
            if await catalog.active_connector(cv.name) is None:
                await catalog.activate(cv.name, cv.version)
            counts["connectors"] += 1
    return counts


async def main(force: bool) -> None:
    if not config.DATABASE_URL:
        raise SystemExit("GATEWAY_DATABASE_URL 未设置")
    from capability_gateway.db import SqlCatalog

    catalog = SqlCatalog(config.DATABASE_URL)
    try:
        await catalog.init()
        log.info("seeded: %s", await seed(catalog, force=force))
    finally:
        await catalog.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="覆盖已有能力与连接器")
    asyncio.run(main(ap.parse_args().force))
