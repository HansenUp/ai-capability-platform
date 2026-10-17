"""工单 MCP Server 的 HTTP 接入层。

启动：``uvicorn mcp_ticket.main:app --port 8091``（默认端口 8091）。环境变量见 ``mcp_ticket.config``。

接口：
- ``GET /healthz``：存活探针，无需鉴权；
- ``GET /tools``：工具清单（名称、说明、风险等级、入参 schema，写工具另声明 dry-run / 幂等 / 补偿工具）；
- ``POST /tools/ticket.search`` / ``ticket.get`` / ``ticket.close_batch``：见 ``mcp_ticket.tools``，均需 OBO 令牌。

行级权限：租户必须一致；租户内 ``aftersales`` 角色可见全部工单，其余只可见本人作为 requester / owner 的工单；
无权限与不存在返回同一个 404 ``{"detail": "ticket not found or no permission"}``。
mock 数据中 records 的 actor 为可读名称（非 Keycloak sub），仅供演示摘要使用。
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI

from mcp_ticket.auth import Identity, identity
from mcp_ticket.tools import TOOL_MANIFEST, CloseBatchIn, GetIn, SearchIn, ticket_close_batch, ticket_get, ticket_search

app = FastAPI(title="MCP Server · ticket", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """存活探针。"""
    return {"status": "ok"}


@app.get("/tools")
async def tools() -> list[dict[str, Any]]:
    """返回工具清单（manifest），供能力网关发现工具及其风险等级、dry-run / 幂等 / 补偿工具等元信息。"""
    return TOOL_MANIFEST


# 以下工具含同步的 SQLite 读写，用 def 让 FastAPI 放进线程池执行，不阻塞事件循环
@app.post("/tools/ticket.search")
def search(body: SearchIn, ident: Identity = Depends(identity)) -> dict[str, Any]:
    """只读工具：搜索调用者有权查看的工单。"""
    return ticket_search(ident, body)


@app.post("/tools/ticket.get")
def get(body: GetIn, ident: Identity = Depends(identity)) -> dict[str, Any]:
    """只读工具：获取单个工单详情。"""
    return ticket_get(ident, body)


@app.post("/tools/ticket.close_batch")
def close_batch(body: CloseBatchIn, ident: Identity = Depends(identity)) -> dict[str, Any]:
    """高风险写工具：批量关闭工单，支持 dry_run 影响预演与幂等键去重。"""
    return ticket_close_batch(ident, body)
