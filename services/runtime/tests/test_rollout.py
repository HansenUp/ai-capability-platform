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
