"""二级发现：hash 向量确定性、RRF、硬过滤（租户 / 绑定 / scope）、策略剔除与 3×limit、ES 路径、outbox 同步、降级。"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from gateway_testkit import ES_URL, Upstream

from capability_gateway import main, search, state
from capability_gateway.catalog import MemoryCatalog
from capability_gateway.embed import HashEmbedder, char_bigrams, cosine, get_embedder
from capability_gateway.registry import SEED, Binding, Capability

c = TestClient(main.app)


def _caps(n: int = 8) -> list[Capability]:
    return [Capability(id=f"cap.t.{i}", name=f"t.{i}", description=f"工单 操作 {i}", system="ticket") for i in range(n)]


def test_hash_embedding_is_deterministic_and_normalized() -> None:
    e = HashEmbedder()
    v1, v2 = e.vector("查询售后工单"), HashEmbedder().vector("查询售后工单")
    assert v1 == v2 and len(v1) == 384 and abs(sum(x * x for x in v1) - 1) < 1e-9
    assert char_bigrams("查询 工单！AB") == ["查询", "工单", "ab"]
    assert cosine(v1, e.vector("售后工单查询")) > cosine(v1, e.vector("发送邮件")) >= -1
    assert e.vector("") == [1.0] + [0.0] * 383  # 空文本：非零向量（ES cosine 拒绝零向量）
    assert get_embedder("hash").name == "hash" and get_embedder("hash").dim == 384


def test_rrf_fuses_rankings() -> None:
    fused = search.rrf([["a", "b", "c"], ["c", "a"]], k=60)
    assert [d for d, _ in fused] == ["a", "c", "b"]
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62) and fused[2][1] == pytest.approx(1 / 62)


def test_search_filters_tenant_binding_and_scope(up: Upstream) -> None:
    caps = [
        *SEED,
        Capability(id="cap.t2.ticket", name="t2.ticket.search", description="查询工单", system="ticket", tenant="t2"),
        Capability(id="cap.ticket.draft", name="ticket.draft", description="查询工单草稿", system="ticket", status="draft"),
        Capability(id="cap.ticket.unbound", name="ticket.unbound", description="查询工单", system="ticket"),
    ]
    binds = [Binding(agent_id="a1", capability_id=x.id, kind="discoverable") for x in caps if x.id != "cap.ticket.unbound"]
    binds.append(Binding(agent_id="a1", capability_id="cap.ticket.unbound", kind="core"))  # core 不进二级检索
    state.catalog = MemoryCatalog(caps, binds)
    r = c.post("/capabilities/search", json={"query": "查询工单", "agent_id": "a1", "limit": 20}).json()
    ids = {i["id"] for i in r["items"]}
    assert {"cap.ticket.search", "cap.ticket.get"} <= ids
    assert not ids & {"cap.t2.ticket", "cap.ticket.draft", "cap.ticket.unbound"}
    r = c.post("/capabilities/search", json={"query": "查询工单", "agent_id": "a1", "scope": ["CRM", "mail.send"], "limit": 20}).json()
    assert {i["id"] for i in r["items"]} == {"cap.crm.opportunity.search", "cap.mail.send"}
    r = c.post("/capabilities/search", json={"query": "查询工单", "agent_id": "a1"}, headers={"X-AI-Tenant": "t2"}).json()
    assert [i["id"] for i in r["items"]] == ["cap.t2.ticket"]
    assert c.post("/capabilities/search", json={"query": "工单", "agent_id": "nobody"}).json()["items"] == []


def test_policy_filters_after_rrf_with_3x_limit(up: Upstream) -> None:
    caps = _caps(10)
    state.catalog = MemoryCatalog(caps, [Binding(agent_id="*", capability_id=x.id, kind="discoverable") for x in caps])
    first = c.post("/capabilities/search", json={"query": "工单操作", "agent_id": "a", "limit": 2}).json()["items"]
    assert up.policy_batches == [6]  # 3 × limit 交策略批量判定
    up.decisions[first[0]["id"]] = "deny"
    up.decisions[first[1]["id"]] = "require_approval"
    r = c.post("/capabilities/search", json={"query": "工单操作", "agent_id": "a", "limit": 2, "taint": ["confidential"]}).json()
    assert r["items"][0]["id"] == first[1]["id"]
    assert first[0]["id"] not in {i["id"] for i in r["items"]} and len(r["items"]) == 2
    assert r["items"][0]["decision"] == "require_approval"
    assert up.policy[-1]["context"]["taint"] == ["confidential"] and up.policy[-1]["principal"]["tenant"] == "default"
    up.policy_down = True  # 策略不可达：全部 deny，不返回任何能力
    assert c.post("/capabilities/search", json={"query": "工单操作", "agent_id": "a"}).json()["items"] == []


async def test_outbox_sync_and_es_hybrid_search(up: Upstream, monkeypatch: pytest.MonkeyPatch) -> None:
    """outbox → Indexer → ES（别名 + external 版本）；查询带硬过滤、BM25 / kNN 各 50、num_candidates 100。"""
    es = search.EsIndex(ES_URL, get_embedder("hash"))
    monkeypatch.setattr(state, "es", es)
    idx = search.Indexer(state.catalog, es, get_embedder("hash"))
    await es.ensure()
    assert up.es.aliases["cg-capabilities"] == {"cg-capabilities-v1-hash384"}
    n = await idx.sync_once()
    assert n == len(SEED) and await state.catalog.outbox_fetch() == []
    doc = up.es.indices["cg-capabilities-v1-hash384"]["cap.ticket.close_batch"]
    assert doc["discoverable_agents"] == ["*"] and doc["core_agents"] == ["*", "demo"] and len(doc["embedding"]) == 384
    # 变更：row_version +1 并重新同步；旧版本写入被 ES 以 409 忽略
    await state.catalog.put_binding(Binding(agent_id="a9", capability_id="cap.ticket.get", kind="discoverable"))
    await idx.sync_once()
    assert up.es.indices["cg-capabilities-v1-hash384"]["cap.ticket.get"]["discoverable_agents"] == ["*", "a9"]
    stale = dict(doc, row_version=0, description="stale")
    await es.upsert(stale)
    assert up.es.indices["cg-capabilities-v1-hash384"]["cap.ticket.close_batch"]["description"] != "stale"

    r = c.post("/capabilities/search", json={"query": "关闭工单", "agent_id": "demo", "scope": ["ticket"]}).json()
    assert r["degraded"] is False and r["items"][0]["name"] == "ticket.close_batch"
    bm25, knn = up.es.searches[-2:]
    filters = bm25["query"]["bool"]["filter"]
    assert {"term": {"tenant": "default"}} in filters and {"term": {"status": "published"}} in filters
    assert {"terms": {"discoverable_agents": ["*", "demo"]}} in filters and knn["knn"]["filter"] == filters
    assert bm25["size"] == 50 and knn["knn"]["k"] == 50 and knn["knn"]["num_candidates"] == 100
    # 删除能力（outbox delete）同步到 ES
    await state.catalog.upsert(SEED[0].model_copy(update={"status": "retired"}))
    await idx.sync_once()
    r = c.post("/capabilities/search", json={"query": "查询工单", "agent_id": "demo"}).json()
    assert "cap.ticket.search" not in {i["id"] for i in r["items"]}  # retired：ES 文档 status 已更新，被硬过滤


async def test_es_unavailable_degrades_to_lexical(up: Upstream, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "es", search.EsIndex(ES_URL, get_embedder("hash")))
    up.es.down = True
    r = c.post("/capabilities/search", json={"query": "查询 销售 商机", "agent_id": "demo"}).json()
    assert r["degraded"] is True and r["items"][0]["name"] == "crm.opportunity.search"
    assert all(i["score"] > 0 for i in r["items"])  # 只有词元命中的能力


async def test_indexer_stops_at_es_error_and_retries(up: Upstream) -> None:
    es = search.EsIndex(ES_URL, get_embedder("hash"))
    await es.ensure()
    idx = search.Indexer(state.catalog, es, get_embedder("hash"))
    up.es.down = True
    with pytest.raises(search.SearchUnavailable):
        await idx.sync_once()
    assert len(await state.catalog.outbox_fetch(1000)) == len(SEED)  # 未确认，下次重试
    up.es.down = False
    assert await idx.sync_once() == len(SEED)


async def test_indexer_without_es_drains_outbox() -> None:
    idx = search.Indexer(state.catalog, None, get_embedder("hash"))
    assert await idx.sync_once() == len(SEED) and await state.catalog.outbox_fetch() == []


async def test_reindex_switches_alias(up: Upstream) -> None:
    es = search.EsIndex(ES_URL, get_embedder("hash"))
    await es.ensure()
    name = await search.reindex_all(state.catalog, es, get_embedder("hash"))
    assert name.startswith("cg-capabilities-v1-hash384-") and up.es.aliases["cg-capabilities"] == {name}
    assert "cg-capabilities-v1-hash384" not in up.es.indices and len(up.es.indices[name]) == len(SEED)


@pytest.mark.skipif(not os.environ.get("GATEWAY_TEST_FASTEMBED"), reason="GATEWAY_TEST_FASTEMBED not set")
async def test_fastembed_bge_small_zh() -> None:
    """本机：fastembed + bge-small-zh-v1.5（512 维），语义相近的中文表述更相似。"""
    from capability_gateway.embed import FastEmbedEmbedder

    a, b, other = await FastEmbedEmbedder().embed(["批量关闭工单并通知客户", "把这些售后单都结掉", "发送邮件（外发）"])
    assert len(a) == 512 and cosine(a, b) > cosine(a, other)
