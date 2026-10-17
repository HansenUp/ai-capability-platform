"""S3.2 哈希灰度：按主体哈希落入 rollout_percent、白名单优先、放量与一键回滚。"""

from __future__ import annotations

from typing import Any

import pytest

from runtime.api import rollout
from runtime.state.db import AgentVersion


def _v(id_: int, version: str, percent: int = 100, allowlist: list[str] | None = None) -> AgentVersion:
    return AgentVersion(id=id_, agent_id="a", version=version, rollout_percent=percent, rollout_allowlist=allowlist or [])


def test_bucket_is_stable_and_uniform() -> None:
    assert rollout.bucket("a", "human:x") == rollout.bucket("a", "human:x")
    hits = sum(rollout.bucket("a", f"human:u{i}") < 10 for i in range(5000))
    assert 400 <= hits <= 600  # 10% ± 2%


def test_choose_rules() -> None:
    v1, v2 = _v(1, "1"), _v(2, "2", percent=10, allowlist=["human:vip"])
    assert rollout.choose([v1], "human:x") == (v1, "stable")
    assert rollout.choose([v2, v1], "human:vip") == (v2, "allowlist")
    assert rollout.choose([v2], "human:x") == (v2, "only")  # 没有稳定版时灰度版即全部流量
    canary_subject = next(f"human:u{i}" for i in range(1000) if rollout.bucket("a", f"human:u{i}") < 10)
    stable_subject = next(f"human:u{i}" for i in range(1000) if rollout.bucket("a", f"human:u{i}") >= 10)
    assert rollout.choose([v1, v2], canary_subject) == (v2, "canary")
    assert rollout.choose([v1, v2], stable_subject) == (v1, "stable")
    v3 = _v(3, "3")  # 更新的全量版本：灰度版 v2 比稳定版旧，不再生效
    assert rollout.choose([v1, v2, v3], canary_subject) == (v3, "stable")


def _publish(client: Any, agent_id: str, version: str, **rollout_args: Any) -> None:
    assert client.post("/admin/agents", json={"agent_id": agent_id, "version": version, "system_prompt": f"v{version}"}).status_code == 201
    r = client.post(f"/admin/agents/{agent_id}/versions/{version}/publish", json={"eval_passed": True, **rollout_args})
    assert r.status_code == 200, r.text


def _served(client: Any, agent_id: str, subject: str) -> tuple[str, str]:
    """以 subject 身份对话一次，返回（实际版本，选版原因）。"""
    body = {"agent_id": agent_id, "messages": [{"role": "user", "content": "hi"}], "stream": False}
    run_id = client.post("/v1/messages", json=body, headers={"X-AI-Subject": subject}).json()["run_id"]
    created = client.get(f"/v1/runs/{run_id}/events").json()[0]["payload"]
    return created["agent_version"], created["rollout"]


def test_ten_percent_canary_then_rollback(client: Any) -> None:
    """演示路径：v1 全量 → v2 灰度 10%（白名单优先）→ 一键回滚到 v1。"""
    _publish(client, "gray", "1")
    _publish(client, "gray", "2", rollout_percent=10, rollout_allowlist=["human:vip"])
    subjects = [f"human:u{i}" for i in range(200)]
    served = [_served(client, "gray", s) for s in subjects]
    canary = [s for s, (version, _) in zip(subjects, served, strict=True) if version == "2"]
    assert 8 <= len(canary) <= 34  # 200 人中约 10%（期望 20）
    assert all(reason == ("canary" if version == "2" else "stable") for version, reason in served)
    assert set(canary) == {s for s in subjects if rollout.bucket("gray", s) < 10}  # 分桶确定、可复算
    assert _served(client, "gray", "human:vip") == ("2", "allowlist")

    r = client.post("/admin/agents/gray/versions/1/rollback")
    assert r.status_code == 200 and r.json()["retired"] == ["2"]
    assert {_served(client, "gray", s)[0] for s in canary + ["human:vip"]} == {"1"}
    versions = {v["version"]: v for v in client.get("/admin/agents").json() if v["agent_id"] == "gray"}
    assert versions["2"]["status"] == "retired" and versions["1"]["rollout_percent"] == 100


