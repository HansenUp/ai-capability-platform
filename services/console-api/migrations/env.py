"""Console 库的 Alembic 运行环境（与 Runtime 的 env.py 同构；版本表为 alembic_version_console）。

支持三种调用方式：
1. 程序化共享连接：调用方把同步 Connection 放进 ``config.attributes["connection"]``（Runtime 启动时的
   ``init_db()`` 与测试即用此方式），这里直接在该连接上执行迁移，不创建新引擎、不新起事件循环；
2. 命令行在线模式：``alembic upgrade head``，按连接串创建异步引擎后经 ``run_sync`` 执行迁移；
   连接串优先取 ``-x db_url=...``，否则取 ``console_api.db.DATABASE_URL``（环境变量 CONSOLE_DATABASE_URL）；
3. 命令行离线模式：``alembic upgrade head --sql``，只输出 SQL 不连库。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from console_api.db import DATABASE_URL, VERSION_TABLE, Base, make_engine, normalize_database_url

config = context.config

# 仅命令行（带 alembic.ini）时按 ini 配置日志；程序化调用不传 ini，避免覆盖应用自身的日志设置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata  # autogenerate 与 compare_metadata 的比对基准：ORM 模型


def _database_url() -> str:
    """命令行模式下的目标连接串：-x db_url=... 优先，其次为 CONSOLE_DATABASE_URL。"""
    return context.get_x_argument(as_dictionary=True).get("db_url") or DATABASE_URL


def do_run_migrations(connection: Connection) -> None:
    """在给定同步连接上配置迁移上下文并执行。SQLite 不支持大部分 ALTER，开启 batch 模式重建表；其他方言关闭。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table=VERSION_TABLE,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """离线模式：不连接数据库，把迁移渲染为 SQL 文本输出。"""
    url = normalize_database_url(_database_url())
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        version_table=VERSION_TABLE,
        render_as_batch=url.get_backend_name() == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """命令行在线模式：创建异步引擎，经 run_sync 在同步连接上执行迁移，结束后释放连接池。"""
    connectable = make_engine(_database_url())
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式：有调用方传入的连接就直接复用（不会嵌套事件循环），否则自建异步引擎执行。"""
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
