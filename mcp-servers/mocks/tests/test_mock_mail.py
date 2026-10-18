"""mail.send / mail.recall：dry-run 不写、幂等恰好一次、同键不同请求 409、按调用者隔离、撤回权限、发件箱计数接口。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcp_template.spec import CONTROL_FIELDS
from mcp_template.testing import ALICE, BOB, DAVE

AUD = "mcp-mail"
MSG = {"to": ["Customer@Client.example", "bob@corp.example"], "cc": [], "subject": "GW-200 换货进度", "body": "新机已发出。"}


@pytest.fixture
def send(mclient: TestClient, auth: Callable[..., dict[str, str]]) -> Callable[..., Any]:
    def call(key: str | None = "key-mail-0001", sub: str = ALICE, dry_run: bool = False, **over: Any) -> Any:
        body: dict[str, Any] = MSG | over | {"dry_run": dry_run}
        if key:
            body["idempotency_key"] = key
        return mclient.post("/tools/mail.send", json=body, headers=auth(AUD, sub=sub, agent="agent:mailer@1"))

    return call


def _count(client: TestClient, **params: str) -> int:
    count: int = client.get("/_mock/outbox", params=params).json()["count"]
    return count


def test_dry_run_previews_without_sending(mclient: TestClient, send: Callable[..., Any]) -> None:
    r = send(key=None, dry_run=True)
    body = r.json()
    assert r.status_code == 200 and body["dry_run"] is True and body["card"] == {"card_type": "impact_preview"}
    assert body["impact_preview"]["recipients"] == 2 and body["impact_preview"]["external_recipients"] == ["Customer@client.example"]
    assert _count(mclient) == 0


def test_send_exactly_once_with_idempotency(mclient: TestClient, send: Callable[..., Any]) -> None:
    first = send().json()
    assert first["status"] == "sent" and first["deduplicated"] is False and first["compensate_tool"] == "mail.recall"
    assert first["by"] == "agent:mailer@1"
    again = send(to=list(reversed(MSG["to"]))).json()  # 收件人顺序不同仍是同一请求
    assert again["deduplicated"] is True and again["message_id"] == first["message_id"]
    assert _count(mclient) == 1
    item = mclient.get("/_mock/outbox").json()["items"][0]
    assert item["sender"] == ALICE and item["act"] == "agent:mailer@1" and item["run_id"] == "run-test"
    assert "body" not in item


def test_same_key_different_message_is_409(mclient: TestClient, send: Callable[..., Any]) -> None:
    assert send().status_code == 200
    assert send(subject="另一封").status_code == 409
    assert _count(mclient) == 1


def test_key_required_and_validation(mclient: TestClient, send: Callable[..., Any]) -> None:
    assert send(key=None).status_code == 422
    assert send(to=["not-an-address"]).status_code == 422
    assert send(to=[]).status_code == 422
    assert _count(mclient) == 0


def test_key_scoped_per_caller(mclient: TestClient, send: Callable[..., Any]) -> None:
    assert send(sub=ALICE).json()["deduplicated"] is False
    assert send(sub=BOB).json()["deduplicated"] is False
    assert _count(mclient) == 2 and _count(mclient, status="sent") == 2


def test_recall_compensates_send(mclient: TestClient, send: Callable[..., Any], auth: Callable[..., dict[str, str]]) -> None:
    mid = send().json()["message_id"]
    recall = lambda sub=ALICE, key="key-recall-01", dry_run=False, tenant="default": mclient.post(  # noqa: E731
        "/tools/mail.recall",
        json={"message_id": mid, "idempotency_key": key, "dry_run": dry_run},
        headers=auth(AUD, sub=sub, tenant=tenant),
    )
    prev = recall(dry_run=True).json()
    assert prev["impact_preview"] == {"messages": 1, "recipients": 2}
    assert recall(sub=BOB).status_code == 404  # 不是发件人
    assert recall(sub=DAVE, tenant="t2").status_code == 404  # 跨租户
    r = recall().json()
    assert r["recalled"] is True and r["compensate_tool"] == "mail.send" and r["deduplicated"] is False
    assert recall().json()["deduplicated"] is True
    assert recall(key="key-recall-02").json()["recalled"] is False  # 已撤回：新键执行也不再变化
    assert _count(mclient, status="sent") == 0 and _count(mclient, status="recalled") == 1


def test_outbox_filters_and_reset(mclient: TestClient, send: Callable[..., Any]) -> None:
    send()
    assert _count(mclient, tenant="default") == 1 and _count(mclient, tenant="t2") == 0
    assert mclient.get("/_mock/outbox").json()["by_status"] == {"sent": 1}
    mclient.post("/_mock/reset")
    assert _count(mclient) == 0
    assert send().json()["deduplicated"] is False  # 幂等记录一并清空


def test_manifest_write_spec(mclient: TestClient) -> None:
    tools = {t["name"]: t for t in mclient.get("/tools").json()}
    send_t, recall_t = tools["mail.send"], tools["mail.recall"]
    assert send_t["risk_level"] == "write_low" and send_t["compensate_tool"] == "mail.recall"
    assert recall_t["compensate_tool"] == "mail.send" and recall_t["compensates"] == "mail.send"
    for t in (send_t, recall_t):
        assert t["idempotency"]["required"] and t["dry_run"]["supported"]
        assert not set(CONTROL_FIELDS) & set(t["input_schema"]["properties"])
