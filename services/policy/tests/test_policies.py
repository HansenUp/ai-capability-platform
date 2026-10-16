"""策略服务测试：用 cedar/tests.json 的用例同时约束 Python 基线评估器，并验证 HTTP 契约。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from policy_service.baseline import evaluate
from policy_service.main import app

# 与 Cedar 引擎共用的用例集（name / principal / resource / context / expect）
CASES = json.loads((Path(__file__).parents[1] / "cedar" / "tests.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_baseline_matches_cedar_cases(case):
    """基线评估器对每个共享用例的决策必须与预期一致。"""
    decision, _ = evaluate(case["principal"], case["resource"], case["context"])
    assert decision == case["expect"]


def test_http_contract():
    """/authz/check 返回 allow 与 dec_ 前缀的判定编号，且记录进判定日志。"""
    c = TestClient(app)
    r = c.post(
        "/authz/check",
        json={
            "subject": "human:1",
            "capability": "cap.crm.read",
            "principal": {"tenant": "t1"},
            "resource": {"risk_level": "read", "tenant": "t1"},
        },
    )
    assert r.status_code == 200 and r.json()["decision"] == "allow" and r.json()["decision_id"].startswith("dec_")
    assert c.get("/authz/decisions").json()[-1]["decision"] == "allow"


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


def test_cross_tenant_check_over_http():
    """HTTP 契约上跨租户的已审批高风险写同样返回 deny。"""
    c = TestClient(app)
    r = c.post(
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
