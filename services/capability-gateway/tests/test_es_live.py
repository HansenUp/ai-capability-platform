"""真实 Elasticsearch 8.x 上的索引 / 同步 / 混合检索（设置 GATEWAY_TEST_ES_URL 时运行，如 http://localhost:9200）。

使用独立别名 cg-test-capabilities，不碰开发索引；结束时删除测试索引。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest

from capability_gateway import search, state
from capability_gateway.embed import get_embedder
from capability_gateway.policy import Principal
from capability_gateway.registry import SEED

ES = os.environ.get("GATEWAY_TEST_ES_URL")
pytestmark = pytest.mark.skipif(not ES, reason="GATEWAY_TEST_ES_URL not set")
ALIAS, PREFIX = "cg-test-capabilities", "cg-test-capabilities-v1-"


async def _drop_test_indices() -> None:
    """ES 8 默认禁止通配删除（action.destructive_requires_name），逐个按名字删除。"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{ES}/{PREFIX}*")
        for name in r.json() if r.status_code == 200 else []:
            await c.delete(f"{ES}/{name}")


@pytest.fixture
async def es() -> AsyncIterator[search.EsIndex]:
    await _drop_test_indices()
    yield search.EsIndex(ES or "", get_embedder("hash"), alias=ALIAS, prefix=PREFIX)
    await _drop_test_indices()


async def test_live_sync_and_hybrid_search(es: search.EsIndex) -> None:
    await es.ensure()
    await es.ensure()  # 幂等
    idx = search.Indexer(state.catalog, es, get_embedder("hash"))
    assert await idx.sync_once() == len(SEED)
    p = Principal(sub="u", tenant="default")
    r = await search.hybrid_search(state.catalog, es, get_embedder("hash"), "关闭工单", tenant=p.tenant, agent_id="demo", scope=None)
    assert not r.degraded and r.items[0][0].name == "ticket.close_batch"
    r = await search.hybrid_search(state.catalog, es, get_embedder("hash"), "销售商机", tenant=p.tenant, agent_id="demo", scope=["CRM"])
    assert [c.id for c, _ in r.items] == ["cap.crm.opportunity.search"]
    r = await search.hybrid_search(state.catalog, es, get_embedder("hash"), "工单", tenant="t9", agent_id="demo", scope=None)
    assert r.items == []
    name = await search.reindex_all(state.catalog, es, get_embedder("hash"))
    async with httpx.AsyncClient() as c:
        assert list((await c.get(f"{ES}/_alias/{ALIAS}")).json()) == [name]
    r = await search.hybrid_search(state.catalog, es, get_embedder("hash"), "发送邮件", tenant="default", agent_id="x", scope=None)
    assert r.items[0][0].name == "mail.send"
