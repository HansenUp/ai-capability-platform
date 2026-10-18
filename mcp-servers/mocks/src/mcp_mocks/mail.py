"""mail mock（8094）：``mail.send`` 外发写工具（幂等键 + dry-run，补偿 ``mail.recall``）与发件箱计数接口。

- ``mail.send``：dry-run 只返回影响预演（收件人数、外部收件人）；执行按（租户, 调用者, 幂等键）去重，
  发件箱写入与幂等记录同事务（``mcp_template.idempotency``），同键不同请求 409；
- ``mail.recall``（补偿）：只能撤回本人在本租户发出的邮件（否则 404，与不存在不可区分）；同样 dry-run + 幂等；
  其补偿声明为 ``mail.send``（重新发送），满足写规范"补偿工具在同一 manifest"；
- ``GET /_mock/outbox?tenant=&status=``：发件箱计数（e2e 断言"拒绝后 0、批准后 1"），``POST /_mock/reset`` 清空。

状态库 ``MCP_MAIL_DB``（缺省系统临时目录下 ``mcp-mail.db``）；内部域名 ``MCP_MAIL_INTERNAL_DOMAINS``（逗号分隔，缺省 ``corp.example``）。
鉴权前缀 ``MCP_MAIL``，受众默认 ``mcp-mail``（见 ``mcp_template.auth``）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from mcp_mocks.common import MockConnector
from mcp_template import idempotency
from mcp_template.auth import Identity
from mcp_template.spec import WriteControl, write_tool

SEND, RECALL = "mail.send", "mail.recall"
_EMAIL_MAX = 254

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    sender TEXT NOT NULL,
    act TEXT,
    run_id TEXT,
    call_id TEXT,
    recipients TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    recalled_at TEXT
);
"""


def db_path() -> Path:
    return Path(os.environ.get("MCP_MAIL_DB") or Path(tempfile.gettempdir()) / "mcp-mail.db")


def internal_domains() -> frozenset[str]:
    raw = os.environ.get("MCP_MAIL_INTERNAL_DOMAINS", "corp.example")
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())


def _check_address(addr: str) -> str:
    local, sep, domain = addr.strip().rpartition("@")
    if not sep or not local or "." not in domain or len(addr) > _EMAIL_MAX or any(c.isspace() for c in addr.strip()):
        raise ValueError(f"invalid email address: {addr!r}")
    return f"{local}@{domain.lower()}"


class SendArgs(BaseModel):
    """mail.send 业务参数（暴露给模型）。"""

    to: list[str] = Field(..., min_length=1, max_length=20)  # 收件人
    cc: list[str] = Field(default_factory=list, max_length=20)  # 抄送
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=20000)

    @field_validator("to", "cc")
    @classmethod
    def _addresses(cls, value: list[str]) -> list[str]:
        return [_check_address(a) for a in value]


class SendIn(SendArgs, WriteControl):
    """mail.send 完整请求体：业务参数 + 网关注入的控制字段。"""


class RecallArgs(BaseModel):
    """mail.recall 业务参数。"""

    message_id: str = Field(..., min_length=1, max_length=64)


class RecallIn(RecallArgs, WriteControl):
    """mail.recall 完整请求体。"""


def _not_found() -> HTTPException:
    return HTTPException(404, "message not found or no permission")


def _recipients(body: SendArgs) -> list[str]:
    return list(dict.fromkeys(body.to + body.cc))


def mail_send(ident: Identity, body: SendIn) -> dict[str, Any]:
    """外发邮件：dry-run 只预演；否则幂等写入发件箱。"""
    recipients = _recipients(body)
    external = [a for a in recipients if a.rsplit("@", 1)[1] not in internal_domains()]
    if body.dry_run:
        return {
            "dry_run": True,
            "impact_preview": {"messages": 1, "recipients": len(recipients), "external_recipients": external, "subject": body.subject},
            "card": {"card_type": "impact_preview"},
        }
    args = {"to": sorted(body.to), "cc": sorted(body.cc), "subject": body.subject, "body": body.body}

    def execute(conn: sqlite3.Connection) -> dict[str, Any]:
        mid = f"M-{uuid.uuid4().hex[:12]}"
        now = idempotency.now_iso()
        conn.execute(
            "INSERT INTO outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sent', ?, NULL)",
            (mid, ident.tenant, ident.sub, ident.act, ident.run_id, ident.call_id, json.dumps(recipients), body.subject, body.body, now),
        )
        return {
            "message_id": mid,
            "status": "sent",
            "recipients": len(recipients),
            "external_recipients": external,
            "sent_at": now,
            "compensate_tool": RECALL,
            "by": ident.act or ident.sub,
        }

    return _run(ident, body, SEND, args, execute)


