"""模板的 HTTP 接入层（`mcp` extra 会把同一批工具再以 streamable-http MCP 协议暴露一份）。"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException

from mcp_template.tools import TOOL_MANIFEST, CloseBatchIn, Identity, ticket_close_batch, ticket_search_impl

app = FastAPI(title="MCP Server Template · ticket", version="0.1.0")


async def identity(authorization: str | None = Header(default=None), x_ai_dev_sub: str | None = Header(default=None)) -> Identity:
    """校验 OBO（On-Behalf-Of）令牌：生产环境走 JWKS 验证，开发环境接受 X-AI-Dev-Sub 直接指定身份。"""
    if x_ai_dev_sub:
        return Identity(sub=x_ai_dev_sub, act="agent:demo")
    if not authorization:
        raise HTTPException(401, "OBO token required")
    raise HTTPException(501, "JWKS verification wired in S8")  # JWKS 校验属于 S8 工作包，尚未实现


@app.get("/tools")
async def tools() -> list[dict[str, Any]]:
    """返回工具清单（manifest），供引擎发现可用工具及其风险等级、dry-run/幂等/补偿工具等元信息。"""
    return TOOL_MANIFEST


@app.post("/tools/ticket.search")
async def search(q: str, ident: Identity = Depends(identity)) -> dict[str, Any]:
    """只读工具：按关键字搜索工单。"""
    return ticket_search_impl(ident, q)


@app.post("/tools/ticket.close_batch")
async def close_batch(body: CloseBatchIn, ident: Identity = Depends(identity)) -> dict[str, Any]:
    """高风险写工具：批量关闭工单，支持 dry_run 影响预演与幂等键去重。"""
    return ticket_close_batch(ident, body)
