"""工具实现：ticket.search / ticket.get（只读）与 ticket.close_batch / ticket.reopen_batch（高风险写，互为补偿，dry-run + 幂等）。

manifest 按写规范（S8.5，见 ``mcp_template.spec``）声明 idempotency / dry_run / compensate_tool，input_schema 不含控制字段。

出入参遵循 docs/decisions/2026-10-16-首个垂直切片场景确认.md §3。所有读写都先过行级权限（见 data.py），
工单叠加 SQLite 里的状态覆盖，因此关闭结果能立即在 search / get 中看到。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from mcp_template.spec import WriteControl, read_tool, write_tool
from mcp_ticket import store
from mcp_ticket.auth import Identity
from mcp_ticket.config import get_settings
from mcp_ticket.data import Ticket, can_view, load_tickets

MAX_RECORDS = 50  # ticket.get 最多返回的处理记录条数，超出截断并标记 records_truncated
MAX_LIMIT = 50
LOCAL_TZ = timezone(timedelta(hours=8))  # 不带时区的查询时间按东八区解释
SUMMARY_FIELDS = ("id", "title", "status", "priority", "category", "owner", "created_at", "updated_at")
Status = Literal["open", "processing", "closed"]


def _not_found() -> HTTPException:
    """无权限与不存在使用同一响应，不泄露工单是否存在。"""
    return HTTPException(404, "ticket not found or no permission")


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)


def _check_ts(value: str | None) -> str | None:
    if value is not None:
        try:
            _parse_ts(value)
        except ValueError:
            raise ValueError("must be an ISO 8601 time") from None
    return value


class SearchIn(BaseModel):
    """ticket.search 入参。"""

    q: str | None = None  # 关键字，匹配标题 / 描述 / 分类（子串，不区分大小写）
    status: Status | None = None
    created_from: str | None = None  # ISO 8601，含；不带时区按东八区
    created_to: str | None = None  # ISO 8601；仅日期时含当天
    cursor: str | None = None  # 上一页返回的 next_cursor（不透明字符串）
    limit: int = Field(20, ge=1, le=MAX_LIMIT)

    @field_validator("created_from", "created_to")
    @classmethod
    def check_ts(cls, value: str | None) -> str | None:
        return _check_ts(value)


class GetIn(BaseModel):
    """ticket.get 入参。"""

    id: str


class BatchArgs(BaseModel):
    """批量关闭 / 重开的业务参数（manifest 只暴露这部分给模型）。"""

    ids: list[str] = Field(..., min_length=1, max_length=100)  # 工单号列表


class CloseBatchIn(BatchArgs, WriteControl):
    """ticket.close_batch 入参：业务参数 + 网关注入的 idempotency_key（只有 dry_run 时可省略）/ dry_run。"""


class ReopenBatchIn(BatchArgs, WriteControl):
    """ticket.reopen_batch 入参（close_batch 的补偿），与 close_batch 对称。"""


def _effective(ticket: Ticket, overrides: dict[str, dict[str, Any]]) -> Ticket:
    """叠加状态覆盖后的工单副本。"""
    o = overrides.get(ticket["id"])
    if o is None:
        return ticket
    return ticket | {"status": o["status"], "closed_at": o["closed_at"], "updated_at": o["updated_at"]}


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, binascii.Error):
        raise HTTPException(422, "invalid cursor") from None
    if offset < 0:
        raise HTTPException(422, "invalid cursor")
    return offset


def _matches(t: Ticket, body: SearchIn) -> bool:
    if body.status and t["status"] != body.status:
        return False
    if body.q:
        needle = body.q.lower()
        if not any(needle in str(t.get(f, "")).lower() for f in ("title", "description", "category")):
            return False
    created = _parse_ts(t["created_at"])
    if body.created_from and created < _parse_ts(body.created_from):
        return False
    if body.created_to:
        upper = _parse_ts(body.created_to)
        if len(body.created_to) == 10:  # 仅日期：含当天
            return created < upper + timedelta(days=1)
        return created <= upper
    return True


def ticket_search(ident: Identity, body: SearchIn) -> dict[str, Any]:
    """只读搜索：仅返回调用者有权查看的工单摘要，按创建时间倒序，游标即偏移量。"""
    cfg = get_settings()
    overrides = store.load_overrides(cfg.db_path)
    visible = [_effective(t, overrides) for t in load_tickets(cfg.data_path) if can_view(ident, t)]
    hits = sorted((t for t in visible if _matches(t, body)), key=lambda t: (_parse_ts(t["created_at"]), t["id"]), reverse=True)
    offset = _decode_cursor(body.cursor)
    page = hits[offset : offset + body.limit]
    more = offset + body.limit < len(hits)
    return {
        "items": [{f: t[f] for f in SUMMARY_FIELDS} for t in page],
        "next_cursor": _encode_cursor(offset + body.limit) if more else None,
        "total": len(hits),
    }


def ticket_get(ident: Identity, body: GetIn) -> dict[str, Any]:
    """只读详情：无权限与不存在同为 404；处理记录超过 MAX_RECORDS 条时截断（保留最早的若干条）。"""
    cfg = get_settings()
    ticket = next((t for t in load_tickets(cfg.data_path) if t["id"] == body.id), None)
    if ticket is None or not can_view(ident, ticket):
        raise _not_found()
    t = _effective(ticket, store.load_overrides(cfg.db_path))
    records = t["records"]
    return {
        **{f: t[f] for f in SUMMARY_FIELDS},
        "requester": t["requester"],
        "description": t["description"],
        "records": records[:MAX_RECORDS],
        "records_truncated": len(records) > MAX_RECORDS,
        "customer_feedback": t["customer_feedback"],
        "closed_at": t["closed_at"],
        "tenant": t["tenant"],
    }


def ticket_close_batch(ident: Identity, body: CloseBatchIn) -> dict[str, Any]:
    """批量关闭：任一工单无权限或不存在则整个请求 404 且不写入；dry_run 只返回影响预演；
    否则按（租户, 用户, 幂等键）去重，状态更新与幂等记录同事务提交。"""
    cfg = get_settings()
    ids = list(dict.fromkeys(body.ids))  # 去重并保持顺序
    tickets = {t["id"]: t for t in load_tickets(cfg.data_path)}
    if any(i not in tickets or not can_view(ident, tickets[i]) for i in ids):
        raise _not_found()
    base_closed = {i for i in ids if tickets[i]["status"] == "closed"}
    if body.dry_run:
        done = base_closed | set(store.load_overrides(cfg.db_path))
        to_close = [i for i in ids if i not in done]
        return {
            "dry_run": True,
            "impact_preview": {
                "tickets": len(to_close),
                "notifications": len(to_close),
                "ids": to_close,
                "already_closed": [i for i in ids if i in done],
            },
            "card": {"card_type": "impact_preview"},
        }
    request_hash = hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()
    try:
        result, replay = store.close_tickets(
            cfg.db_path,
            tenant=ident.tenant,
            sub=ident.sub,
            act=ident.act,
            key=str(body.idempotency_key),  # 非 dry_run 时校验器已保证存在
            request_hash=request_hash,
            ids=ids,
            base_closed=base_closed,
        )
    except store.IdempotencyConflict:
        raise HTTPException(409, "idempotency key reused with a different request") from None
    return result | {"deduplicated": replay}


def ticket_reopen_batch(ident: Identity, body: ReopenBatchIn) -> dict[str, Any]:
    """批量重开（close_batch 的补偿）：权限规则与 close_batch 相同（任一工单不可见整体 404 且不写入）；
    dry_run 只返回影响预演；否则按（租户, 用户, 幂等键）去重，状态恢复与幂等记录同事务提交。
    只重开经本连接器关闭的工单，源系统里本就关闭或当前未关闭的记入 not_reopened。"""
    cfg = get_settings()
    ids = list(dict.fromkeys(body.ids))
    tickets = {t["id"]: t for t in load_tickets(cfg.data_path)}
    if any(i not in tickets or not can_view(ident, tickets[i]) for i in ids):
        raise _not_found()
    if body.dry_run:
        closed_here = {i for i, o in store.load_overrides(cfg.db_path).items() if o["status"] == "closed"}
        to_reopen = [i for i in ids if i in closed_here]
        return {
            "dry_run": True,
            "impact_preview": {
                "tickets": len(to_reopen),
                "notifications": len(to_reopen),
                "ids": to_reopen,
                "not_reopened": [i for i in ids if i not in closed_here],
            },
            "card": {"card_type": "impact_preview"},
        }
    # 请求指纹带工具名：同一幂等键先用于 close_batch 再用于 reopen_batch 视为不同请求（409），不会误回放关闭结果
    request_hash = hashlib.sha256(json.dumps({"tool": store.COMPENSATE_TOOL, "ids": sorted(ids)}).encode()).hexdigest()
    try:
        result, replay = store.reopen_tickets(
            cfg.db_path,
            tenant=ident.tenant,
            sub=ident.sub,
            act=ident.act,
            key=str(body.idempotency_key),
            request_hash=request_hash,
            ids=ids,
        )
    except store.IdempotencyConflict:
        raise HTTPException(409, "idempotency key reused with a different request") from None
    return result | {"deduplicated": replay}


TOOL_MANIFEST: list[dict[str, Any]] = [
    read_tool(
        "ticket.search",
        "按关键字 / 状态 / 创建时间范围搜索工单，返回调用者有权查看的工单摘要（游标分页）",
        SearchIn.model_json_schema(),
    ),
    read_tool("ticket.get", "按工单号获取工单详情（含处理记录与客户反馈）；无权限与不存在返回同一 404", GetIn.model_json_schema()),
    write_tool(
        store.CLOSE_TOOL,
        "批量关闭工单（高风险写操作）：支持 dry_run 影响预演，执行必须带幂等键；补偿工具 ticket.reopen_batch",
        BatchArgs.model_json_schema(),
        compensate_tool=store.COMPENSATE_TOOL,
    ),
    write_tool(
        store.COMPENSATE_TOOL,
        "批量重开经本系统关闭的工单（close_batch 的补偿，高风险写）：支持 dry_run 影响预演，执行必须带幂等键",
        BatchArgs.model_json_schema(),
        compensate_tool=store.CLOSE_TOOL,
        compensates=store.CLOSE_TOOL,
    ),
]
