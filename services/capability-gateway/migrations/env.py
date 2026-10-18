"""能力网关目录库的 Alembic 运行环境（version_table = cg_alembic_version）。

1. 程序化共享连接：调用方把同步 Connection 放进 ``config.attributes["connection"]``（db.migrate 与测试）；
2. 命令行在线模式：``alembic upgrade head``，连接串取 ``-x db_url=...`` 或 GATEWAY_DATABASE_URL；
3. 命令行离线模式：``alembic upgrade head --sql``。
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from capability_gateway.db import VERSION_TABLE, Base, make_engine, normalize_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("db_url") or os.environ.get("GATEWAY_DATABASE_URL")
    if not url:
        raise SystemExit("GATEWAY_DATABASE_URL 未设置（或用 -x db_url=... 指定）")
    return url


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    url = normalize_database_url(_database_url())
    context.configure(
        url=url,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = make_engine(_database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
