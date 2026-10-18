"""knowledge.search：ACL（租户 / 角色 / 本人）过滤、不可见文档不参与计数、打分排序、数据分级汇总。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from mcp_mocks.knowledge import terms
from mcp_template.testing import ALICE, BOB, CAROL, DAVE

AUD = "mcp-knowledge"


def _search(client: TestClient, headers: dict[str, str], query: str, **body: Any) -> dict[str, Any]:
    r = client.post("/tools/knowledge.search", json={"query": query, "limit": 20, **body}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _ids(out: dict[str, Any]) -> set[str]:
    return {i["id"] for i in out["items"]}


def test_acl_filters_by_tenant_role_and_user(kclient: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    assert _ids(_search(kclient, auth(AUD, sub=ALICE, roles=("sales",)), "GW-200")) == {"K-001", "K-003", "K-004", "K-006"}
    assert _ids(_search(kclient, auth(AUD, sub=BOB, roles=("aftersales",)), "GW-200")) == {"K-001", "K-003", "K-005"}
    assert _ids(_search(kclient, auth(AUD, sub=CAROL, roles=("platform-admin",)), "GW-200")) == {"K-001", "K-003"}
    assert _ids(_search(kclient, auth(AUD, sub=DAVE, tenant="t2", roles=("sales",)), "GW-200")) == {"K-101", "K-102"}


def test_user_acl_is_personal(kclient: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    """K-006 只授权给 alice 本人：同角色的其他用户看不到。"""
    other_sales = "00000000-0000-4000-8000-00000000e11e"
    assert "K-006" in _ids(_search(kclient, auth(AUD, sub=ALICE, roles=("sales",)), "回访纪要"))
    out = _search(kclient, auth(AUD, sub=other_sales, roles=("sales",)), "回访纪要")
    assert "K-006" not in _ids(out)


def test_invisible_docs_do_not_leak_through_total_or_class(kclient: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    out = _search(kclient, auth(AUD, sub=BOB, roles=("aftersales",)), "折扣审批权限")
    assert out == {"items": [], "total": 0, "data_class": "public"}
    alice = _search(kclient, auth(AUD, sub=ALICE, roles=("sales",)), "折扣审批权限")
    assert alice["items"][0]["id"] == "K-004" and alice["data_class"] == "confidential"


def test_ranking_title_hits_first_and_limit(kclient: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    out = _search(kclient, auth(AUD, sub=ALICE, roles=("sales",)), "GW-200 退换货政策")
    assert out["items"][0]["id"] == "K-001"
    scores = [i["score"] for i in out["items"]]
    assert scores == sorted(scores, reverse=True)
    assert set(out["items"][0]) == {"id", "title", "snippet", "data_class", "score", "updated_at"}
    limited = kclient.post("/tools/knowledge.search", json={"query": "GW-200", "limit": 2}, headers=auth(AUD, roles=("sales",))).json()
    assert len(limited["items"]) == 2 and limited["total"] == 4


def test_validation(kclient: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    assert kclient.post("/tools/knowledge.search", json={"query": ""}, headers=auth(AUD)).status_code == 422
    assert kclient.post("/tools/knowledge.search", json={"query": "x", "limit": 21}, headers=auth(AUD)).status_code == 422


def test_terms() -> None:
    assert terms("GW-200 退换货") == {"gw-200", "退换", "换货"}
    assert terms("票") == {"票"}
