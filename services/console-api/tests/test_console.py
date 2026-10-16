"""Console API 测试：发布闸门与归属校验、节点注册 / 心跳 / 绑定 / 路由表 / 指令队列。"""

from fastapi.testclient import TestClient

from console_api.main import app

c = TestClient(app)
ADMIN = {"X-Console-User": "alice", "X-Console-Role": "ADMIN"}  # 只能管理自己创建 / 被分配的 Agent
TADMIN = {"X-Console-User": "root", "X-Console-Role": "TENANT_ADMIN"}  # 租户管理员


def test_publish_gate_and_assignment():
    """评测未通过时发布 409；通过后记录灰度；未分配的 ADMIN 不能改可见性；审计留痕。"""
    assert c.post("/agents", json={"agent_id": "a1", "version": "1"}, headers=ADMIN).status_code == 201
    r = c.post("/agents/a1/versions/1/publish", json={"eval_passed": False}, headers=ADMIN)
    assert r.status_code == 409
    r = c.post("/agents/a1/versions/1/publish", json={"eval_passed": True, "eval_run_ref": "lf_run_1", "canary_percent": 10}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["canary"]["percent"] == 10
    # 另一个未被分配 a1 的 ADMIN 不能操作它
    other = {"X-Console-User": "bob", "X-Console-Role": "ADMIN"}
    assert c.put("/agents/a1/visibility", json={"roles": ["sales"]}, headers=other).status_code == 403
    assert c.get("/audit", headers=ADMIN).json()[-1]["action"] == "agent.publish"


def test_node_registry_and_routing():
    """节点注册后进入路由表分组；绑定权重可查；指令队列拉取即清空。"""
    assert c.post("/nodes/register", json={"node_id": "n1", "labels": ["general", "aftersales"]}).status_code == 200
    assert c.post("/nodes/n1/heartbeat", json={"load": 0.2}).status_code == 200
    assert c.put("/agents/a1/bindings", json=[{"node_group": "aftersales", "weight": 100}], headers=TADMIN).status_code == 200
    rt = c.get("/routing").json()
    assert rt["groups"]["aftersales"] == ["n1"] and rt["bindings"]["a1"] == {"aftersales": 100}
    assert c.post("/nodes/n1/commands", json={"type": "reload_skills"}, headers=TADMIN).status_code == 202
    assert c.get("/nodes/n1/commands").json()[0]["type"] == "reload_skills"
    assert c.get("/nodes/n1/commands").json() == []
