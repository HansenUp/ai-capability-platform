"""Pure minimal-projection comparisons: no service or model execution."""

from copy import deepcopy

import pytest

from runtime.api.isolated_evaluation_rules import comparison


def sample():
    return {
        "binding": dict.fromkeys(
            (
                "tenant",
                "subject",
                "agent_id",
                "dataset_id",
                "dataset_version",
                "dataset_revision",
                "dataset_digest",
                "catalog_revision",
                "evaluator_version",
                "scenario",
                "cases",
                "profile_digest",
            ),
            "fixed",
        ),
        "source": "isolated-synthetic",
        "publish_eligible": False,
        "status": "COMMITTED",
        "receipt": {
            "result": {
                "selected_count": 2,
                "verified_count": 2,
                "passed_count": 1,
                "pass_rate": 0.5,
            }
        },
    }


def test_versions_can_differ_without_granting_qualification():
    left = sample()
    right = deepcopy(left)
    left["binding"].update(agent_version="1", agent_snapshot="old")
    right["binding"].update(agent_version="2", agent_snapshot="new")
    right["receipt"]["result"].update(passed_count=2, pass_rate=1)
    assert comparison(left, right) == {
        "comparable": True,
        "reasons": [],
        "pass_rate_delta": 0.5,
        "source": "isolated-synthetic",
        "publish_eligible": False,
    }


@pytest.mark.parametrize("field", list(sample()["binding"]))
@pytest.mark.parametrize("missing", [False, True])
def test_conditions_must_be_present_and_equal(field, missing):
    left, right = sample(), sample()
    if missing:
        del right["binding"][field]
    else:
        right["binding"][field] = "changed"
    value = comparison(left, right)
    assert not value["comparable"] and value["pass_rate_delta"] is None
    assert value["reasons"] == [("MISSING_" if missing else "DIFFERENT_") + field.upper()]


@pytest.mark.parametrize("change", ["missing", "partial", "boolean", "inconsistent", "executing", "source"])
def test_incomplete_results_are_not_zero(change):
    left, right = sample(), sample()
    result = right["receipt"]["result"]
    if change == "missing":
        result.pop("pass_rate")
    elif change == "partial":
        result["verified_count"] = 1
    elif change == "boolean":
        result["pass_rate"] = True
    elif change == "inconsistent":
        result["pass_rate"] = 1
    elif change == "executing":
        right["status"] = "EXECUTING"
    else:
        right["source"] = "external"
    assert comparison(left, right)["pass_rate_delta"] is None
    assert "INCOMPLETE_RESULT" in comparison(left, right)["reasons"]


@pytest.mark.parametrize("field", ["binding", "receipt", "result"])
@pytest.mark.parametrize("malformed", ["not-an-object", [1], 1, True])
def test_malformed_nested_evidence_cannot_produce_a_comparison(field, malformed):
    left, right = sample(), sample()
    if field == "result":
        right["receipt"]["result"] = malformed
    else:
        right[field] = malformed
    result = comparison(left, right)
    assert result["comparable"] is False and result["pass_rate_delta"] is None
    assert result["reasons"]
    assert result["publish_eligible"] is False
