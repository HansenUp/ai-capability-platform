"""模板的 HTTP 接入层（REST ``/tools/{name}``，与能力网关 ``/invoke`` 转发约定一致）。

启动（只绑本机）：``uvicorn mcp_template.main:app --host 127.0.0.1 --port 8099``。

接口：
- ``GET /healthz``：存活探针，无需鉴权；
- ``GET /tools``：manifest（每个写工具声明 ``idempotency`` / ``dry_run`` / ``compensate_tool``，见 :mod:`mcp_template.spec`）；
- ``POST /tools/<name>``：工具调用，需 OBO 令牌 + act 断言（鉴权见 :mod:`mcp_template.auth`，前缀 ``MCP_TEMPLATE``）。
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI

from mcp_template.auth import Identity, identity_dependency
from mcp_template.tools import TOOL_MANIFEST, BatchIn, SearchIn, ticket_close_batch, ticket_reopen_batch, ticket_search_impl

app = FastAPI(title="MCP Server Template · ticket", version="0.2.0")
identity = identity_dependency("MCP_TEMPLATE", default_audience="mcp-template")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """存活探针。"""
    return {"status": "ok"}


@app.get("/tools")
async def tools() -> list[dict[str, Any]]:
    """返回工具清单（manifest）。"""
    return TOOL_MANIFEST


@app.post("/tools/ticket.search")
def search(body: SearchIn, ident: Identity = Depends(identity)) -> dict[str, Any]:
    """只读工具：按关键字搜索工单。"""
    return ticket_search_impl(ident, body)


# 写工具含同步 SQLite 读写，用 def 让 FastAPI 放进线程池执行
@app.post("/tools/ticket.close_batch")
def close_batch(body: BatchIn, ident: Identity = Depends(identity)) -> dict[str, Any]:
    """高风险写工具：批量关闭工单。"""
    return ticket_close_batch(ident, body)


@app.post("/tools/ticket.reopen_batch")
def reopen_batch(body: BatchIn, ident: Identity = Depends(identity)) -> dict[str, Any]:
    """写工具（补偿）：批量重开工单。"""
    return ticket_reopen_batch(ident, body)
