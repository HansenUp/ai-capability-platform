"""工具行为：搜索过滤与游标分页、详情与记录截断、批量关闭（dry-run、幂等重放、键冲突、整体 404 不部分写入、事务原子性）、
批量重开（close_batch 的补偿：恢复关闭前状态、对称的 dry-run / 幂等 / 权限）。

身份用开发头（MCP_TICKET_DEV_AUTH=true）指定，验签与权限矩阵见 test_ticket_auth.py。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ticket_testkit import ALICE, BOB

from mcp_ticket import store
from mcp_ticket.config import get_settings

NOT_FOUND = {"detail": "ticket not found or no permission"}
ALICE_H = {"X-AI-Dev-Sub": ALICE, "X-AI-Dev-Roles": "sales"}
BOB_H = {"X-AI-Dev-Sub": BOB, "X-AI-Dev-Roles": "aftersales"}


@pytest.fixture(autouse=True)
def dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TICKET_DEV_AUTH", "true")


def search(client: TestClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    r = client.post("/tools/ticket.search", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def close(client: TestClient, headers: dict[str, str], ids: list[str], key: str = "key-0000001", dry_run: bool = False) -> Any:
    return client.post("/tools/ticket.close_batch", json={"ids": ids, "idempotency_key": key, "dry_run": dry_run}, headers=headers)


# ---------------- 搜索与分页 ----------------


def test_search_returns_summary_fields_only(client: TestClient) -> None:
    out = search(client, ALICE_H)
    assert set(out["items"][0]) == {"id", "title", "status", "priority", "category", "owner", "created_at", "updated_at"}
    assert out["total"] == 6 and out["next_cursor"] is None


def test_pagination_cursor_walks_all_without_duplicates(client: TestClient) -> None:
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        out = search(client, BOB_H, limit=5, cursor=cursor)
        seen += [i["id"] for i in out["items"]]
        assert out["total"] == 16
        pages += 1
        cursor = out["next_cursor"]
        if cursor is None:
            break
    assert pages == 4 and len(seen) == len(set(seen)) == 16
    created = [i["created_at"] for i in search(client, BOB_H, limit=50)["items"]]
    assert created == sorted(created, reverse=True)  # 按创建时间倒序（mock 数据时区偏移统一为 +08:00）


def test_limit_default_max_and_bad_cursor(client: TestClient) -> None:
    assert len(search(client, BOB_H)["items"]) == 16  # 默认 20 > 16
    assert client.post("/tools/ticket.search", json={"limit": 51}, headers=BOB_H).status_code == 422
    assert client.post("/tools/ticket.search", json={"limit": 0}, headers=BOB_H).status_code == 422
    assert client.post("/tools/ticket.search", json={"cursor": "!!!"}, headers=BOB_H).status_code == 422
    assert client.post("/tools/ticket.search", json={"created_from": "yesterday"}, headers=BOB_H).status_code == 422


def test_search_filters(client: TestClient) -> None:
    ids = lambda **b: {i["id"] for i in search(client, BOB_H, limit=50, **b)["items"]}  # noqa: E731
    assert ids(q="发票") == {"T-1004"}
    assert ids(q="gw-200") >= {"T-1001", "T-1016"}  # 不区分大小写，匹配标题 / 描述
    assert ids(status="closed") == {"T-1001", "T-1004", "T-1007", "T-1010", "T-1014"}
    assert ids(created_from="2026-10-15") == {"T-1006", "T-1008", "T-1013", "T-1016"}
    assert ids(created_from="2026-10-15", created_to="2026-10-15") == {"T-1006"}  # 仅日期的 created_to 含当天
    assert ids(created_to="2026-08-31T00:00:00+08:00") == {"T-1007"}


# ---------------- 详情 ----------------


def test_get_detail_has_full_fields(client: TestClient) -> None:
    r = client.post("/tools/ticket.get", json={"id": "T-1001"}, headers=ALICE_H)
    assert r.status_code == 200
    t = r.json()
    assert t["tenant"] == "default" and t["requester"] == ALICE and t["owner"] == BOB
    assert t["status"] == "closed" and t["closed_at"]
    assert t["customer_feedback"]["score"] == 5
    assert len(t["records"]) == 7 and t["records_truncated"] is False
    assert set(t["records"][0]) == {"time", "actor", "type", "content"}
    assert client.post("/tools/ticket.get", json={"id": "T-1002"}, headers=ALICE_H).json()["customer_feedback"] is None


def test_records_over_50_are_truncated(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = json.loads(Path(get_settings().data_path).read_text(encoding="utf-8"))[0]
    ticket["records"] = [{"time": "2026-09-03T09:00:00+08:00", "actor": "bob", "type": "note", "content": f"记录 {n}"} for n in range(60)]
    data = tmp_path / "many.json"
    data.write_text(json.dumps([ticket], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("MCP_TICKET_DATA", str(data))
    t = client.post("/tools/ticket.get", json={"id": "T-1001"}, headers=ALICE_H).json()
    assert len(t["records"]) == 50 and t["records_truncated"] is True


# ---------------- 批量关闭 ----------------


def test_dry_run_previews_without_writing(client: TestClient) -> None:
    r = close(client, ALICE_H, ["T-1002", "T-1001"], dry_run=True)
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["impact_preview"]["tickets"] == 1 and body["impact_preview"]["ids"] == ["T-1002"]
    assert body["impact_preview"]["already_closed"] == ["T-1001"]
    assert search(client, ALICE_H, status="closed")["total"] == 2  # 仍只有原本关闭的 T-1001 / T-1004
    assert store.load_overrides(get_settings().db_path) == {}


def test_close_then_visible_in_search_and_get(client: TestClient) -> None:
    r = close(client, ALICE_H, ["T-1002"])
    body = r.json()
    assert r.status_code == 200 and body["closed"] == ["T-1002"] and body["deduplicated"] is False and body["by"] == ALICE
    assert body["compensate_tool"] == "ticket.reopen_batch"
    assert "T-1002" in {i["id"] for i in search(client, ALICE_H, status="closed")["items"]}
    assert search(client, ALICE_H, status="open")["total"] == 0
    detail = client.post("/tools/ticket.get", json={"id": "T-1002"}, headers=BOB_H).json()
    assert detail["status"] == "closed" and detail["closed_at"] == body["closed_at"]


def test_idempotent_replay_returns_stored_result(client: TestClient) -> None:
    first = close(client, ALICE_H, ["T-1002", "T-1003"]).json()
    second = close(client, ALICE_H, ["T-1003", "T-1002"]).json()  # 顺序不同视为同一请求
    assert second["deduplicated"] is True
    assert {k: v for k, v in second.items() if k != "deduplicated"} == {k: v for k, v in first.items() if k != "deduplicated"}
    assert len(store.load_overrides(get_settings().db_path)) == 2


def test_same_key_different_request_is_409(client: TestClient) -> None:
    assert close(client, ALICE_H, ["T-1002"]).status_code == 200
    r = close(client, ALICE_H, ["T-1003"])
    assert r.status_code == 409
    assert search(client, ALICE_H, status="processing")["total"] == 3  # T-1003 未被关闭


def test_idempotency_key_is_scoped_per_caller(client: TestClient) -> None:
    assert close(client, ALICE_H, ["T-1002"]).json()["deduplicated"] is False
    other = close(client, BOB_H, ["T-1002"]).json()  # 同键同请求，但调用者不同：不能拿到别人的结果
    assert other["deduplicated"] is False and other["already_closed"] == ["T-1002"]


def test_invisible_id_fails_whole_request_without_partial_write(client: TestClient) -> None:
    r = close(client, ALICE_H, ["T-1002", "T-1006"])  # T-1006 与 alice 无关
    missing = close(client, ALICE_H, ["T-1002", "T-9999"])
    assert r.status_code == missing.status_code == 404
    assert r.json() == missing.json() == NOT_FOUND
    assert store.load_overrides(get_settings().db_path) == {}
    assert search(client, ALICE_H, status="open")["total"] == 1  # T-1002 仍为 open
    assert close(client, ALICE_H, ["T-1002"]).json()["deduplicated"] is False  # 失败的请求没有占用幂等键


def test_close_already_closed_is_noop(client: TestClient) -> None:
    body = close(client, ALICE_H, ["T-1001"]).json()
    assert body["closed"] == [] and body["already_closed"] == ["T-1001"]


def test_close_validation(client: TestClient) -> None:
    assert close(client, ALICE_H, [], key="key-0000001").status_code == 422
    assert close(client, ALICE_H, ["T-1002"], key="short").status_code == 422


def test_status_update_and_idempotency_record_commit_together(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """写幂等记录前失败：状态更新随事务回滚，不留下半成品。"""

    def boom(*_: Any, **__: Any) -> str:
        raise RuntimeError("disk full")

    with monkeypatch.context() as m:
        m.setattr(store, "json", SimpleNamespace(dumps=boom, loads=json.loads))
        with pytest.raises(RuntimeError):
            close(client, ALICE_H, ["T-1002"])
    assert store.load_overrides(get_settings().db_path) == {}
    assert close(client, ALICE_H, ["T-1002"]).json()["deduplicated"] is False


# ---------------- 批量重开（close_batch 的补偿） ----------------


def reopen(client: TestClient, headers: dict[str, str], ids: list[str], key: str = "key-reopen-01", dry_run: bool = False) -> Any:
    return client.post("/tools/ticket.reopen_batch", json={"ids": ids, "idempotency_key": key, "dry_run": dry_run}, headers=headers)


def _status(client: TestClient, tid: str) -> str:
    return client.post("/tools/ticket.get", json={"id": tid}, headers=BOB_H).json()["status"]


def test_reopen_restores_pre_close_status(client: TestClient) -> None:
    assert _status(client, "T-1002") == "open" and _status(client, "T-1003") == "processing"
    closed = close(client, ALICE_H, ["T-1002", "T-1003"]).json()
    assert closed["compensate_tool"] == "ticket.reopen_batch"
    r = reopen(client, ALICE_H, ["T-1003", "T-1002", "T-1001"])  # T-1001 在源系统里就是关闭的：不在补偿范围
    body = r.json()
    assert r.status_code == 200 and body["reopened"] == ["T-1003", "T-1002"] and body["not_reopened"] == ["T-1001"]
    assert body["compensate_tool"] == "ticket.close_batch" and body["deduplicated"] is False and body["by"] == ALICE
    assert _status(client, "T-1002") == "open" and _status(client, "T-1003") == "processing"
    assert client.post("/tools/ticket.get", json={"id": "T-1002"}, headers=BOB_H).json()["closed_at"] is None
    assert close(client, ALICE_H, ["T-1002"], key="key-close-02").json()["closed"] == ["T-1002"]  # 重开后可再次关闭


def test_reopen_dry_run_previews_without_writing(client: TestClient) -> None:
    close(client, ALICE_H, ["T-1002"])
    body = reopen(client, ALICE_H, ["T-1002", "T-1003"], dry_run=True).json()
    assert body["dry_run"] is True and body["card"] == {"card_type": "impact_preview"}
    assert body["impact_preview"]["ids"] == ["T-1002"] and body["impact_preview"]["not_reopened"] == ["T-1003"]
    assert _status(client, "T-1002") == "closed"
    no_key = client.post("/tools/ticket.reopen_batch", json={"ids": ["T-1002"], "dry_run": True}, headers=ALICE_H)
    assert no_key.status_code == 200  # dry-run 不需要幂等键
    assert client.post("/tools/ticket.reopen_batch", json={"ids": ["T-1002"]}, headers=ALICE_H).status_code == 422


def test_reopen_idempotent_replay_and_conflicts(client: TestClient) -> None:
    close(client, ALICE_H, ["T-1002", "T-1003"])
    first = reopen(client, ALICE_H, ["T-1002"]).json()
    again = reopen(client, ALICE_H, ["T-1002"]).json()
    assert again["deduplicated"] is True and again["reopened"] == first["reopened"] == ["T-1002"]
    assert reopen(client, ALICE_H, ["T-1003"]).status_code == 409  # 同键不同请求
    assert reopen(client, ALICE_H, ["T-1002", "T-1003"], key="key-0000001").status_code == 409  # 与 close_batch 的键冲突
    assert _status(client, "T-1003") == "closed"


def test_reopen_scoped_per_caller_and_row_permissions(client: TestClient) -> None:
    close(client, ALICE_H, ["T-1002"])
    assert reopen(client, BOB_H, ["T-1002"]).json()["deduplicated"] is False  # 同键不同调用者：各自独立
    assert _status(client, "T-1002") == "open"
    close(client, BOB_H, ["T-1006"], key="key-bob-0001")
    r = reopen(client, ALICE_H, ["T-1006"], key="key-reopen-02")  # T-1006 与 alice 无关：整体 404，不写入
    assert r.status_code == 404 and r.json() == NOT_FOUND
    assert _status(client, "T-1006") == "closed"
