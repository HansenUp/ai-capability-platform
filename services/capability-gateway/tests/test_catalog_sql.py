"""目录存储：MemoryCatalog 与 SqlCatalog（Alembic 0001～0003 建表）行为一致——CRUD、同租户重名冲突、绑定、outbox、连接器版本与探活；
Skill 仓储（MemorySkillRepo 与 SqlSkillRepo）：名称归属、版本不可变、row_version 比较并交换、事件。

SQL 后端默认用 SQLite 文件跑；设置 GATEWAY_TEST_DATABASE_URL（如 mysql+asyncmy://root:root@localhost:3316/ai_gateway_test，
库需已存在）时同时对 MySQL 运行（会删除该库中的 cg_* 表）。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from capability_gateway.catalog import Catalog, CatalogConflict, ConnectorTool, ConnectorVersion, MemoryCatalog
from capability_gateway.db import VERSION_TABLE, Base, SqlCatalog, SqlSkillRepo
from capability_gateway.registry import Binding, Capability
from capability_gateway.skill_repo import MemorySkillRepo, SkillConflict, SkillEvent, SkillRepo, SkillVersion

BACKENDS = ["memory", "sqlite"] + (["mysql"] if os.environ.get("GATEWAY_TEST_DATABASE_URL") else [])


async def _sql(url: str) -> SqlCatalog:
    cat = SqlCatalog(url)
    if cat.engine.dialect.name == "mysql":
        async with cat.engine.begin() as conn:
            tables = ("cg_skill_event", "cg_skill_version", "cg_skill", "cg_outbox", "cg_connector_tool", "cg_connector")
            for t in (*tables, "cg_agent_binding", "cg_capability", VERSION_TABLE):
                await conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
    await cat.init()
    return cat


@pytest.fixture(params=BACKENDS)
async def cat(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Catalog]:
    if request.param == "memory":
        yield MemoryCatalog(seed=False)
        return
    url = f"sqlite+aiosqlite:///{tmp_path / 'gw.db'}" if request.param == "sqlite" else os.environ["GATEWAY_TEST_DATABASE_URL"]
    c = await _sql(url)
    try:
        yield c
    finally:
        await c.close()


CAP = Capability(id="cap.a.read", name="a.read", description="读取 A", system="a", input_schema={"type": "object"})


async def test_capability_crud_and_outbox(cat: Catalog) -> None:
    stored = await cat.upsert(CAP.model_copy(update={"row_version": 99}))  # 请求体中的 row_version 被忽略
    assert stored.row_version == 1 and (await cat.get("cap.a.read")) == stored
    again = await cat.upsert(CAP.model_copy(update={"description": "读取 A（新）"}))
    assert again.row_version == 2 and again.description == "读取 A（新）"
    assert (await cat.by_name("a.read")) == again and await cat.by_name("a.read", "other") is None
    await cat.upsert(CAP.model_copy(update={"id": "cap.a.draft", "name": "a.draft", "status": "draft"}))
    assert await cat.by_name("a.draft") is None and len(await cat.list_all()) == 2 and len(await cat.list_published()) == 1
    assert set(await cat.get_many(["cap.a.read", "cap.a.draft", "nope"])) == {"cap.a.read", "cap.a.draft"}
    with pytest.raises(CatalogConflict):
        await cat.upsert(CAP.model_copy(update={"id": "cap.a.other"}))  # 同租户同名
    await cat.upsert(CAP.model_copy(update={"id": "cap.t2.read", "tenant": "t2"}))  # 不同租户允许同名
    events = await cat.outbox_fetch()
    assert [e.aggregate_id for e in events] == ["cap.a.read", "cap.a.read", "cap.a.draft", "cap.t2.read"]
    await cat.outbox_ack([e.id for e in events[:3]])
    assert [e.aggregate_id for e in await cat.outbox_fetch()] == ["cap.t2.read"]


async def test_bindings_bump_row_version(cat: Catalog) -> None:
    await cat.upsert(CAP)
    await cat.outbox_ack([e.id for e in await cat.outbox_fetch()])
    await cat.put_binding(Binding(agent_id="demo", capability_id=CAP.id, kind="core"))
    await cat.put_binding(Binding(agent_id="demo", capability_id=CAP.id, kind="core"))  # 幂等
    await cat.put_binding(Binding(agent_id="*", capability_id=CAP.id, kind="discoverable"))
    assert (await cat.get(CAP.id)).row_version == 3  # type: ignore[union-attr]
    assert len(await cat.outbox_fetch()) == 2
    assert await cat.bound_ids("demo", "core") == {CAP.id} and await cat.bound_ids("x", "discoverable") == {CAP.id}
    assert await cat.bound_ids("x", "core") == set()
    assert [b.agent_id for b in await cat.bindings(capability_id=CAP.id)] == ["*", "demo"]
    assert await cat.delete_binding(Binding(agent_id="demo", capability_id=CAP.id, kind="core"))
    assert not await cat.delete_binding(Binding(agent_id="demo", capability_id=CAP.id, kind="core"))
    with pytest.raises(KeyError):
        await cat.put_binding(Binding(agent_id="demo", capability_id="nope"))


async def test_connector_versions_and_probe_state(cat: Catalog) -> None:
    await cat.put_connector(ConnectorVersion(name="c", version="1", endpoint="http://c1", system="a"))
    await cat.put_connector(ConnectorVersion(name="c", version="2", endpoint="http://c2", system="a"))
    assert await cat.active_connector("c") is None
    assert (await cat.activate("c", "1")).status == "active"
    assert (await cat.activate("c", "2")).endpoint == "http://c2"
    assert [v.status for v in await cat.list_connectors("c")] == ["inactive", "active"]
    assert (await cat.connector_for(CAP)).version == "2"  # type: ignore[union-attr]  # 按 system 匹配
    cv = await cat.active_connector("c")
    assert cv is not None
    tool = ConnectorTool(connector="c", version="2", name="a.read", spec={"name": "a.read"}, read_only=True)
    await cat.save_probe(cv.model_copy(update={"health": "up", "manifest_hash": "h" * 64, "last_probe_at": 1.5}), [tool])
    got = await cat.get_connector("c", "2")
    assert got is not None and got.health == "up" and got.manifest_hash == "h" * 64 and got.last_probe_at == 1.5
    assert await cat.connector_tools("c", "2") == [tool]
    await cat.put_connector(ConnectorVersion(name="c", version="2", endpoint="http://c2b", system="a"))  # 更新地址不改状态
    got = await cat.get_connector("c", "2")
    assert got is not None and got.endpoint == "http://c2b" and got.status == "active" and got.health == "up"
    with pytest.raises(KeyError):
        await cat.activate("c", "9")


async def test_migration_matches_models(tmp_path: Path) -> None:
    """0001～0003 迁移建出的 schema 与 ORM 模型一致（autogenerate 无差异），版本表为 cg_alembic_version。"""
    cat = await _sql(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    try:
        async with cat.engine.connect() as conn:

            def diff(sync_conn):  # type: ignore[no-untyped-def]
                mc = MigrationContext.configure(sync_conn, opts={"version_table": VERSION_TABLE, "compare_type": True})
                return compare_metadata(mc, Base.metadata)

            assert await conn.run_sync(diff) == []
            q = text(f"SELECT version_num FROM {VERSION_TABLE}")  # noqa: S608 —— 常量表名
            assert (await conn.execute(q)).scalar() == "0003"
    finally:
        await cat.close()


def _skill_repo(cat: Catalog) -> SkillRepo:
    return SqlSkillRepo(cat.session) if isinstance(cat, SqlCatalog) else MemorySkillRepo()


async def test_skill_repo_versions_are_immutable_and_cas(cat: Catalog) -> None:
    repo = _skill_repo(cat)
    rec = SkillVersion(
        name="s1",
        version="1.0.0",
        tenant="t1",
        status="submitted",
        manifest={"files": []},
        source_key="k",
        source_sha256="0" * 64,
        submitted_by="frank",
    )
    await repo.create(rec, SkillEvent(name="s1", version="1.0.0", event="submitted", actor="frank"))
    with pytest.raises(SkillConflict) as e:
        await repo.create(rec, SkillEvent(name="s1", version="1.0.0", event="submitted"))
    assert e.value.code == "version_exists"
    with pytest.raises(SkillConflict) as e:
        await repo.create(rec.model_copy(update={"version": "2.0.0", "tenant": "t2"}), SkillEvent(name="s1", version="2.0.0", event="x"))
    assert e.value.code == "name_taken"
    got = await repo.get("s1", "1.0.0")
    assert got is not None and got.row_version == 1 and got.created_at.tzinfo is not None
    saved = await repo.save(
        got.model_copy(update={"status": "scanned", "scan_report": {"passed": True}}),
        [SkillEvent(name="s1", version="1.0.0", event="scanned", actor="system:scanner", detail={"blocking": []})],
    )
    assert saved.status == "scanned" and saved.row_version == 2 and saved.scan_report == {"passed": True}
    with pytest.raises(SkillConflict) as e:  # 旧 row_version 写入失败（并发更新）
        await repo.save(got.model_copy(update={"status": "scan_failed"}), [])
    assert e.value.code == "concurrent_update"
    assert [x.event for x in await repo.events("s1", "1.0.0")] == ["submitted", "scanned"]
    assert [r.version for r in await repo.list(statuses=["scanned"], tenant="t1")] == ["1.0.0"]
    assert await repo.list(statuses=["listed"]) == [] and await repo.list(tenant="t2") == []
