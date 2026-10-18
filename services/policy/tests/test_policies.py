"""策略服务测试：用 cedar/tests.json 的用例约束 Python 基线评估器，并验证 HTTP 契约（baseline 引擎）。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from policy_service import main
from policy_service.baseline import evaluate

# 与 Cedar 引擎共用的用例集（name / principal / resource / context / expect / expect_reason / action?）
CASES = json.loads((Path(__file__).parents[1] / "cedar" / "tests.json").read_text())


@pytest.fixture
def baseline_client(monkeypatch):
    """显式切到 baseline 引擎的测试客户端。"""
    monkeypatch.setattr(main, "settings", main.load_settings({"POLICY_ENGINE": "baseline"}))
    return TestClient(main.app)


@pytest.mark.parametrize("case", [c for c in CASES if c.get("action", "invoke") == "invoke"], ids=lambda c: c["name"])
def test_baseline_matches_cedar_cases(case):
    """旧入口 evaluate 对每个共享用例（invoke 动作）的决策与原因必须与预期一致。"""
    decision, reason = evaluate(case["principal"], case["resource"], case["context"])
    assert decision == case["expect"]
    if "expect_reason" in case:
        assert reason == case["expect_reason"]


def test_http_contract(baseline_client):
    """/authz/check 返回 allow、dec_ 前缀的判定编号与 engine 字段，且记录进判定日志。"""
    r = baseline_client.post(
        "/authz/check",
        json={
            "subject": "human:1",
            "capability": "cap.crm.read",
            "principal": {"tenant": "t1"},
            "resource": {"risk_level": "read", "tenant": "t1"},
        },
    )
    body = r.json()
    assert r.status_code == 200 and body["decision"] == "allow" and body["decision_id"].startswith("dec_")
    assert body["engine"] == "baseline" and body["policy_ids"] == ["permit:read_in_tenant"]
    last = baseline_client.get("/authz/decisions").json()[-1]
    assert last["decision"] == "allow" and last["engine"] == "baseline"


def test_cross_tenant_approved_high_write_is_denied():
    """跨租户的高风险写即使已审批也必须拒绝：审批不能替代基础权限。"""
    decision, matched = evaluate(
        {"role": "sales", "tenant": "t2"},
        {"system": "CRM", "risk_level": "write_high", "data_class": "internal", "tenant": "t1"},
        {"approved": True, "taint": []},
    )
    assert decision == "deny" and matched == "forbid:cross_tenant"


def test_cross_tenant_denied_for_every_risk_level():
    """跨租户调用在所有风险级别上都被 forbid:cross_tenant 拒绝，permit 无法绕过。"""
    for risk in ("read", "write_low", "write_high"):
        for approved in (False, True):
            decision, matched = evaluate(
                {"role": "sales", "tenant": "t2"},
                {"system": "CRM", "risk_level": risk, "data_class": "internal", "tenant": "t1"},
                {"approved": approved, "taint": []},
            )
            assert (decision, matched) == ("deny", "forbid:cross_tenant"), (risk, approved)


def test_require_approval_only_for_same_tenant_unapproved_high_write():
    """require_approval 只在同租户且未审批的高风险写上出现；同租户已审批为 allow，跨租户为 deny。"""
    resource = {"system": "CRM", "risk_level": "write_high", "data_class": "internal", "tenant": "t1"}
    same_tenant = {"role": "sales", "tenant": "t1"}
    assert evaluate(same_tenant, resource, {"approved": False, "taint": []})[0] == "require_approval"
    assert evaluate(same_tenant, resource, {"approved": True, "taint": []})[0] == "allow"
    assert evaluate({"role": "sales", "tenant": "t2"}, resource, {"approved": False, "taint": []})[0] == "deny"


def test_cross_tenant_check_over_http(baseline_client):
    """HTTP 契约上跨租户的已审批高风险写同样返回 deny。"""
    r = baseline_client.post(
        "/authz/check",
        json={
            "subject": "human:1",
            "capability": "cap.crm.close_batch",
            "principal": {"role": "sales", "tenant": "t2"},
            "resource": {"system": "CRM", "risk_level": "write_high", "data_class": "internal", "tenant": "t1"},
            "context": {"approved": True, "taint": []},
        },
    )
    assert r.status_code == 200 and r.json()["decision"] == "deny" and r.json()["matched"] == "forbid:cross_tenant"


def test_legacy_check_missing_attributes_denied(baseline_client):
    """旧格式缺少资源租户 / 风险级别、或审批字段类型不对时一律 deny（invalid_request），不再按同租户默认放行。"""
    bad_bodies = [
        {"subject": "human:1", "capability": "cap.crm.read", "principal": {"tenant": "t1"}, "resource": {"risk_level": "read"}},
        {"subject": "human:1", "capability": "cap.crm.read", "principal": {"tenant": "t1"}, "resource": {"tenant": "t1"}},
        {"subject": "human:1", "capability": "cap.crm.read", "principal": {}, "resource": {"risk_level": "read", "tenant": "t1"}},
        {
            "subject": "human:1",
            "capability": "cap.crm.close_batch",
            "principal": {"tenant": "t1"},
            "resource": {"risk_level": "write_high", "tenant": "t1"},
            "context": {"approved": "yes", "taint": []},
        },
    ]
    for body in bad_bodies:
        r = baseline_client.post("/authz/check", json=body)
        assert r.status_code == 200 and r.json()["decision"] == "deny" and r.json()["matched"] == "invalid_request", body


def test_legacy_check_agent_context(baseline_client):
    """旧格式在 context.agent 中带 Agent 租户时同样受 forbid:agent_cross_tenant 约束。"""
    r = baseline_client.post(
        "/authz/check",
        json={
            "subject": "human:1",
            "agent": "foreign-agent",
            "capability": "cap.crm.read",
            "principal": {"roles": ["sales"], "tenant": "t1"},
            "resource": {"system": "CRM", "risk_level": "read", "data_class": "internal", "tenant": "t1"},
            "context": {"approved": False, "taint": [], "agent": {"id": "foreign-agent", "tenant": "t2"}},
        },
    )
    assert r.json()["decision"] == "deny" and r.json()["matched"] == "forbid:agent_cross_tenant"
