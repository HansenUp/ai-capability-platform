"""crm.opportunity.search：行级权限（本人 / finance 全租户 / 跨租户隔离）、过滤、confidential 分级。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from mcp_template.testing import ALICE, BOB, DAVE

AUD = "mcp-crm"


def _search(client: TestClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    r = client.post("/tools/crm.opportunity.search", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _ids(out: dict[str, Any]) -> set[str]:
    return {i["id"] for i in out["items"]}


def test_row_level_permissions(cclient: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    alice = _search(cclient, auth(AUD, sub=ALICE, roles=("sales",)))
    assert _ids(alice) == {"O-3001", "O-3002", "O-3005"} and alice["data_class"] == "confidential"
    assert _ids(_search(cclient, auth(AUD, sub=BOB, roles=("finance",)))) == {"O-3001", "O-3002", "O-3003", "O-3004", "O-3005"}
    assert _search(cclient, auth(AUD, sub=BOB, roles=("aftersales",)))["total"] == 0
    assert _ids(_search(cclient, auth(AUD, sub=DAVE, tenant="t2", roles=("sales",)))) == {"O-3006"}
    assert _ids(_search(cclient, auth(AUD, sub=DAVE, tenant="t2", roles=("finance",)))) == {"O-3006", "O-3007"}


def test_filters_and_order(cclient: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    h = lambda: auth(AUD, sub=ALICE, roles=("sales",))  # noqa: E731
    assert _ids(_search(cclient, h(), q="gw-200")) == {"O-3001", "O-3005"}
    assert _ids(_search(cclient, h(), q="华东")) == {"O-3001"}
    assert _ids(_search(cclient, h(), stage="won")) == {"O-3005"}
    items = _search(cclient, h())["items"]
    assert [i["updated_at"] for i in items] == sorted((i["updated_at"] for i in items), reverse=True)
    assert set(items[0]) == {"id", "name", "customer", "stage", "amount", "currency", "owner", "expected_close", "updated_at"}
    assert cclient.post("/tools/crm.opportunity.search", json={"stage": "lost-ish"}, headers=h()).status_code == 422


def test_manifest_declares_confidential(cclient: TestClient) -> None:
    (tool,) = cclient.get("/tools").json()
    assert tool["name"] == "crm.opportunity.search" and tool["risk_level"] == "read" and tool["data_class"] == "confidential"
