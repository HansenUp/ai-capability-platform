"""SQLite 存储：工单状态覆盖（关闭结果）与幂等记录。

mock 数据文件只读，批量关闭的结果写入 ``ticket_overrides``，查询时叠加到原始数据上；
批量重开（关闭的补偿）删除本连接器写入的关闭覆盖，工单恢复关闭前的状态。
状态更新与幂等记录在**同一个事务**里提交（``BEGIN IMMEDIATE`` 串行化写入）：要么两者都落库，
要么都不落——避免"状态已改但幂等记录丢失导致重试重复执行"或"幂等记录在但状态没改"。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COMPENSATE_TOOL = "ticket.reopen_batch"  # 关闭的补偿工具
CLOSE_TOOL = "ticket.close_batch"  # 重开的补偿工具（对称）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_overrides (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_by TEXT NOT NULL,
    act TEXT
);
CREATE TABLE IF NOT EXISTS idempotency (
    tenant TEXT NOT NULL,
    sub TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant, sub, key)
);
"""


class IdempotencyConflict(Exception):
    """同一幂等键携带了不同的请求内容。"""


def now_iso() -> str:
    """当前 UTC 时间（秒精度，ISO 8601）。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """打开连接并确保表存在；autocommit 模式，事务由调用方显式 BEGIN / COMMIT。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        yield conn


def load_overrides(path: Path) -> dict[str, dict[str, Any]]:
    """读取全部状态覆盖，按工单号索引。"""
    with connect(path) as conn:
        return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM ticket_overrides")}


def _replay(conn: sqlite3.Connection, tenant: str, sub: str, key: str, request_hash: str) -> dict[str, Any] | None:
    """事务内查幂等记录：同键同请求返回首次结果，同键不同请求抛 IdempotencyConflict，未用过返回 None。"""
    row = conn.execute("SELECT request_hash, result FROM idempotency WHERE tenant=? AND sub=? AND key=?", (tenant, sub, key)).fetchone()
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise IdempotencyConflict(key)
    result: dict[str, Any] = json.loads(row["result"])
    return result


def _record(conn: sqlite3.Connection, tenant: str, sub: str, key: str, request_hash: str, result: dict[str, Any], now: str) -> None:
    conn.execute(
        "INSERT INTO idempotency VALUES (?, ?, ?, ?, ?, ?)",
        (tenant, sub, key, request_hash, json.dumps(result, ensure_ascii=False), now),
    )


def close_tickets(
    path: Path,
    *,
    tenant: str,
    sub: str,
    act: str | None,
    key: str,
    request_hash: str,
    ids: list[str],
    base_closed: set[str],
) -> tuple[dict[str, Any], bool]:
    """幂等地关闭工单，返回（结果, 是否为重复请求）。

    ``base_closed`` 为原始数据里本就已关闭的工单号。同键同请求返回首次的结果；同键不同请求抛 IdempotencyConflict。
    """
    now = now_iso()
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = _replay(conn, tenant, sub, key, request_hash)
            if prior is not None:
                conn.execute("ROLLBACK")
                return prior, True
            existing = {r["id"] for r in conn.execute("SELECT id FROM ticket_overrides")}
            to_close = [i for i in ids if i not in base_closed and i not in existing]
            for i in to_close:
                conn.execute("INSERT INTO ticket_overrides VALUES (?, 'closed', ?, ?, ?, ?)", (i, now, now, sub, act))
            result = {
                "closed": to_close,
                "already_closed": [i for i in ids if i not in to_close],
                "closed_at": now,
                "compensate_tool": COMPENSATE_TOOL,
                "by": act or sub,
            }
            _record(conn, tenant, sub, key, request_hash, result, now)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return result, False


def reopen_tickets(
    path: Path,
    *,
    tenant: str,
    sub: str,
    act: str | None,
    key: str,
    request_hash: str,
    ids: list[str],
) -> tuple[dict[str, Any], bool]:
    """幂等地重开工单（close_tickets 的补偿），返回（结果, 是否为重复请求）。

    只重开经本连接器关闭的工单（删除其关闭覆盖，恢复关闭前状态）；原始数据里就已关闭或当前未关闭的记入 not_reopened。
    与 close_tickets 共用幂等表：同键不同请求（含换了工具）抛 IdempotencyConflict。
    """
    now = now_iso()
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = _replay(conn, tenant, sub, key, request_hash)
            if prior is not None:
                conn.execute("ROLLBACK")
                return prior, True
            existing = {r["id"] for r in conn.execute("SELECT id FROM ticket_overrides WHERE status='closed'")}
            to_reopen = [i for i in ids if i in existing]
            for i in to_reopen:
                conn.execute("DELETE FROM ticket_overrides WHERE id=?", (i,))
            result = {
                "reopened": to_reopen,
                "not_reopened": [i for i in ids if i not in to_reopen],
                "reopened_at": now,
                "compensate_tool": CLOSE_TOOL,
                "by": act or sub,
            }
            _record(conn, tenant, sub, key, request_hash, result, now)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return result, False
