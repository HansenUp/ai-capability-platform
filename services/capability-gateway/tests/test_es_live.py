"""真实 Elasticsearch 8.x 上的索引 / 同步 / 混合检索（设置 GATEWAY_TEST_ES_URL 时运行，如 http://localhost:9200）。

使用每轮独立别名与已确认所有权账本；结束时仅精确清理本轮索引。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest
from es_resource_testkit import owned_es

from capability_gateway import search, state
from capability_gateway.embed import get_embedder
from capability_gateway.policy import Principal
from capability_gateway.registry import SEED

ES = os.environ.get("GATEWAY_TEST_ES_URL")
pytestmark = pytest.mark.skipif(not ES, reason="GATEWAY_TEST_ES_URL not set")


@pytest.fixture
async def es() -> AsyncIterator[search.EsIndex]:
    async with owned_es(ES or "", get_embedder("hash")) as index:
        yield index


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
        assert list((await c.get(f"{ES}/_alias/{es.alias}")).json()) == [name]
    r = await search.hybrid_search(state.catalog, es, get_embedder("hash"), "发送邮件", tenant="default", agent_id="x", scope=None)
    assert r.items[0][0].name == "mail.send"
