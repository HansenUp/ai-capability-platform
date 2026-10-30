"""Synthetic owner boundary; no network or CRM environment."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNTIME_TESTS = str(Path(__file__).resolve().parents[2] / "services/runtime/tests")

spec = importlib.util.spec_from_file_location("sales", Path(__file__).resolve().parents[1] / "sales_readonly_fixture.py")
sales = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sales)


def test_projection_filter_and_unknown():
    out = sales.search(sales.ROWS, "default", "human:alice", {})
    assert [r["id"] for r in out["items"]] == ["S-1", "S-2"]
    assert set(out["items"][0]) == set(sales.FIELDS)
    assert out["items"][1]["amount"] is None
    assert out["items"][1]["updated_at"] is None
    assert out["items"][1]["expected_close"] is None
    assert "NEVER" not in str(out)
    assert "建议（非执行）[S-1 / 2026-10-29" in sales.summary(out)
    assert "金额未知 USD" in sales.summary(out)
    assert len(sales.search(sales.ROWS, "default", "human:alice", {"q": "企业甲", "stage": "proposal"})["items"]) == 1


@pytest.mark.parametrize(
    "args",
    [
        {"tenant": "foreign"},
        {"subject": "human:bob"},
        {"owner": "human:bob"},
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"stage": "invalid"},
    ],
)
def test_reject_body_authority_and_bounds(args):
    with pytest.raises(ValueError):
        sales.search(sales.ROWS, "default", "human:alice", args)


def test_bound_empty_failure_and_duplicates_order():
    out = sales.search(sales.ROWS, "default", "human:alice", {"limit": 1})
    assert out["truncated"] and out["returned"] == 1
    assert "已截断" in sales.summary(out)
    assert sales.summary({"error": "source failed"}) == "模拟商机查询不可用，未生成摘要。"
    empty = sales.search(sales.ROWS, "default", "human:unknown", {})
    assert empty["items"] == [] and "零业绩" in sales.summary(empty)
    rows = [dict(sales.ROWS[0], id="A"), dict(sales.ROWS[0], id="B")]
    assert [i["id"] for i in sales.search(rows, "default", "human:alice", {})["items"]] == ["B", "A"]


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "100", None])
def test_invalid_amount(value):
    assert sales.project(dict(sales.ROWS[0], amount=value))["amount"] is None


@pytest.mark.asyncio
async def test_signed_identity_and_no_role_bypass(monkeypatch):
    import sys

    sys.path.insert(0, RUNTIME_TESTS)
    from fastapi import HTTPException
    from test_auth import IdP

    from runtime.api import auth
    from runtime.config import settings

    monkeypatch.setenv("EGRESS_ALLOWLIST", "http://idp.test")
    provider = IdP()
    from dataclasses import replace

    monkeypatch.setattr(
        auth,
        "settings",
        replace(
            settings,
            jwt_issuer="http://idp.test/realms/ai-platform",
            jwt_audience="ai-runtime",
            jwks_url="http://idp.test/realms/ai-platform/protocol/openid-connect/certs",
        ),
    )
    auth.set_transport_for_tests(provider.transport())

    def rc(claims, subject="human:alice", tenant="default"):
        return SimpleNamespace(credential=provider.token(claims), gate=SimpleNamespace(subject=subject, tenant_id=tenant))

    try:
        assert len((await sales.read(rc({"sub": "alice"}), {}))["items"]) == 2
        for role in ("finance", "platform-admin"):
            result = await sales.read(rc({"sub": "stranger", "realm_access": {"roles": [role]}}, "human:stranger"), {})
            assert result["items"] == []
        for claims in (
            {"sub": "alice", "act": {}},
            {"sub": "alice", "client_id": "svc"},
            {"sub": "alice", "principal_type": "service"},
            {"sub": "alice", "tenant": "foreign"},
            {"sub": "bob"},
            {"sub": "alice", "exp": 1},
        ):
            with pytest.raises((ValueError, HTTPException)):
                await sales.read(rc(claims), {})
        with pytest.raises((ValueError, HTTPException)):
            await sales.read(SimpleNamespace(credential="forged", gate=SimpleNamespace(subject="human:alice", tenant_id="default")), {})
    finally:
        auth.set_transport_for_tests(None)
