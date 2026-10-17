"""Console API 测试：发布闸门与归属校验、Runtime 同步（发布 / 灰度 / 回滚）、持久化与租户隔离、节点控制面。"""

import asyncio

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

ADMIN = {"X-Console-User": "alice", "X-Console-Role": "ADMIN"}  # 只能管理自己创建 / 被分配的 Agent
TADMIN = {"X-Console-User": "root", "X-Console-Role": "TENANT_ADMIN"}  # 租户管理员


def test_publish_gate_and_assignment(c, runtime):
    """评测未通过时发布 409；通过后记录灰度并推送到 Runtime；未分配的 ADMIN 不能改可见性；审计留痕。"""
    assert c.post("/agents", json={"agent_id": "a1", "version": "1"}, headers=ADMIN).status_code == 201
    r = c.post("/agents/a1/versions/1/publish", json={"eval_passed": False}, headers=ADMIN)
    assert r.status_code == 409
    runtime.calls.clear()
    body = {"eval_passed": True, "eval_run_ref": "lf_run_1", "canary_percent": 10, "canary_allowlist": ["human:vip"]}
    r = c.post("/agents/a1/versions/1/publish", json=body, headers=ADMIN)
    assert r.status_code == 200 and r.json()["canary"] == {"percent": 10, "allowlist": ["human:vip"]}
    assert [(m, p) for m, p, _ in runtime.calls] == [("POST", "/admin/agents"), ("POST", "/admin/agents/a1/versions/1/publish")]
    assert runtime.calls[1][2] | {} == {
        "eval_run_ref": "lf_run_1",
        "eval_passed": True,
        "rollout_percent": 10,
        "rollout_allowlist": ["human:vip"],
    }
    other = {"X-Console-User": "bob", "X-Console-Role": "ADMIN"}  # 未被分配 a1
    assert c.put("/agents/a1/visibility", json={"roles": ["sales"]}, headers=other).status_code == 403
    assert c.get("/audit", headers=ADMIN).json()[-1]["action"] == "agent.publish"


def test_runtime_failure_leaves_console_unchanged(c, runtime):
    """Runtime 拒绝或不可达：502，Console 侧不标记 published（避免两边状态不一致）。"""
    assert c.post("/agents", json={"agent_id": "a2", "version": "1"}, headers=ADMIN).status_code == 201
    runtime.fail_with = 409
    try:
        r = c.post("/agents/a2/versions/1/publish", json={"eval_passed": True}, headers=ADMIN)
    finally:
        runtime.fail_with = None
    assert r.status_code == 502
    status = {v["version"]: v["status"] for v in c.get("/agents", headers=ADMIN).json() if v["agent_id"] == "a2"}
    assert status == {"1": "draft"}


def test_canary_then_rollback(c, runtime):
    """v1 全量 → v2 灰度 10% → 放量 50% → 一键回滚 v1：每一步都先同步 Runtime，再改 Console 状态。"""
    for v, pct in (("1", 100), ("2", 10)):
        assert c.post("/agents", json={"agent_id": "g", "version": v}, headers=TADMIN).status_code == 201
        assert (
            c.post(f"/agents/g/versions/{v}/publish", json={"eval_passed": True, "canary_percent": pct}, headers=TADMIN).status_code == 200
        )
    runtime.calls.clear()
    r = c.put("/agents/g/versions/2/canary", json={"percent": 50}, headers=TADMIN)
    assert r.status_code == 200 and r.json()["canary"]["percent"] == 50
    r = c.post("/agents/g/versions/1/rollback", headers=TADMIN)
    assert r.status_code == 200 and r.json()["retired"] == ["2"]
    assert [(m, p) for m, p, _ in runtime.calls] == [
        ("PUT", "/admin/agents/g/versions/2/rollout"),
        ("POST", "/admin/agents/g/versions/1/rollback"),
    ]
    status = {v["version"]: (v["status"], v["canary"]["percent"]) for v in c.get("/agents", headers=TADMIN).json() if v["agent_id"] == "g"}
    assert status == {"1": ("published", 100), "2": ("retired", 50)}
    assert c.put("/agents/g/versions/2/canary", json={"percent": 60}, headers=TADMIN).status_code == 409  # 已退役


def test_published_version_is_immutable_and_tenants_are_isolated(c):
    assert c.post("/agents", json={"agent_id": "a1", "version": "1", "system_prompt": "x"}, headers=ADMIN).status_code == 409
    other_tenant = TADMIN | {"X-Console-Tenant": "t2"}
    assert all(v["tenant"] == "t2" for v in c.get("/agents", headers=other_tenant).json())
    assert c.post("/agents/a1/versions/1/rollback", headers=other_tenant).status_code == 404
    assert c.post("/agents", json={"agent_id": "a1", "version": "1"}, headers=other_tenant).status_code == 201  # 同名 Agent 各租户独立


def test_schema_matches_orm(c):
    """迁移建出的 schema 与 ORM 模型一致（Console 表）。"""
    from console_api import db

    async def diff():
        async with db.engine.connect() as conn:

            def _cmp(sync_conn):
                ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True, "version_table": db.VERSION_TABLE})
                return [d for d in compare_metadata(ctx, db.Base.metadata) if "t_console_" in str(d)]

            return await conn.run_sync(_cmp)

    assert asyncio.run(diff()) == []


def test_node_registry_and_routing(c):
    """节点注册后进入路由表分组；绑定权重可查；指令队列拉取即清空。"""
    assert c.post("/nodes/register", json={"node_id": "n1", "labels": ["general", "aftersales"]}).status_code == 200
    assert c.post("/nodes/n1/heartbeat", json={"load": 0.2}).status_code == 200
    assert c.put("/agents/a1/bindings", json=[{"node_group": "aftersales", "weight": 100}], headers=TADMIN).status_code == 200
    rt = c.get("/routing").json()
    assert rt["groups"]["aftersales"] == ["n1"] and rt["bindings"]["a1"] == {"aftersales": 100}
    assert c.post("/nodes/n1/commands", json={"type": "reload_skills"}, headers=TADMIN).status_code == 202
    assert c.get("/nodes/n1/commands").json()[0]["type"] == "reload_skills"
    assert c.get("/nodes/n1/commands").json() == []


def test_publish_retry_after_runtime_already_published(c, runtime):
    """评审项：Runtime 已发布、Console 写库失败后重试——快照写入返回 409 时继续发布（Runtime 发布幂等且带租户校验）。"""
    assert c.post("/agents", json={"agent_id": "rp", "version": "1"}, headers=TADMIN).status_code == 201
    runtime.fail_paths[("POST", "/admin/agents")] = 409
    try:
        r = c.post("/agents/rp/versions/1/publish", json={"eval_passed": True}, headers=TADMIN)
    finally:
        runtime.fail_paths.clear()
    assert r.status_code == 200 and r.json()["status"] == "published"
    assert runtime.params[-1] == {"tenant_id": "default"}  # 发布带租户校验


def test_runtime_rejection_other_than_conflict_still_502(c, runtime):
    assert c.post("/agents", json={"agent_id": "rp2", "version": "1"}, headers=TADMIN).status_code == 201
    runtime.fail_paths[("POST", "/admin/agents")] = 422
    try:
        assert c.post("/agents/rp2/versions/1/publish", json={"eval_passed": True}, headers=TADMIN).status_code == 502
    finally:
        runtime.fail_paths.clear()
