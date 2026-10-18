"""模板工具：manifest 写规范、dry-run 预演、幂等去重（按调用者隔离、同键不同请求 409）、close / reopen 互为补偿。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mcp_template.spec import CONTROL_FIELDS, validate_manifest, write_tool
from mcp_template.testing import ALICE, BOB

ALICE_H = {"X-AI-Dev-Sub": ALICE}
BOB_H = {"X-AI-Dev-Sub": BOB}


@pytest.fixture(autouse=True)
def dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TEMPLATE_DEV_AUTH", "true")


def call(client: TestClient, tool: str, headers: dict[str, str], **body: object) -> dict:
    r = client.post(f"/tools/{tool}", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_manifest_satisfies_write_spec(client: TestClient) -> None:
    manifest = client.get("/tools").json()
    assert validate_manifest(manifest) == []
    tools = {t["name"]: t for t in manifest}
    for name in ("ticket.close_batch", "ticket.reopen_batch"):
        t = tools[name]
        assert t["idempotency"]["required"] and t["dry_run"]["supported"] and t["compensate_tool"] in tools
        assert t["supports_dry_run"] and t["requires_idempotency_key"]
        assert not set(CONTROL_FIELDS) & set(t["input_schema"]["properties"])
        assert t["input_schema"]["required"] == ["ids"]
    assert tools["ticket.close_batch"]["compensate_tool"] == "ticket.reopen_batch"
    assert tools["ticket.reopen_batch"]["compensates"] == "ticket.close_batch"


def test_validate_manifest_flags_violations() -> None:
    bad = write_tool("x.write", "d", {"type": "object", "properties": {}}, compensate_tool="x.undo")
    exposed = {"name": "y.write", "description": "d", "risk_level": "write_low", "input_schema": {"properties": {"dry_run": {}}}}
    errors = validate_manifest([bad, exposed])
    assert any("x.undo" in e for e in errors)
    assert any("control fields" in e for e in errors)
    assert any("idempotency" in e for e in errors) and any("dry_run" in e for e in errors)
    assert any("compensate_tool" in e and "y.write" in e for e in errors)


def test_dry_run_then_idempotent_execute(client: TestClient) -> None:
    body = {"ids": ["T-1", "T-2"], "idempotency_key": "agent:demo:call1:abcd"}
    prev = call(client, "ticket.close_batch", ALICE_H, **body, dry_run=True)
    assert prev["impact_preview"]["tickets"] == 2 and prev["card"]["card_type"] == "impact_preview"
    first = call(client, "ticket.close_batch", ALICE_H, **body)
    second = call(client, "ticket.close_batch", ALICE_H, ids=["T-2", "T-1"], idempotency_key=body["idempotency_key"])
    assert first["closed"] == ["T-1", "T-2"] and first["deduplicated"] is False
    assert second["deduplicated"] is True and second["closed"] == first["closed"]
    assert call(client, "ticket.close_batch", ALICE_H, **body, dry_run=True)["impact_preview"]["tickets"] == 0


def test_key_required_unless_dry_run_and_validation(client: TestClient) -> None:
    assert client.post("/tools/ticket.close_batch", json={"ids": ["T-1"]}, headers=ALICE_H).status_code == 422
    assert client.post("/tools/ticket.close_batch", json={"ids": ["T-1"], "dry_run": True}, headers=ALICE_H).status_code == 200
    assert client.post("/tools/ticket.close_batch", json={"ids": [], "idempotency_key": "key-00001"}, headers=ALICE_H).status_code == 422
    assert client.post("/tools/ticket.close_batch", json={"ids": ["T-1"], "idempotency_key": "short"}, headers=ALICE_H).status_code == 422


def test_same_key_different_request_or_tool_is_409(client: TestClient) -> None:
    call(client, "ticket.close_batch", ALICE_H, ids=["T-1"], idempotency_key="key-00000001")
    r = client.post("/tools/ticket.close_batch", json={"ids": ["T-2"], "idempotency_key": "key-00000001"}, headers=ALICE_H)
    assert r.status_code == 409
    r = client.post("/tools/ticket.reopen_batch", json={"ids": ["T-1"], "idempotency_key": "key-00000001"}, headers=ALICE_H)
    assert r.status_code == 409  # 换了工具也算不同请求


def test_idempotency_key_scoped_per_caller(client: TestClient) -> None:
    call(client, "ticket.close_batch", ALICE_H, ids=["T-1"], idempotency_key="key-00000001")
    other = call(client, "ticket.close_batch", BOB_H, ids=["T-1"], idempotency_key="key-00000001")
    assert other["deduplicated"] is False and other["already_closed"] == ["T-1"]  # 同租户状态共享，但拿不到 alice 的结果


def test_reopen_compensates_close(client: TestClient) -> None:
    closed = call(client, "ticket.close_batch", ALICE_H, ids=["T-1", "T-2"], idempotency_key="key-close-01")
    assert closed["compensate_tool"] == "ticket.reopen_batch"
    prev = call(client, "ticket.reopen_batch", ALICE_H, ids=["T-1", "T-3"], dry_run=True)
    assert prev["impact_preview"]["ids"] == ["T-1"]
    reopened = call(client, "ticket.reopen_batch", ALICE_H, ids=["T-1", "T-3"], idempotency_key="key-reopen-01")
    assert reopened["reopened"] == ["T-1"] and reopened["not_closed"] == ["T-3"]
    assert reopened["compensate_tool"] == "ticket.close_batch"
    again = call(client, "ticket.reopen_batch", ALICE_H, ids=["T-3", "T-1"], idempotency_key="key-reopen-01")
    assert again["deduplicated"] is True and again["reopened"] == ["T-1"]
    assert call(client, "ticket.close_batch", ALICE_H, ids=["T-1"], dry_run=True)["impact_preview"]["ids"] == ["T-1"]


def test_requires_identity(client: TestClient) -> None:
    """未携带任何身份信息时 401；健康检查与 manifest 无需鉴权。"""
    assert client.post("/tools/ticket.search", json={"q": "x"}).status_code == 401
    assert client.get("/healthz").json() == {"status": "ok"}
