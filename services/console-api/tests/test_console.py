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


NODE = {"Authorization": "Bearer test-node-token"}  # 节点服务令牌（开发态静态令牌，见 conftest）


def test_node_registry_and_routing(c):
    """节点注册后进入路由表分组；绑定权重可查；指令拉取不清空，回执后不再下发（S9.8）。"""
    assert c.post("/nodes/register", json={"node_id": "n1", "labels": ["general", "aftersales"]}, headers=NODE).status_code == 200
    assert c.post("/nodes/n1/heartbeat", json={"load": 0.2}, headers=NODE).status_code == 200
    assert c.put("/agents/a1/bindings", json=[{"node_group": "aftersales", "weight": 100}], headers=TADMIN).status_code == 200
    rt = c.get("/routing").json()
    assert rt["groups"]["aftersales"] == ["n1"] and rt["bindings"]["a1"] == {"aftersales": 100}
    assert c.post("/nodes/n1/commands", json={"type": "reload_skills"}, headers=TADMIN).status_code == 202
    cmds = c.get("/nodes/n1/commands", headers=NODE).json()
    assert cmds[0]["type"] == "reload_skills"
    assert c.get("/nodes/n1/commands", headers=NODE).json() == cmds  # 拉取不清空
    assert c.post(f"/nodes/n1/commands/{cmds[0]['id']}/ack", json={"status": "ok"}, headers=NODE).status_code == 200
    assert c.get("/nodes/n1/commands", headers=NODE).json() == []


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


RUN_ITEM_AT = "2026-10-18T00:30:00.000Z"  # 运行项创建时间
TRACE_AT = "2026-10-18T00:20:00.000Z"  # Trace 时间戳（评测运行时间窗内）


