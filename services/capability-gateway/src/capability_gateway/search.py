"""能力检索（S8.1 / S8.2 二级发现，D123）：ES 索引、outbox → ES 同步、混合检索与降级。

索引：``cg-capabilities-v1-<provider><dim>``（reindex 时追加 ``-<时间戳>``），别名 ``cg-capabilities``（查询与写入都经别名）。
name / description 用 ES 内置 ``cjk`` 分析器；tenant / status / system / risk_level / data_class / 绑定 agent 为 keyword；
``embedding`` 为 dense_vector（cosine）。文档版本取能力的 row_version（``version_type=external_gte``），乱序同步不回退。

检索流程（应用侧）：硬过滤（租户、published、discovery.scope、binding=discoverable 含 "*"）→ BM25 top50 + kNN top50
（num_candidates 100）→ RRF(k=60) → 以 MySQL 中的权威数据复核过滤条件（防 ES 滞后）。策略判定在 main 中进行。
- ``ES_URL`` 未配置：在进程内对目录做同样的过滤 + 词元打分 top50 + 向量余弦 top50 + RRF（开发 / 单测），degraded=false；
- ES 已配置但不可用 / 出错：退回目录 + 词元打分（无向量），degraded=true。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from capability_gateway import config
from capability_gateway.catalog import Catalog
from capability_gateway.embed import Embedder, char_bigrams, cosine
from capability_gateway.registry import Binding, Capability

log = logging.getLogger("capability_gateway.search")

ALIAS = "cg-capabilities"
INDEX_PREFIX = "cg-capabilities-v1-"
TOP_K = 50  # BM25 与 kNN 各取前 50
NUM_CANDIDATES = 100
RRF_K = 60
ES_ADMIN_TIMEOUT_S = 60.0


class SearchUnavailable(Exception):
    """ES 不可用或返回异常。"""


def index_base(embedder: Embedder, prefix: str = INDEX_PREFIX) -> str:
    return f"{prefix}{embedder.name}{embedder.dim}"


def index_body(dim: int) -> dict[str, Any]:
    """索引 settings + mappings。"""
    kw = {"type": "keyword"}
    return {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "id": kw,
                "tenant": kw,
                "status": kw,
                "system": kw,
                "risk_level": kw,
                "data_class": kw,
                "discoverable_agents": kw,
                "core_agents": kw,
                "row_version": {"type": "long"},
                "name": {"type": "text", "analyzer": "cjk", "fields": {"keyword": kw}},
                "description": {"type": "text", "analyzer": "cjk"},
                "embedding": {"type": "dense_vector", "dims": dim, "index": True, "similarity": "cosine"},
            },
        },
    }


def build_doc(cap: Capability, bindings: Iterable[Binding], vector: list[float]) -> dict[str, Any]:
    bs = list(bindings)
    return {
        "id": cap.id,
        "tenant": cap.tenant,
        "status": cap.status,
        "system": cap.system,
        "risk_level": cap.risk_level,
        "data_class": cap.data_class,
        "discoverable_agents": sorted({b.agent_id for b in bs if b.kind == "discoverable"}),
        "core_agents": sorted({b.agent_id for b in bs if b.kind == "core"}),
        "row_version": cap.row_version,
        "name": cap.name,
        "description": cap.description,
        "embedding": vector,
    }


def es_filters(tenant: str, agent_id: str, scope: list[str] | None) -> list[dict[str, Any]]:
    """硬过滤：租户、已发布、对该 Agent（或 "*"）可检索、discovery.scope（系统名 / 工具名 / 能力 id 任一命中）。"""
    f: list[dict[str, Any]] = [
        {"term": {"tenant": tenant}},
        {"term": {"status": "published"}},
        {"terms": {"discoverable_agents": sorted({agent_id, "*"})}},
    ]
    if scope:
        should = [{"terms": {"system": scope}}, {"terms": {"name.keyword": scope}}, {"terms": {"id": scope}}]
        f.append({"bool": {"should": should, "minimum_should_match": 1}})
    return f


def in_scope(cap: Capability, scope: list[str] | None) -> bool:
    return not scope or cap.system in scope or cap.name in scope or cap.id in scope


def rrf(rankings: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion：score(d) = Σ 1 / (k + rank)，rank 从 1 起；同分按首次出现顺序。"""
    scores: dict[str, float] = {}
    order: dict[str, int] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
            order.setdefault(doc, len(order))
    return sorted(scores.items(), key=lambda kv: (-kv[1], order[kv[0]]))