def mail_recall(ident: Identity, body: RecallIn) -> dict[str, Any]:
    """撤回本人发出的邮件（mail.send 的补偿）。"""
    if body.dry_run:
        with idempotency.connect(db_path(), _SCHEMA) as conn:
            row = _own_message(conn, ident, body.message_id)
        recallable = row["status"] == "sent"
        return {
            "dry_run": True,
            "impact_preview": {"messages": int(recallable), "recipients": len(json.loads(row["recipients"])) if recallable else 0},
            "card": {"card_type": "impact_preview"},
        }

    def execute(conn: sqlite3.Connection) -> dict[str, Any]:
        row = _own_message(conn, ident, body.message_id)
        recalled = row["status"] == "sent"
        now = idempotency.now_iso()
        if recalled:
            conn.execute("UPDATE outbox SET status='recalled', recalled_at=? WHERE id=?", (now, body.message_id))
        return {
            "message_id": body.message_id,
            "recalled": recalled,
            "status": "recalled",
            "at": now,
            "compensate_tool": SEND,
            "by": ident.act or ident.sub,
        }

    return _run(ident, body, RECALL, {"message_id": body.message_id}, execute)


def _own_message(conn: sqlite3.Connection, ident: Identity, message_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM outbox WHERE id=? AND tenant=? AND sender=?", (message_id, ident.tenant, ident.sub)
    ).fetchone()
    if row is None:
        raise _not_found()
    return row


def _run(
    ident: Identity, body: WriteControl, tool: str, args: dict[str, Any], execute: Callable[[sqlite3.Connection], dict[str, Any]]
) -> dict[str, Any]:
    """幂等执行写操作；同键不同请求 409。"""
    with idempotency.connect(db_path(), _SCHEMA) as conn:
        try:
            result, replay = idempotency.run_once(
                conn,
                tenant=ident.tenant,
                sub=ident.sub,
                key=str(body.idempotency_key),
                req_hash=idempotency.request_hash(tool, args),
                execute=execute,
            )
        except idempotency.IdempotencyConflict:
            raise HTTPException(409, "idempotency key reused with a different request") from None
    return result | {"deduplicated": replay}


def outbox(tenant: str | None, status: Literal["sent", "recalled"] | None) -> dict[str, Any]:
    """发件箱计数与摘要（不含正文）。"""
    sql, params = "SELECT id, tenant, sender, act, run_id, call_id, recipients, subject, status, sent_at FROM outbox WHERE 1=1", []
    if tenant:
        sql += " AND tenant=?"
        params.append(tenant)
    if status:
        sql += " AND status=?"
        params.append(status)
    with idempotency.connect(db_path(), _SCHEMA) as conn:
        rows = [dict(r) | {"recipients": json.loads(r["recipients"])} for r in conn.execute(sql + " ORDER BY sent_at, id", params)]
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {"count": len(rows), "by_status": by_status, "items": rows}


def reset_outbox() -> None:
    with idempotency.connect(db_path(), _SCHEMA) as conn:
        conn.execute("DELETE FROM outbox")
        conn.execute("DELETE FROM idempotency")


TOOL_MANIFEST = [
    write_tool(
        SEND,
        "发送邮件（外发）：支持 dry_run 影响预演（收件人数、外部收件人），执行必须带幂等键；补偿工具 mail.recall",
        SendArgs.model_json_schema(),
        risk_level="write_low",
        compensate_tool=RECALL,
    ),
    write_tool(
        RECALL,
        "撤回本人发出的邮件（mail.send 的补偿）：支持 dry_run 影响预演，执行必须带幂等键",
        RecallArgs.model_json_schema(),
        risk_level="write_low",
        compensate_tool=SEND,
        compensates=SEND,
    ),
]

connector = MockConnector("mail", "MCP_MAIL", "mcp-mail", TOOL_MANIFEST)
connector.on_reset(reset_outbox)
app = connector.app


@app.post("/tools/mail.send")
def send(body: SendIn, ident: Identity = Depends(connector.identity)) -> dict[str, Any]:
    """写工具：外发邮件。"""
    connector.record(SEND, ident)
    return mail_send(ident, body)


@app.post("/tools/mail.recall")
def recall(body: RecallIn, ident: Identity = Depends(connector.identity)) -> dict[str, Any]:
    """写工具（补偿）：撤回邮件。"""
    connector.record(RECALL, ident)
    return mail_recall(ident, body)


@app.get("/_mock/outbox")
def get_outbox(tenant: str | None = None, status: Literal["sent", "recalled"] | None = None) -> dict[str, Any]:
    """发件箱计数（仅本机 e2e 断言用，不鉴权）。"""
    return outbox(tenant, status)
