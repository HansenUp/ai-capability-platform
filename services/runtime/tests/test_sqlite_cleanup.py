from __future__ import annotations

import asyncio
import gc
import sqlite3
import weakref
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy import text

from runtime.state import db


async def test_cancelled_sqlite_read_closes_retained_cursor_before_connection_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "cancelled-reader.db"
    engine = db.make_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE cursor_probe (value INTEGER)"))
        await connection.execute(text("INSERT INTO cursor_probe VALUES (1), (2)"))
    entered = asyncio.Event()
    blocked = asyncio.Event()
    retained: list[aiosqlite.Cursor] = []
    original = aiosqlite.Cursor.execute

    async def execute(cursor: aiosqlite.Cursor, sql: str, parameters: object = None) -> aiosqlite.Cursor:
        result = await original(cursor, sql, parameters)
        if sql == "SELECT value FROM cursor_probe":
            retained.append(cursor)
            entered.set()
            await blocked.wait()
        return result

    monkeypatch.setattr(aiosqlite.Cursor, "execute", execute)

    async def read() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT value FROM cursor_probe"))

    task = asyncio.create_task(read())
    writer = await aiosqlite.connect(path, timeout=0)
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(retained) == 1
        await writer.execute("INSERT INTO cursor_probe VALUES (3)")
        await writer.commit()
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            retained[0]._cursor.fetchone()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await writer.rollback()
        await writer.close()
        retained.clear()
        await engine.dispose()


def test_sqlite_close_finalizes_live_cursors_without_weakening_active_read_lock(tmp_path: Path) -> None:
    path = tmp_path / "read-lock.db"
    writer = sqlite3.connect(path, timeout=0)
    reader = sqlite3.connect(path, factory=db._SQLiteConnection)
    try:
        writer.execute("CREATE TABLE cursor_probe (value INTEGER)")
        writer.execute("INSERT INTO cursor_probe VALUES (1), (2)")
        writer.commit()
        cursor = reader.cursor()
        cursor.execute("SELECT value FROM cursor_probe")
        writer.execute("INSERT INTO cursor_probe VALUES (3)")
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            writer.commit()
        writer.rollback()
        reader.close()
        reader.close()
        writer.execute("INSERT INTO cursor_probe VALUES (4)")
        writer.commit()
        with pytest.raises(sqlite3.ProgrammingError, match="closed cursor"):
            cursor.fetchone()
        assert writer.execute("SELECT value FROM cursor_probe ORDER BY value").fetchall() == [(1,), (2,), (4,)]
    finally:
        reader.close()
        writer.close()


def test_sqlite_cursor_factory_preserves_subclass_and_does_not_retain_unused_cursors(tmp_path: Path) -> None:
    class ProbeCursor(sqlite3.Cursor):
        pass

    connection = sqlite3.connect(tmp_path / "weak-cursors.db", factory=db._SQLiteConnection)
    try:
        cursor = connection.cursor(lambda owner: ProbeCursor(owner))
        assert isinstance(cursor, ProbeCursor)
        reference = weakref.ref(cursor)
        del cursor
        gc.collect()
        assert reference() is None
        assert not connection._cursors
        cursor = connection.cursor()
        cursor.close()
        connection.close()
        connection.close()
    finally:
        connection.close()
