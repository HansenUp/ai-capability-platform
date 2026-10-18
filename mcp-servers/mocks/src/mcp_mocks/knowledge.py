"""knowledge mock（8092）：``knowledge.search``，只读。

文档带租户、ACL 与数据分级（``data/knowledge.json``，可用 ``MCP_KNOWLEDGE_DATA`` 换文件）。按调用者过滤：
租户必须一致；ACL 含 ``*``（租户内公开）、``role:<realm 角色>`` 或 ``user:<sub>`` 之一命中才可见。
检索为词元打分（ASCII 词 + 中文字 2-gram，标题权重 3、正文 1），只在可见文档里检索——不可见文档不参与打分、不出现在 total。
响应带 ``data_class``（命中文档的最高分级），供网关 / Runtime 形成数据污点。

鉴权前缀 ``MCP_KNOWLEDGE``，受众默认 ``mcp-knowledge``（见 ``mcp_template.auth``）。
"""

from __future__ import annotations

import json
import os
import re
from functools import cache
from pathlib import Path
from typing import Any

from fastapi import Depends
from pydantic import BaseModel, Field

from mcp_mocks.common import MockConnector
from mcp_template.auth import Identity
from mcp_template.spec import read_tool

DEFAULT_DATA = Path(__file__).parent / "data" / "knowledge.json"
CLASS_RANK = {"public": 0, "internal": 1, "confidential": 2}
SNIPPET_CHARS = 80
_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9.\-]*")
_CJK = re.compile(r"[一-鿿]+")

Doc = dict[str, Any]


class SearchIn(BaseModel):
    """knowledge.search 入参。"""

    query: str = Field(..., min_length=1, max_length=200)  # 检索语句
    limit: int = Field(5, ge=1, le=20)


@cache
def load_docs(path: Path) -> tuple[Doc, ...]:
    """读取文档（JSON 数组），按路径缓存；调用方不得修改返回的字典。"""
    return tuple(json.loads(path.read_text(encoding="utf-8")))


def data_path() -> Path:
    return Path(os.environ.get("MCP_KNOWLEDGE_DATA") or DEFAULT_DATA)


def can_view(ident: Identity, doc: Doc) -> bool:
    """租户一致，且 ACL 命中 ``*`` / ``role:<r>`` / ``user:<sub>`` 之一。"""
    if doc["tenant"] != ident.tenant:
        return False
    acl = set(doc.get("acl", []))
    return "*" in acl or f"user:{ident.sub}" in acl or any(f"role:{r}" in acl for r in ident.roles)


def terms(text: str) -> set[str]:
    """词元：小写 ASCII 词（如 gw-200）+ 中文连续片段的字 2-gram（单字片段取单字）。"""
    low = text.lower()
    out = set(_ASCII_WORD.findall(low))
    for seg in _CJK.findall(low):
        out |= {seg} if len(seg) == 1 else {seg[i : i + 2] for i in range(len(seg) - 1)}
    return out


def score(query_terms: set[str], doc: Doc) -> float:
    """命中词元的加权比例：标题命中记 3，正文命中记 1，按词元数归一。"""
    if not query_terms:
        return 0.0
    title, body = doc["title"].lower(), doc["body"].lower()
    raw = sum(3 if t in title else 1 if t in body else 0 for t in query_terms)
    return round(raw / (3 * len(query_terms)), 4)


def knowledge_search(ident: Identity, body: SearchIn) -> dict[str, Any]:
    """在调用者可见的文档里检索，按分数倒序、同分按 id。"""
    q = terms(body.query)
    scored = [(score(q, d), d) for d in load_docs(data_path()) if can_view(ident, d)]
    hits = sorted(((s, d) for s, d in scored if s > 0), key=lambda x: (-x[0], x[1]["id"]))
    page = hits[: body.limit]
    items = [
        {
            "id": d["id"],
            "title": d["title"],
            "snippet": d["body"][:SNIPPET_CHARS],
            "data_class": d["data_class"],
            "score": s,
            "updated_at": d["updated_at"],
        }
        for s, d in page
    ]
    top = max((CLASS_RANK[i["data_class"]] for i in items), default=0)
    return {"items": items, "total": len(hits), "data_class": next(k for k, v in CLASS_RANK.items() if v == top)}


TOOL_MANIFEST = [
    read_tool(
        "knowledge.search",
        "企业知识库检索：按调用者身份（租户、角色、本人）过滤文档 ACL，返回命中片段与数据分级",
        SearchIn.model_json_schema(),
        data_class="internal",
    )
]

connector = MockConnector("knowledge", "MCP_KNOWLEDGE", "mcp-knowledge", TOOL_MANIFEST)
app = connector.app


@app.post("/tools/knowledge.search")
def search(body: SearchIn, ident: Identity = Depends(connector.identity)) -> dict[str, Any]:
    """只读工具：知识检索（ACL 过滤）。"""
    connector.record("knowledge.search", ident)
    return knowledge_search(ident, body)