def lexical_rank(query: str, caps: list[Capability], top: int = TOP_K) -> list[str]:
    """词元打分（字 2-gram 上的 BM25 近似）：只返回有命中的能力，按分数降序。"""
    q = Counter(char_bigrams(query))
    if not q or not caps:
        return []
    docs = {c.id: Counter(char_bigrams(c.search_text())) for c in caps}
    n = len(docs)
    avg = sum(sum(d.values()) for d in docs.values()) / n or 1.0
    df = Counter(g for d in docs.values() for g in d)
    scored: list[tuple[float, str]] = []
    for cid, d in docs.items():
        length = sum(d.values())
        s = 0.0
        for g in q:
            tf = d.get(g, 0)
            if tf:
                idf = math.log(1 + (n - df[g] + 0.5) / (df[g] + 0.5))
                s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg))
        if s > 0:
            scored.append((s, cid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [cid for _, cid in scored[:top]]


class EsIndex:
    """最小化的 ES REST 客户端（httpx），只用到索引管理、单文档写入、批量与查询。"""

    def __init__(self, url: str, embedder: Embedder, *, alias: str = ALIAS, prefix: str = INDEX_PREFIX) -> None:
        self.url = url.rstrip("/")
        self.embedder = embedder
        self.alias, self.prefix = alias, prefix  # 测试可用独立的别名 / 前缀，避免碰到开发索引

    async def _req(
        self, method: str, path: str, json: Any = None, content: bytes | None = None, ok: tuple[int, ...] = (), admin: bool = False
    ) -> httpx.Response:
        """admin=True：索引管理与后台写入（建索引、分片就绪可能要几秒），超时放宽到 ES_ADMIN_TIMEOUT_S；查询用 ES_TIMEOUT_S。"""
        headers = {"Content-Type": "application/x-ndjson"} if content is not None else None
        timeout = ES_ADMIN_TIMEOUT_S if admin else config.ES_TIMEOUT_S
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=config.transport) as c:
                r = await c.request(method, f"{self.url}{path}", json=json, content=content, headers=headers)
        except httpx.HTTPError as e:
            raise SearchUnavailable(f"elasticsearch unreachable: {type(e).__name__}") from e
        if r.status_code >= 400 and r.status_code not in ok:
            raise SearchUnavailable(f"elasticsearch {method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    async def ensure(self) -> None:
        """别名不存在时创建基础索引并挂别名（幂等）。"""
        r = await self._req("GET", f"/_alias/{self.alias}", ok=(404,))
        if r.status_code == 200:
            return
        name = index_base(self.embedder, self.prefix)
        await self._req("PUT", f"/{name}", json=index_body(self.embedder.dim), ok=(400,), admin=True)  # 400：已存在（并发创建）
        await self._req("POST", "/_aliases", json={"actions": [{"add": {"index": name, "alias": self.alias}}]}, admin=True)

    async def upsert(self, doc: dict[str, Any]) -> None:
        path = f"/{self.alias}/_doc/{doc['id']}?version={doc['row_version']}&version_type=external_gte&refresh=wait_for"
        await self._req("PUT", path, json=doc, ok=(409,), admin=True)  # 409：已有更新的版本，忽略

    async def delete(self, cid: str) -> None:
        await self._req("DELETE", f"/{self.alias}/_doc/{cid}?refresh=wait_for", ok=(404,), admin=True)

    async def search(self, query: str, vector: list[float], filters: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        """BM25 top50 与 kNN top50 两路召回（同一组硬过滤），返回两路的能力 id 排名。"""
        bm25 = {
            "size": TOP_K,
            "_source": False,
            "query": {"bool": {"filter": filters, "must": {"multi_match": {"query": query, "fields": ["name^2", "description"]}}}},
        }
        knn = {
            "size": TOP_K,
            "_source": False,
            "knn": {"field": "embedding", "query_vector": vector, "k": TOP_K, "num_candidates": NUM_CANDIDATES, "filter": filters},
        }
        r1, r2 = await asyncio.gather(
            self._req("POST", f"/{self.alias}/_search", json=bm25), self._req("POST", f"/{self.alias}/_search", json=knn)
        )
        try:
            return [h["_id"] for h in r1.json()["hits"]["hits"]], [h["_id"] for h in r2.json()["hits"]["hits"]]
        except (ValueError, KeyError, TypeError) as e:
            raise SearchUnavailable("elasticsearch returned malformed search response") from e

    async def reindex(self, docs: list[dict[str, Any]]) -> str:
        """全量重建：新建 ``<base>-<时间戳>`` 索引、批量写入、原子切换别名、删除旧的同前缀索引。返回新索引名。"""
        import json as _json

        name = f"{index_base(self.embedder, self.prefix)}-{int(time.time())}"
        await self._req("PUT", f"/{name}", json=index_body(self.embedder.dim), admin=True)
        if docs:
            lines: list[str] = []
            for d in docs:
                meta = {"index": {"_index": name, "_id": d["id"], "version": d["row_version"], "version_type": "external_gte"}}
                lines += [_json.dumps(meta), _json.dumps(d, ensure_ascii=False)]
            r = await self._req("POST", "/_bulk?refresh=true", content=("\n".join(lines) + "\n").encode(), admin=True)
            if r.json().get("errors"):
                raise SearchUnavailable("bulk indexing reported errors")
        r = await self._req("GET", f"/_alias/{self.alias}", ok=(404,))
        old = list(r.json()) if r.status_code == 200 else []
        actions: list[dict[str, Any]] = [{"remove": {"index": o, "alias": self.alias}} for o in old]
        actions.append({"add": {"index": name, "alias": self.alias}})
        await self._req("POST", "/_aliases", json={"actions": actions}, admin=True)
        for o in old:
            if o != name and o.startswith(self.prefix):
                await self._req("DELETE", f"/{o}", ok=(404,), admin=True)
        return name


async def doc_for(catalog: Catalog, embedder: Embedder, cap: Capability) -> dict[str, Any]:
    (vec,) = await embedder.embed([cap.search_text()])
    return build_doc(cap, await catalog.bindings(capability_id=cap.id), vec)


class Indexer:
    """outbox → ES 同步。ES 未配置时只确认（丢弃）事件，避免 outbox 无限增长（之后需 reindex）。"""

    def __init__(self, catalog: Catalog, es: EsIndex | None, embedder: Embedder) -> None:
        self.catalog, self.es, self.embedder = catalog, es, embedder

    async def sync_once(self, batch: int = 100) -> int:
        """处理一批 outbox 事件，返回已确认条数。ES 出错时停在出错事件之前（下次重试）。"""
        events = await self.catalog.outbox_fetch(batch)
        done: list[int] = []
        try:
            for ev in events:
                if self.es is not None:
                    cap = await self.catalog.get(ev.aggregate_id)
                    if cap is None or ev.op == "delete":
                        await self.es.delete(ev.aggregate_id)
                    else:
                        await self.es.upsert(await doc_for(self.catalog, self.embedder, cap))
                done.append(ev.id)
        finally:
            await self.catalog.outbox_ack(done)
        return len(done)

    async def run(self, interval_s: float) -> None:
        while True:
            try:
                if self.es is not None:
                    await self.es.ensure()
                while await self.sync_once():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 —— 后台任务不能因单次失败退出
                log.warning("capability indexer: %s", e)
            await asyncio.sleep(interval_s)


@dataclass
class Ranked:
    """检索结果：按 RRF 分数排序的（能力, 分数），以及是否降级。"""

    items: list[tuple[Capability, float]]
    degraded: bool


async def _candidates(catalog: Catalog, tenant: str, agent_id: str, scope: list[str] | None) -> list[Capability]:
    allowed = await catalog.bound_ids(agent_id, "discoverable")
    return [c for c in await catalog.list_published() if c.tenant == tenant and c.id in allowed and in_scope(c, scope)]


_vec_cache: dict[tuple[str, str, int], list[float]] = {}


async def _local_vectors(embedder: Embedder, caps: list[Capability]) -> list[list[float]]:
    """进程内检索用的文档向量（按 能力 id + row_version 缓存）。"""
    missing = [c for c in caps if (embedder.name, c.id, c.row_version) not in _vec_cache]
    if missing:
        for c, v in zip(missing, await embedder.embed([c.search_text() for c in missing]), strict=True):
            _vec_cache[(embedder.name, c.id, c.row_version)] = v
    return [_vec_cache[(embedder.name, c.id, c.row_version)] for c in caps]


async def hybrid_search(
    catalog: Catalog, es: EsIndex | None, embedder: Embedder, query: str, *, tenant: str, agent_id: str, scope: list[str] | None
) -> Ranked:
    """混合检索 + RRF；ES 不可用时退回词元打分并标 degraded。"""
    if es is not None:
        try:
            (qvec,) = await embedder.embed([query])
            bm25, knn = await es.search(query, qvec, es_filters(tenant, agent_id, scope))
            fused = rrf([bm25, knn])
            caps = await catalog.get_many([cid for cid, _ in fused])
            allowed = await catalog.bound_ids(agent_id, "discoverable")
            items = [
                (caps[cid], score)
                for cid, score in fused
                if cid in caps
                and caps[cid].status == "published"
                and caps[cid].tenant == tenant
                and cid in allowed
                and in_scope(caps[cid], scope)
            ]
            return Ranked(items, degraded=False)
        except Exception as e:  # noqa: BLE001 —— ES 或向量 provider 任何失败都退回目录词元打分
            log.warning("capability search degraded to catalog lexical scoring: %s", e)
            cands = await _candidates(catalog, tenant, agent_id, scope)
            by_id = {c.id: c for c in cands}
            fused = rrf([lexical_rank(query, cands)])
            return Ranked([(by_id[cid], s) for cid, s in fused], degraded=True)
    cands = await _candidates(catalog, tenant, agent_id, scope)
    by_id = {c.id: c for c in cands}
    lex = lexical_rank(query, cands)
    try:
        (qvec,) = await embedder.embed([query])
        vecs = await _local_vectors(embedder, cands)
    except Exception as e:  # noqa: BLE001 —— 向量 provider 不可用（未安装 fastembed、模型网关不可达）：只用词元打分
        log.warning("capability search without vectors (embedder %s failed): %s", embedder.name, e)
        return Ranked([(by_id[cid], s) for cid, s in rrf([lex])], degraded=True)
    sims = sorted(((cosine(qvec, v), c.id) for c, v in zip(cands, vecs, strict=True)), key=lambda x: (-x[0], x[1]))
    knn = [cid for _, cid in sims[:TOP_K]]
    fused = rrf([lex, knn])
    return Ranked([(by_id[cid], s) for cid, s in fused], degraded=False)


async def reindex_all(catalog: Catalog, es: EsIndex, embedder: Embedder) -> str:
    """全量重建索引（python -m capability_gateway.reindex）。"""
    caps = await catalog.list_all()
    docs = [await doc_for(catalog, embedder, c) for c in caps]
    return await es.reindex(docs)


async def stop_task(task: asyncio.Task[Any] | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
