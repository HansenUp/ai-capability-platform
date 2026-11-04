from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import event, text
from sqlalchemy.pool import NullPool

from runtime.state import db


@pytest.mark.parametrize("poolclass", [None, NullPool])
async def test_sqlite_connection_is_closed_before_owner_loop_ends(tmp_path: Path, poolclass) -> None:
    engine = db.make_engine(f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}", poolclass=poolclass)
    drivers = []
    closed = []

    @event.listens_for(engine.sync_engine, "connect")
    def connected(connection, record):
        drivers.append(connection.driver_connection)

    @event.listens_for(engine.sync_engine, "close")
    def closing(connection, record):
        closed.append(connection.driver_connection)

    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE persisted (value INTEGER)"))
            await connection.execute(text("INSERT INTO persisted VALUES (1)"))
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT value FROM persisted"))).scalar_one() == 1
    finally:
        await engine.dispose()
    assert drivers and len(closed) == len(drivers)
    for driver in drivers:
        thread = getattr(driver, "_thread", driver)
        thread.join(timeout=1)
        assert not thread.is_alive(), "owned SQLite worker outlived connection cleanup"


@pytest.mark.parametrize("stop_fails", [False, True])
async def test_runtime_lifespan_disposes_database_after_background_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_fails
) -> None:
    from runtime import main

    engine = db.make_engine(f"sqlite+aiosqlite:///{tmp_path / 'shutdown.db'}")
    order = []

    @event.listens_for(engine.sync_engine, "close")
    def closing(connection, record):
        order.append("database")

    async def initialize():
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1

    async def stop_node():
        order.append("node")

    async def stop_background():
        # Background tasks retain database access until they finish.
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 2"))).scalar_one() == 2
        order.append("background")
        if stop_fails:
            raise RuntimeError("stop failed")

    async def start_background():
        return SimpleNamespace(worker=None, stop=stop_background)

    async def start_node(app):
        return SimpleNamespace(stop=stop_node)

    monkeypatch.setattr(main.db, "engine", engine)
    monkeypatch.setattr(main, "init_db", initialize)
    monkeypatch.setattr(main, "validate_for_production", lambda value: None)
    monkeypatch.setattr(main.worker_settings_mod, "validate", lambda *args: None)
    monkeypatch.setattr(main.worker_mod, "start_background", start_background)
    monkeypatch.setattr(main.node_agent_mod, "start", start_node)
    monkeypatch.setattr(main.registry, "get", lambda value: None)
    monkeypatch.setattr(main.tracing, "setup_tracing", lambda: None)
    monkeypatch.setattr(main.tracing, "force_flush", lambda: order.append("flush"))
    app = FastAPI()
    try:
        if stop_fails:
            with pytest.raises(RuntimeError, match="stop failed"):
                async with main.lifespan(app):
                    pass
            assert order == ["node", "background", "database"]
            assert app.state.node_shutdown_complete is False
        else:
            async with main.lifespan(app):
                pass
            assert order == ["node", "background", "flush", "database"]
            assert app.state.node_shutdown_complete is True
    finally:
        await engine.dispose()