def test_rollout_adjust_and_guards(client: Any) -> None:
    _publish(client, "gray2", "1")
    _publish(client, "gray2", "2", rollout_percent=0)
    assert _served(client, "gray2", "human:anyone") == ("1", "stable")
    assert client.put("/admin/agents/gray2/versions/2/rollout", json={"percent": 100}).status_code == 200
    assert _served(client, "gray2", "human:anyone") == ("2", "stable")  # 放量到 100 即成为稳定版
    assert client.post("/admin/agents", json={"agent_id": "gray2", "version": "3"}).status_code == 201
    assert client.post("/admin/agents/gray2/versions/3/rollback").status_code == 409  # 草稿不能作为回滚目标
    assert client.put("/admin/agents/gray2/versions/3/rollout", json={"percent": 50}).status_code == 409
    assert client.put("/admin/agents/gray2/versions/9/rollout", json={"percent": 50}).status_code == 404


@pytest.mark.parametrize("percent", [-1, 101])
def test_rollout_percent_bounds(client: Any, percent: int) -> None:
    r = client.post("/admin/agents/demo/versions/1/publish", json={"eval_passed": True, "rollout_percent": percent})
    assert r.status_code == 422


def test_zero_percent_first_release_is_allowlist_only() -> None:
    """首个版本以 0% + 白名单发布：只有白名单可用，其他主体没有可用版本（不再"全员走灰度"）。"""
    v = _v(1, "1", percent=0, allowlist=["human:vip"])
    assert rollout.choose([v], "human:vip") == (v, "allowlist")
    assert rollout.choose([v], "human:x") == (None, "only")
    assert rollout.choose([_v(1, "1", percent=10)], "human:x")[1] == "only"  # 比例大于 0 时仍全员可用


def _create(client: Any, agent_id: str, version: str, tenant: str = "default") -> int:
    return int(client.post("/admin/agents", json={"agent_id": agent_id, "version": version, "tenant_id": tenant}).status_code)


def test_admin_is_tenant_safe(client: Any) -> None:
    """评审项：回滚不能退役其他租户的同名 Agent；草稿不能被其他租户改写归属；带 tenant_id 的调用校验归属。"""
    assert _create(client, "xt", "1", "ta") == 201
    assert client.post("/admin/agents/xt/versions/1/publish", json={"eval_passed": True}).status_code == 200
    assert _create(client, "xt", "2", "tb") == 201
    assert client.post("/admin/agents/xt/versions/2/publish", json={"eval_passed": True}).status_code == 200
    r = client.post("/admin/agents/xt/versions/2/rollback")
    assert r.status_code == 200 and r.json()["retired"] == []  # ta 的 xt@1 不受影响
    status = {v["version"]: v["status"] for v in client.get("/admin/agents").json() if v["agent_id"] == "xt"}
    assert status == {"1": "published", "2": "published"}
    assert _create(client, "xt", "3", "ta") == 201
    assert _create(client, "xt", "3", "tb") == 409  # 草稿属于 ta，tb 不能改写
    assert client.post("/admin/agents/xt/versions/1/rollback", params={"tenant_id": "tb"}).status_code == 404
    assert client.put("/admin/agents/xt/versions/1/rollout", params={"tenant_id": "tb"}, json={"percent": 100}).status_code == 404


def test_pinned_version_must_be_published(client: Any) -> None:
    """评审项：请求里指定版本只能是已发布版本——回滚后的旧版本、未发布的草稿都不能被直接调用。"""
    _publish(client, "pin", "1")
    _publish(client, "pin", "2")
    assert client.post("/admin/agents/pin/versions/2/rollback").status_code == 200  # pin@1 退役
    assert _create(client, "pin", "3") == 201  # 草稿
    body = {"agent_id": "pin", "messages": [{"role": "user", "content": "hi"}], "stream": False}
    assert client.post("/v1/messages", json=body | {"agent_version": "2"}).status_code == 200
    assert client.post("/v1/messages", json=body | {"agent_version": "1"}).status_code == 404
    assert client.post("/v1/messages", json=body | {"agent_version": "3"}).status_code == 404


def test_only_newest_published_version_can_be_canary(client: Any) -> None:
    """评审项：给较旧的已发布版本降比例会让更新的版本成为唯一版本，拒绝；退役版本不能直接重新发布。"""
    _publish(client, "nw", "1")
    _publish(client, "nw", "2", rollout_percent=20)
    assert client.put("/admin/agents/nw/versions/1/rollout", json={"percent": 50}).status_code == 409
    assert client.put("/admin/agents/nw/versions/2/rollout", json={"percent": 60}).status_code == 200
    assert client.post("/admin/agents/nw/versions/1/publish", json={"eval_passed": True}).status_code == 200  # 已发布：幂等
    assert client.post("/admin/agents/nw/versions/2/rollback").status_code == 200
    assert client.post("/admin/agents/nw/versions/1/publish", json={"eval_passed": True}).status_code == 409  # 已退役
