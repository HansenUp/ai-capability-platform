"""SQLite 幂等执行：业务写入与幂等记录在**同一事务**里提交（``BEGIN IMMEDIATE`` 串行化写入）。

幂等键按（租户, 调用者 sub, 键）隔离——不同调用者用同一个键互不影响，拿不到别人的结果；
请求哈希包含工具名与业务参数，同键不同请求（含换了工具）抛 :class:`IdempotencyConflict`（端点映射为 409）。
要么业务写入与幂等记录都落库，要么都不落：避免"已执行但记录丢失导致重试重复执行"。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

IDEMPOTENCY_SCHEMA = """
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


def request_hash(tool: str, args: Mapping[str, Any]) -> str:
    """请求指纹：工具名 + 业务参数（键排序后的 JSON）。调用方应先把顺序无关的列表规范化。"""
    return hashlib.sha256(json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def connect(path: Path, schema: str = "") -> Iterator[sqlite3.Connection]:
    """打开连接并确保幂等表与业务表存在；autocommit 模式，事务由调用方显式 BEGIN / COMMIT。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(IDEMPOTENCY_SCHEMA + schema)
        yield conn


def run_once(
    conn: sqlite3.Connection,
    *,
    tenant: str,
    sub: str,
    key: str,
    req_hash: str,
    execute: Callable[[sqlite3.Connection], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """幂等执行 ``execute``（在同一事务内），返回（结果, 是否为重复请求）。

    同键同请求返回首次结果且不再执行；同键不同请求抛 IdempotencyConflict；``execute`` 抛异常时整个事务回滚、不占用键。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT request_hash, result FROM idempotency WHERE tenant=? AND sub=? AND key=?", (tenant, sub, key)).fetchone()
        if row is not None:
            conn.execute("ROLLBACK")
            if row["request_hash"] != req_hash:
                raise IdempotencyConflict(key)
            return json.loads(row["result"]), True
        result = execute(conn)
        conn.execute(
            "INSERT INTO idempotency VALUES (?, ?, ?, ?, ?, ?)",
            (tenant, sub, key, req_hash, json.dumps(result, ensure_ascii=False), now_iso()),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return result, False
