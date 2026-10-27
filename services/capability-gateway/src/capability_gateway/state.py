"""进程级共享状态：目录存储、检索索引、Skill 仓储与包存储（按配置在导入时创建；测试直接 monkeypatch 本模块的属性）。"""

from __future__ import annotations

from capability_gateway import config
from capability_gateway.catalog import Catalog, MemoryCatalog
from capability_gateway.embed import get_embedder
from capability_gateway.search import EsIndex
from capability_gateway.skill_repo import MemorySkillRepo, SkillRepo
from capability_gateway.skill_store import PackageStore, make_store


def make_catalog() -> Catalog:
    """CATALOG_BACKEND=memory（默认，装入开发种子）或 mysql（GATEWAY_DATABASE_URL，启动时迁移；
    种子用 python -m capability_gateway.seed）。"""
    if config.CATALOG_BACKEND == "mysql":
        if not config.DATABASE_URL:
            raise RuntimeError("CATALOG_BACKEND=mysql requires GATEWAY_DATABASE_URL")
        from capability_gateway.db import SqlCatalog

        return SqlCatalog(config.DATABASE_URL, migrate_on_init=config.MIGRATE_ON_START)
    if config.CATALOG_BACKEND != "memory":
        raise RuntimeError(f"unknown CATALOG_BACKEND: {config.CATALOG_BACKEND}")
    return MemoryCatalog()


def make_es() -> EsIndex | None:
    if not config.ES_URL:
        return None
    return EsIndex(config.ES_URL, get_embedder(), alias=config.ES_ALIAS, prefix=f"{config.ES_ALIAS}-v1-")


def make_skill_repo(cat: Catalog) -> SkillRepo:
    """mysql 后端与目录共用连接池（表由同一套迁移创建）；memory 后端用内存仓储。"""
    if config.CATALOG_BACKEND == "mysql":
        from capability_gateway.db import SqlCatalog, SqlSkillRepo

        assert isinstance(cat, SqlCatalog)
        return SqlSkillRepo(cat.session)
    return MemorySkillRepo()


catalog: Catalog = make_catalog()
es: EsIndex | None = make_es()
skills: SkillRepo = make_skill_repo(catalog)
skill_store: PackageStore = make_store()
