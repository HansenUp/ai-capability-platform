"""全量重建能力检索索引：``python -m capability_gateway.reindex``。

按当前 EMBED_PROVIDER 新建 ``<别名>-v1-<provider><dim>-<时间戳>``，从目录库（GATEWAY_DATABASE_URL；
未设置时用内存种子，便于本机演示）批量写入全部能力，原子切换别名（ES_INDEX_ALIAS，缺省 ``cg-capabilities``）并删除旧索引。
切换 provider / 维度或 ES 丢失数据后执行；重建期间查询仍走旧索引。
"""

from __future__ import annotations

import asyncio
import logging

from capability_gateway import config
from capability_gateway.catalog import Catalog, MemoryCatalog
from capability_gateway.embed import get_embedder
from capability_gateway.search import EsIndex, reindex_all

log = logging.getLogger("capability_gateway.reindex")


async def main() -> None:
    if not config.ES_URL:
        raise SystemExit("ES_URL 未设置")
    catalog: Catalog
    if config.DATABASE_URL:
        from capability_gateway.db import SqlCatalog

        catalog = SqlCatalog(config.DATABASE_URL)
    else:
        catalog = MemoryCatalog()
    try:
        await catalog.init()
        embedder = get_embedder()
        index = EsIndex(config.ES_URL, embedder, alias=config.ES_ALIAS, prefix=f"{config.ES_ALIAS}-v1-")
        name = await reindex_all(catalog, index, embedder)
        log.info("alias %s -> %s", config.ES_ALIAS, name)
    finally:
        await catalog.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
