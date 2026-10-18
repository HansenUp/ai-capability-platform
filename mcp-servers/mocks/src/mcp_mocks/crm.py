"""crm mock（8093）：``crm.opportunity.search``，只读，数据分级 confidential。

商机数据 ``data/crm.json``（可用 ``MCP_CRM_DATA`` 换文件）。行级权限：租户必须一致；持 realm 角色 ``finance`` 可见租户内全部商机，
其余调用者只见 ``owner`` 为本人的商机。响应固定带 ``data_class: "confidential"``（读取后 Run 带 confidential 污点，
verify-s8 链路⑤据此让外发 ``mail.send`` 走 require_approval）。

鉴权前缀 ``MCP_CRM``，受众默认 ``mcp-crm``（见 ``mcp_template.auth``）。
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends
from pydantic import BaseModel, Field

from mcp_mocks.common import MockConnector
from mcp_template.auth import Identity
from mcp_template.spec import DataClass, read_tool

DEFAULT_DATA = Path(__file__).parent / "data" / "crm.json"
VIEW_ALL_ROLE = "finance"  # 可见租户内全部商机的 realm 角色
DATA_CLASS: DataClass = "confidential"
Stage = Literal["prospecting", "qualification", "proposal", "negotiation", "won", "lost"]
FIELDS = ("id", "name", "customer", "stage", "amount", "currency", "owner", "expected_close", "updated_at")

Opportunity = dict[str, Any]


class SearchIn(BaseModel):
    """crm.opportunity.search 入参。"""

    q: str | None = Field(default=None, max_length=100)  # 关键字，匹配商机名称与客户（子串，不区分大小写）
    stage: Stage | None = None
    limit: int = Field(20, ge=1, le=50)


@cache
def load_opportunities(path: Path) -> tuple[Opportunity, ...]:
    return tuple(json.loads(path.read_text(encoding="utf-8")))


def data_path() -> Path:
    return Path(os.environ.get("MCP_CRM_DATA") or DEFAULT_DATA)


def can_view(ident: Identity, opp: Opportunity) -> bool:
    """租户一致；finance 可见全部，其余只见本人负责的商机。"""
    return opp["tenant"] == ident.tenant and (VIEW_ALL_ROLE in ident.roles or opp["owner"] == ident.sub)


def _matches(opp: Opportunity, body: SearchIn) -> bool:
    if body.stage and opp["stage"] != body.stage:
        return False
    if body.q:
        needle = body.q.lower()
        return needle in opp["name"].lower() or needle in opp["customer"].lower()
    return True


def opportunity_search(ident: Identity, body: SearchIn) -> dict[str, Any]:
    """只读搜索：调用者可见的商机，按更新时间倒序。"""
    hits = [o for o in load_opportunities(data_path()) if can_view(ident, o) and _matches(o, body)]
    hits.sort(key=lambda o: (o["updated_at"], o["id"]), reverse=True)
    return {"items": [{f: o[f] for f in FIELDS} for o in hits[: body.limit]], "total": len(hits), "data_class": DATA_CLASS}


TOOL_MANIFEST = [
    read_tool(
        "crm.opportunity.search",
        "查询本人有权查看的销售商机（客户、阶段、金额、预计成交日期）；机密数据",
        SearchIn.model_json_schema(),
        data_class=DATA_CLASS,
    )
]

connector = MockConnector("crm", "MCP_CRM", "mcp-crm", TOOL_MANIFEST)
app = connector.app


@app.post("/tools/crm.opportunity.search")
def search(body: SearchIn, ident: Identity = Depends(connector.identity)) -> dict[str, Any]:
    """只读工具：商机搜索（行级权限）。"""
    connector.record("crm.opportunity.search", ident)
    return opportunity_search(ident, body)