def _langfuse(runs, scores, paged=False, agent=("ev", "1", "default"), traces=None):
    """替身 Langfuse：runs 为 运行名 → (metadata, [(datasetItemId, traceId)])；scores 为 traceId → [eval_pass 值]。
    Trace 的 metadata.attributes 缺省为 agent（agent_id, version, tenant），traces 可按 traceId 覆盖 Trace 字段。
    paged=True 时运行详情不带 datasetRunItems，运行项走 /api/public/dataset-run-items 分页（每页 7 条）。"""
    import httpx

    attrs = {"ai.agent_id": agent[0], "ai.agent_version": agent[1], "ai.tenant_id": agent[2]}

    def handler(request):
        path = request.url.path
        if path.startswith("/api/public/traces/"):
            tid = path.rsplit("/", 1)[-1]
            if tid not in scores:
                return httpx.Response(404)
            body = {"id": tid, "timestamp": TRACE_AT, "metadata": {"attributes": attrs}}
            body |= (traces or {}).get(tid, {})
            return httpx.Response(200, json=body | {"scores": [{"name": "eval_pass", "value": v} for v in scores[tid]]})
        if path == "/api/public/dataset-run-items":
            meta, items = runs[request.url.params["runName"]]
            page = int(request.url.params["page"])
            rows = [{"datasetItemId": i, "traceId": t, "createdAt": RUN_ITEM_AT} for i, t in items]
            return httpx.Response(200, json={"data": rows[(page - 1) * 7 : page * 7], "meta": {"totalPages": (len(rows) + 6) // 7}})
        name = path.rsplit("/", 1)[-1]
        if name not in runs:
            return httpx.Response(404)
        meta, items = runs[name]
        body = {"name": name, "datasetId": "ds-1", "metadata": meta, "createdAt": RUN_ITEM_AT}
        if not paged:
            body["datasetRunItems"] = [{"datasetItemId": i, "traceId": t, "createdAt": RUN_ITEM_AT} for i, t in items]
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def _items(n, prefix="t"):
    return [(f"item-{i}", f"{prefix}-{i}") for i in range(n)]


def test_langfuse_eval_gate(c, runtime, monkeypatch):
    """S4.6 / S8：langfuse 模式——数据集须在该 Agent 的 EVAL_DATASETS 名单内、版本一致、条目数 ≥ EVAL_MIN_ITEMS，
    通过率不信任 metadata，按运行项与 eval_pass 评分重算；0% + 白名单预发布不经闸门。"""
    from console_api import eval_gate

    meta = {"agent_id": "ev", "agent_version": "1", "pass_rate": 1.0}  # metadata 的 pass_rate 一律不采信
    scores = {f"t-{i}": [1] for i in range(40)} | {f"f-{i}": [1] for i in range(28)} | {f"f-{i}": [0] for i in range(28, 40)}
    scores |= {"t-dup": [1, 0]}  # 同一 Trace 上有一条不通过的 eval_pass：按不通过计
    runs = {
        "ok": (meta, _items(30) + [("item-0", "t-0")]),  # 重复运行项按条目去重
        "low": (meta, _items(40, "f")),  # 28/40 = 70% < 80%（metadata 自称 100%）
        "few": (meta, _items(10)),  # 条目数不足
        "dup": (meta, _items(29) + [("item-29", "t-dup")] * 3),  # 30 条，item-29 不通过 → 29/30
        "notrace": (meta, _items(29) + [("item-29", None)]),
        "other": ({"agent_id": "ev", "agent_version": "9"}, _items(30)),
    }
    monkeypatch.setattr(eval_gate, "MODE", "langfuse")
    monkeypatch.setattr(eval_gate, "MIN_ITEMS", 30)
    monkeypatch.setattr(eval_gate, "DATASETS", {"ev": ["ticket-summary"], "*": ["generic"]})
    eval_gate.set_transport_for_tests(_langfuse(runs, scores))
    try:
        assert c.post("/agents", json={"agent_id": "ev", "version": "1"}, headers=TADMIN).status_code == 201
        pre = {"canary_percent": 0, "canary_allowlist": ["human:alice"]}
        assert c.post("/agents/ev/versions/1/publish", json={"canary_percent": 0}, headers=TADMIN).status_code == 409  # 预发布需白名单
        assert c.post("/agents/ev/versions/1/publish", json=pre, headers=TADMIN).status_code == 200
        for ref, why in (
            ("langfuse:ticket-summary/low", "70%"),
            ("langfuse:ticket-summary/few", "EVAL_MIN_ITEMS"),
            ("langfuse:ticket-summary/other", "ev@9"),
            ("langfuse:ticket-summary/none", "404"),
            ("langfuse:generic/ok", "not an evaluation dataset"),  # 有单独名单的 Agent 不回落到 "*"
        ):
            r = c.put("/agents/ev/versions/1/canary", json={"percent": 100, "eval_run_ref": ref}, headers=TADMIN)
            assert r.status_code == 409 and why in r.json()["detail"], (ref, r.text)
        monkeypatch.setattr(eval_gate, "MIN_PASS_RATE", 0.99)
        for name in ("dup", "notrace"):  # 同一条目任一运行项不通过 / 运行项缺 Trace：该条目按不通过计
            r = c.put(
                "/agents/ev/versions/1/canary", json={"percent": 100, "eval_run_ref": f"langfuse:ticket-summary/{name}"}, headers=TADMIN
            )
            assert r.status_code == 409 and "29/30" in r.json()["detail"], r.text
        monkeypatch.setattr(eval_gate, "MIN_PASS_RATE", 0.8)
        r = c.put("/agents/ev/versions/1/canary", json={"percent": 100, "eval_run_ref": "langfuse:ticket-summary/ok"}, headers=TADMIN)
        assert r.status_code == 200 and r.json()["eval_run_ref"] == "langfuse:ticket-summary/ok"
        # 请求体自称 eval_passed=true 在 langfuse 模式下无效
        assert c.post("/agents", json={"agent_id": "ev", "version": "2"}, headers=TADMIN).status_code == 201
        assert c.post("/agents/ev/versions/2/publish", json={"eval_passed": True}, headers=TADMIN).status_code == 409
    finally:
        eval_gate.set_transport_for_tests(None)


async def test_langfuse_eval_gate_paged_run_items(monkeypatch):
    """运行详情不带 datasetRunItems 时按 /api/public/dataset-run-items 分页拉取；Langfuse 不可达失败关闭。"""
    import httpx

    from console_api import eval_gate

    meta = {"agent_id": "pg", "agent_version": "2"}
    runs = {"r": (meta, _items(31))}
    monkeypatch.setattr(eval_gate, "MODE", "langfuse")
    monkeypatch.setattr(eval_gate, "MIN_ITEMS", 30)
    monkeypatch.setattr(eval_gate, "DATASETS", {"*": ["ds"]})
    eval_gate.set_transport_for_tests(_langfuse(runs, {f"t-{i}": [1] for i in range(31)}, paged=True, agent=("pg", "2", "default")))
    try:
        assert await eval_gate.check("pg", "2", "langfuse:ds/r", None) is None
        assert "not an evaluation dataset" in await eval_gate.check("pg", "2", "langfuse:other/r", True)

        def down(request):
            raise httpx.ConnectError("down")

        eval_gate.set_transport_for_tests(httpx.MockTransport(down))
        assert "unavailable" in await eval_gate.check("pg", "2", "langfuse:ds/r", True)
    finally:
        eval_gate.set_transport_for_tests(None)


async def test_langfuse_eval_gate_rejects_reused_traces(monkeypatch):
    """评审 #4：Trace 复用防护——同一 traceId 跨条目复用整次拒绝；Trace 的 Agent / 版本 / 租户不符、时间戳在运行时间窗外、
    缺时间戳的条目按不通过计。"""
    from console_api import eval_gate

    meta = {"agent_id": "ev", "agent_version": "1"}
    scores = {f"t-{i}": [1] for i in range(30)}
    runs = {"reuse": (meta, _items(29) + [("item-29", "t-0")]), "ok": (meta, _items(30))}
    monkeypatch.setattr(eval_gate, "MODE", "langfuse")
    monkeypatch.setattr(eval_gate, "MIN_ITEMS", 30)
    monkeypatch.setattr(eval_gate, "MIN_PASS_RATE", 0.99)
    monkeypatch.setattr(eval_gate, "DATASETS", {"*": ["ds"]})
    try:
        eval_gate.set_transport_for_tests(_langfuse(runs, scores))
        assert "reused across dataset items" in await eval_gate.check("ev", "1", "langfuse:ds/reuse", None, "default")
        assert await eval_gate.check("ev", "1", "langfuse:ds/ok", None, "default") is None
        assert "0/30" in await eval_gate.check("ev", "1", "langfuse:ds/ok", None, "t2")  # 其他租户的发布不能用本租户的 Trace
        for override in (
            {"metadata": {"attributes": {"ai.agent_id": "ev", "ai.agent_version": "9", "ai.tenant_id": "default"}}},  # 别的版本
            {"metadata": {"attributes": {"ai.agent_id": "other", "ai.agent_version": "1", "ai.tenant_id": "default"}}},
            {"timestamp": "2026-10-17T00:00:00.000Z"},  # 早于运行时间窗（6 小时）
            {"timestamp": "2026-10-18T02:00:00.000Z"},  # 晚于运行项创建时间
            {"timestamp": None},
        ):
            eval_gate.set_transport_for_tests(_langfuse(runs, scores, traces={"t-3": override}))
            assert "29/30" in await eval_gate.check("ev", "1", "langfuse:ds/ok", None, "default"), override
    finally:
        eval_gate.set_transport_for_tests(None)


def test_console_prod_requires_langfuse_eval_gate(monkeypatch):
    """CONSOLE_ENV=prod：评测闸门必须为 langfuse，且配置 EVAL_DATASETS 与 Langfuse 密钥，否则拒绝启动；dev 不检查。"""
    import pytest

    from console_api import eval_gate

    monkeypatch.setenv("CONSOLE_ENV", "prod")
    monkeypatch.setattr(eval_gate, "MODE", "flag")
    monkeypatch.setattr(eval_gate, "DATASETS", {})
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CONSOLE_EVAL_GATE 必须为 langfuse"):
        eval_gate.validate_for_production()
    assert len(eval_gate.production_problems()) == 3
    monkeypatch.setattr(eval_gate, "MODE", "langfuse")
    monkeypatch.setattr(eval_gate, "DATASETS", {"ticket-summary": ["ticket-summary"]})
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    eval_gate.validate_for_production()
    monkeypatch.setenv("CONSOLE_ENV", "dev")
    monkeypatch.setattr(eval_gate, "MODE", "flag")
    eval_gate.validate_for_production()  # dev 不检查
    assert eval_gate._parse_datasets('{"a": ["x"], "b": "bad"}') == {"a": ["x"]} and eval_gate._parse_datasets("not json") == {}


def test_rollback_to_pre_release_is_refused(c, runtime):
    """S4 评审：0% 预发布版本不能借回滚绕过评测闸门。"""
    assert c.post("/agents", json={"agent_id": "prb", "version": "1"}, headers=TADMIN).status_code == 201
    pre = {"canary_percent": 0, "canary_allowlist": ["human:alice"]}
    assert c.post("/agents/prb/versions/1/publish", json=pre, headers=TADMIN).status_code == 200
    assert c.post("/agents/prb/versions/1/rollback", headers=TADMIN).status_code == 409
