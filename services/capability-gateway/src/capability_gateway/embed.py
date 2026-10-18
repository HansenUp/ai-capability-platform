"""能力检索的向量 provider 抽象（D124）：``EMBED_PROVIDER=hash|fastembed|litellm``。

- hash（CI 默认）：字 2-gram 特征哈希到 384 维并 L2 归一化。完全确定（不依赖 Python 的随机化 hash），无需模型；
  中文按字切分天然适配，语义能力弱，只作为可复现的基线。
- fastembed（本机）：``BAAI/bge-small-zh-v1.5``（512 维，ONNX，CPU），首次使用时懒加载，推理放到线程池；
  需要可选依赖 ``pip install ai-capability-gateway[fastembed]``，模型缓存目录可用 ``FASTEMBED_CACHE_PATH`` 指定。
- litellm（生产）：经模型网关 ``POST {MODEL_GATEWAY_URL}/v1/embeddings``（模型别名 ``EMBED_MODEL``，默认 embed-default），
  维度 ``EMBED_DIM``（默认 1024，作为 dimensions 参数下发）。

索引名带 provider 与维度（``cg-capabilities-v1-<provider><dim>``），切换 provider 后需要 ``python -m capability_gateway.reindex``。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import threading
import unicodedata
from typing import Any, Protocol

import httpx

from capability_gateway import config


class Embedder(Protocol):
    """文本向量化接口。"""

    name: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _runs(text: str) -> list[str]:
    """按非字母数字字符切成连续片段（中文字符属于字母类，保留），统一小写与全角 → 半角。"""
    s = unicodedata.normalize("NFKC", text).lower()
    out: list[str] = []
    cur: list[str] = []
    for ch in s:
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def char_bigrams(text: str) -> list[str]:
    """字 2-gram：每个连续片段内相邻两字一组；单字片段保留为 1-gram。"""
    grams: list[str] = []
    for run in _runs(text):
        if len(run) == 1:
            grams.append(run)
        else:
            grams.extend(run[i : i + 2] for i in range(len(run) - 1))
    return grams


class HashEmbedder:
    """字 2-gram 特征哈希（signed feature hashing），确定性 384 维。"""

    name = "hash"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def vector(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for g in char_bigrams(text):
            d = hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(d[:4], "little") % self.dim
            v[idx] += 1.0 if d[4] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v))
        if norm == 0:  # 空文本：ES 的 cosine 不接受零向量，放一个固定方向
            v[0] = 1.0
            return v
        return [x / norm for x in v]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.vector(t) for t in texts]


class FastEmbedEmbedder:
    """fastembed + bge-small-zh-v1.5（本机）。模型首次使用时加载（线程安全），推理在线程池执行不阻塞事件循环。"""

    name = "fastembed"

    def __init__(self, model: str | None = None, dim: int = 512) -> None:
        self.model_name = model or os.environ.get("FASTEMBED_MODEL", "BAAI/bge-small-zh-v1.5")
        self.dim = dim
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding  # 可选依赖：ai-capability-gateway[fastembed]

                kwargs: dict[str, Any] = {}
                if os.environ.get("FASTEMBED_CACHE_PATH"):
                    kwargs["cache_dir"] = os.environ["FASTEMBED_CACHE_PATH"]
                self._model = TextEmbedding(self.model_name, **kwargs)
            return self._model

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in e] for e in self._load().embed(texts)]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._embed_sync, texts)


class LiteLLMEmbedder:
    """经模型网关（LiteLLM，OpenAI 兼容）计算向量（生产）。"""

    name = "litellm"

    def __init__(self) -> None:
        self.base_url = os.environ.get("MODEL_GATEWAY_URL", "http://localhost:4000").rstrip("/")
        self.model = os.environ.get("EMBED_MODEL", "embed-default")
        self.dim = int(os.environ.get("EMBED_DIM", "1024"))
        self.api_key = os.environ.get("MODEL_GATEWAY_API_KEY", "")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "input": texts, "dimensions": self.dim}
        async with httpx.AsyncClient(timeout=10.0, transport=config.transport) as c:
            r = await c.post(f"{self.base_url}/v1/embeddings", json=body, headers=headers)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
        out = [[float(x) for x in d["embedding"]] for d in data]
        if any(len(v) != self.dim for v in out):
            raise ValueError(f"embedding dimension mismatch: expected {self.dim}")
        return out


_cache: dict[str, Embedder] = {}


def get_embedder(provider: str | None = None) -> Embedder:
    """按 EMBED_PROVIDER 返回（进程内单例的）向量 provider。"""
    p = (provider or os.environ.get("EMBED_PROVIDER") or "hash").strip().lower()
    if p not in _cache:
        if p == "hash":
            _cache[p] = HashEmbedder()
        elif p == "fastembed":
            _cache[p] = FastEmbedEmbedder()
        elif p == "litellm":
            _cache[p] = LiteLLMEmbedder()
        else:
            raise ValueError(f"unknown EMBED_PROVIDER: {p}")
    return _cache[p]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（向量不要求已归一化）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
