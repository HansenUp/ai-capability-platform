"""Pure evidence fixtures, not claimed as executed Runtime Runs or browser tests."""

from copy import deepcopy

import pytest

from runtime.api.isolated_evaluation_rules import CASES, TEXT, TOOLS, Verdict, aggregate, evaluate


def evidence(scenario, case):
    output = {}
    text = TEXT.get((scenario, case), "模拟销售摘要：S-1、S-2")
    if scenario == "sales-summary-readonly" and case == "success":
        output = {"data_class": "confidential", "items": [{"id": "S-1", "amount": "100"}, {"id": "S-2", "amount": None}]}
    if scenario == "order-metrics-readonly":
        count = {"success": 3, "incomplete": 1, "empty": 0}[case]
        groups = [{"total_minor": "12000"}, {"total_minor": "500"}] if case == "success" else []
        if case == "incomplete":
            groups = [{"total_minor": None}]
        output = {"order_count": count, "complete": True, "currencies": groups}
        text = f"模拟经营摘要：当前创建窗口订单数 {count}"
    return [
        {"seq": 1, "type": "run.created", "payload": {}},
        {"seq": 2, "type": "tool.requested", "payload": {"tool": TOOLS[scenario], "call_id": "read"}},
        {"seq": 3, "type": "tool.result", "payload": {
            "call_id": "read", "status": "error" if case == "failure" else "ok", "output": output,
        }},
        {"seq": 4, "type": "model.response", "payload": {"text": text}},
    ]


@pytest.mark.parametrize(("scenario", "case"), [(s, c) for s, cases in CASES.items() for c in sorted(cases)])
def test_fixed_cases(scenario, case):
    verdict = evaluate(scenario, case, "completed", evidence(scenario, case))
    assert verdict == Verdict(case, "passed", ())


@pytest.mark.parametrize("state", ["submitted", "working", "failed", "canceled", "input-required", "auth-required"])
def test_nonterminal_and_failed_run_never_pass_expected_failure(state):
    assert (
        evaluate("ticket-summary-readonly", "failure", state, evidence("ticket-summary-readonly", "failure")).judgement
        == "not_verifiable"
    )


@pytest.mark.parametrize("change", ["gap", "duplicate", "bool_seq", "no_answer", "no_tool_result", "bad_creation", "run_error"])
def test_missing_evidence_unknown(change):
    rows = evidence("ticket-summary-readonly", "success")
    if change in {"gap", "duplicate", "bool_seq"}:
        rows[1]["seq"] = {"gap": 3, "duplicate": 1, "bool_seq": True}[change]
    elif change == "no_answer":
        rows[3]["payload"] = {}
    elif change == "no_tool_result":
        rows[2]["type"] = "model.requested"
    elif change == "bad_creation":
        rows[0]["type"] = "model.requested"
    else:
        rows.append({"seq": 5, "type": "run.error", "payload": {}})
    assert evaluate("ticket-summary-readonly", "success", "completed", rows).judgement == "not_verifiable"


@pytest.mark.parametrize("change", ["tool", "status", "call", "extra", "interaction", "leak", "wrong_answer"])
def test_observed_contract_violations_fail(change):
    rows = evidence("ticket-summary-readonly", "success")
    if change == "tool":
        rows[1]["payload"]["tool"] = "ticket.close_batch"
    elif change == "status":
        rows[2]["payload"]["status"] = "error"
    elif change == "call":
        rows[2]["payload"]["call_id"] = "other"
    elif change == "extra":
        rows.append({"seq": 5, "type": "tool.requested", "payload": {"tool": "echo"}})
    elif change == "interaction":
        rows.append({"seq": 5, "type": "input.requested", "payload": {}})
    elif change == "leak":
        rows[2]["payload"]["output"] = {"email": "NEVER-SYNTHETIC-SECRET"}
    else:
        rows[3]["payload"]["text"] = "wrong"
    verdict = evaluate("ticket-summary-readonly", "success", "completed", rows)
    assert verdict.judgement == "not_passed"
    assert "NEVER-" not in repr(verdict)


def test_failure_must_not_generate_success_summary():
    rows = evidence("ticket-summary-readonly", "failure")
    rows.append({"seq": 5, "type": "model.response", "payload": {"text": "模拟工单摘要：invented"}})
    assert "SUCCESS_AFTER_TOOL_FAILURE" in evaluate("ticket-summary-readonly", "failure", "completed", rows).reasons


@pytest.mark.parametrize("value", [0, "0", False, "missing"])
def test_unknown_order_amount_not_zero_or_missing(value):
    rows = evidence("order-metrics-readonly", "incomplete")
    group = rows[2]["payload"]["output"]["currencies"][0]
    if value == "missing":
        group.clear()
    else:
        group["total_minor"] = value
    assert "UNKNOWN_AMOUNT_REPLACED" in evaluate("order-metrics-readonly", "incomplete", "completed", rows).reasons


def test_sales_sensitive_projection_and_unknown():
    original = evidence("sales-summary-readonly", "success")
    for field in ("owner", "notes", "email"):
        rows = deepcopy(original)
        rows[2]["payload"]["output"]["items"][0][field] = "synthetic"
        assert evaluate("sales-summary-readonly", "success", "completed", rows).judgement == "not_passed"
    original[2]["payload"]["output"]["items"][1].pop("amount")
    assert evaluate("sales-summary-readonly", "success", "completed", original).judgement == "not_passed"


def test_aggregate_partial_unknown_and_fixed_denominator():
    passed = Verdict("success", "passed", ())
    failed = Verdict("failure", "not_passed", ("EXPECTED_RESPONSE_MISSING",))
    assert aggregate(["success", "failure"], [passed])["pass_rate"] is None
    assert aggregate(["success", "failure"], [passed, Verdict("failure", "not_verifiable", ("RUN_ERROR",))])["pass_rate"] is None
    result = aggregate(["success", "failure"], [passed, failed])
    assert result["pass_rate"] == 0.5
    assert result["selected_count"] == 2
    assert result["verified_count"] == 2
    assert result["source"] == "isolated-synthetic"
    assert result["publish_eligible"] is False
    assert aggregate(["failure"], [failed])["pass_rate"] == 0
    for selected, values in [([], []), (["success", "success"], [passed]), (["success"], [passed, passed]), (["success"], [failed])]:
        with pytest.raises(ValueError):
            aggregate(selected, values)


def test_unknown_catalog_rejected():
    with pytest.raises(ValueError):
        evaluate("unknown", "success", "completed", [])
