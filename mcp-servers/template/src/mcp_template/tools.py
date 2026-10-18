"""模板示例工具：一个只读工具 + 一对互为补偿的写工具，演示写规范（见 :mod:`mcp_template.spec`）。

- ``ticket.search``（只读）；
- ``ticket.close_batch``（写）：dry-run 影响预演、幂等键、补偿工具 ``ticket.reopen_batch``；
- ``ticket.reopen_batch``（写，close_batch 的补偿）：同样满足三项规范，反向补偿为 ``ticket.close_batch``。

示例状态存在 SQLite（``MCP_TEMPLATE_DB``，缺省系统临时目录下 ``mcp-template.db``），与幂等记录同库同事务。
行级权限是下游系统的职责：示例只按租户隔离状态，真实连接器须按 identity 做行级过滤（参考 mcp-ticket）。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from mcp_template import idempotency
from mcp_template.auth import Identity
from mcp_template.spec import WriteControl, read_tool, write_tool

_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_closed (
    tenant TEXT NOT NULL,
    id TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    closed_by TEXT NOT NULL,
    PRIMARY KEY (tenant, id)
);
"""


def db_path() -> Path:
    return Path(os.environ.get("MCP_TEMPLATE_DB") or Path(tempfile.gettempdir()) / "mcp-template.db")


class SearchIn(BaseModel):
    """ticket.search 入参。"""

    q: str = ""  # 关键字


class BatchArgs(BaseModel):
    """批量操作的业务参数（暴露给模型的部分）。"""

    ids: list[str] = Field(..., min_length=1, max_length=100)  # 工单号列表


class BatchIn(BatchArgs, WriteControl):
    """批量写工具的完整请求体：业务参数 + 网关注入的控制字段。"""


def ticket_search_impl(identity: Identity, body: SearchIn) -> dict[str, Any]:
    """只读搜索实现（示例数据）。"""
    return {"tickets": [{"id": "T-1", "owner": identity.sub, "title": f"匹配 {body.q}"}]}


def _closed(conn: sqlite3.Connection, tenant: str) -> set[str]:
    return {r["id"] for r in conn.execute("SELECT id FROM demo_closed WHERE tenant=?", (tenant,))}


def _batch(identity: Identity, body: BatchIn, *, tool: str, close: bool) -> dict[str, Any]:
    """close / reopen 共用流程：dry_run 只预演；否则幂等执行，状态更新与幂等记录同事务。"""
    ids = list(dict.fromkeys(body.ids))
    with idempotency.connect(db_path(), _SCHEMA) as conn:
        if body.dry_run:
            closed = _closed(conn, identity.tenant)
            todo = [i for i in ids if (i not in closed) == close]
            return {
                "dry_run": True,
                "impact_preview": {"tickets": len(todo), "notifications": len(todo), "ids": todo},
                "card": {"card_type": "impact_preview"},
            }

        def execute(c: sqlite3.Connection) -> dict[str, Any]:
            closed = _closed(c, identity.tenant)
            todo = [i for i in ids if (i not in closed) == close]
            now = idempotency.now_iso()
            for i in todo:
                if close:
                    c.execute("INSERT INTO demo_closed VALUES (?, ?, ?, ?)", (identity.tenant, i, now, identity.act or identity.sub))
                else:
                    c.execute("DELETE FROM demo_closed WHERE tenant=? AND id=?", (identity.tenant, i))
            done_key, skip_key = ("closed", "already_closed") if close else ("reopened", "not_closed")
            return {
                done_key: todo,
                skip_key: [i for i in ids if i not in todo],
                "at": now,
                "compensate_tool": "ticket.reopen_batch" if close else "ticket.close_batch",
                "by": identity.act or identity.sub,
            }

        try:
            result, replay = idempotency.run_once(
                conn,
                tenant=identity.tenant,
                sub=identity.sub,
                key=str(body.idempotency_key),
                req_hash=idempotency.request_hash(tool, {"ids": sorted(ids)}),
                execute=execute,
            )
        except idempotency.IdempotencyConflict:
            raise HTTPException(409, "idempotency key reused with a different request") from None
    return result | {"deduplicated": replay}


def ticket_close_batch(identity: Identity, body: BatchIn) -> dict[str, Any]:
    """批量关闭（写）。"""
    return _batch(identity, body, tool="ticket.close_batch", close=True)


def ticket_reopen_batch(identity: Identity, body: BatchIn) -> dict[str, Any]:
    """批量重开（写，close_batch 的补偿）。"""
    return _batch(identity, body, tool="ticket.reopen_batch", close=False)


TOOL_MANIFEST: list[dict[str, Any]] = [
    read_tool("ticket.search", "按关键字搜索工单（只读示例）", SearchIn.model_json_schema()),
    write_tool(
        "ticket.close_batch",
        "批量关闭工单（高风险写示例）：支持 dry_run 影响预演，执行必须带幂等键",
        BatchIn.model_json_schema(),
        compensate_tool="ticket.reopen_batch",
    ),
    write_tool(
        "ticket.reopen_batch",
        "批量重开工单（close_batch 的补偿）：支持 dry_run 影响预演，执行必须带幂等键",
        BatchIn.model_json_schema(),
        compensate_tool="ticket.close_batch",
        compensates="ticket.close_batch",
    ),
]
