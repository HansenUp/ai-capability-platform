from datetime import UTC, datetime
from uuid import UUID, uuid4

from test_f2_conversations import isolated as isolated
from test_s7_run_api import _user
from test_s7_run_api import idp as idp

from runtime.state import db


def seed(client, headers, count=205):
    scope = client.get("/v1/workbench/identity", headers=headers).json()
    prefix = uuid4().int >> 32 << 32
    ids = [str(UUID(int=prefix + index)) for index in range(count)]
    run_ids = ["run_" + uuid4().hex[:12] + f"{index:04d}" for index in range(count)]
    run_ids.sort()

    async def insert():
        async with db.SessionLocal() as session:
            for cid in ids:
                session.add(
                    db.Conversation(
                        conversation_id=cid,
                        scope_id=scope["scope_id"],
                        tenant_id=scope["tenant_id"],
                        subject=scope["subject"],
                        agent_id="demo",
                    )
                )
            for rid in run_ids:
                session.add(
                    db.Run(
                        run_id=rid,
                        tenant_id=scope["tenant_id"],
                        subject=scope["subject"],
                        agent_id="demo",
                        agent_version="1",
                        model_alias="default-chat",
                        session_id=ids[0],
                        state="completed",
                        last_seq=0,
                        created_at=datetime(2026, 1, 1, tzinfo=UTC),
                    )
                )
                session.add(
                    db.ContinuationInput(
                        attempt_id=str(uuid4()),
                        conversation_id=ids[0],
                        run_id=rid,
                        question="synthetic history",
                        selected_run_ids=[],
                        composed="{}",
                        digest="0" * 64,
                    )
                )
            await session.commit()

    client.portal.call(insert)
    return ids, run_ids


def test_pages_exceed_old_cap_and_registration_reads_selection_only(client, idp):
    headers = _user(idp, "pagination-" + uuid4().hex)
    ids, runs = seed(client, headers)
    for path, expected in [("/v1/workbench/conversations?agent_id=demo", ids), (f"/v1/workbench/conversations/{ids[0]}?", runs)]:
        found, cursor = [], None
        while True:
            params = {"limit": 20}
            if path.startswith("/v1/workbench/conversations?"):
                params["agent_id"] = "demo"
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get(path, headers=headers, params=params)
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["snapshot_guaranteed"] is False and data["page_size"] == 20
            found.extend(item if isinstance(item, str) else item["run_id"] for item in data["items"])
            if data["next_cursor"] is None:
                assert len(data["items"]) == 5
                break
            assert data["next_cursor"] != cursor
            cursor = data["next_cursor"]
        assert found == expected and len(set(found)) == 205
    prepared = client.post(
        "/v1/workbench/create-attempts",
        headers=headers,
        json={
            "attempt_id": str(uuid4()),
            "agent_id": "demo",
            "text": "synthetic continuation",
            "conversation_id": ids[0],
            "selected_run_ids": [runs[-1]],
        },
    )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["status"] == "PREPARED"


def test_cursor_bound_to_scope_agent_conversation_and_page_size(client, idp):
    headers = _user(idp, "cursor-" + uuid4().hex)
    ids, _ = seed(client, headers, 3)
    base = "/v1/workbench/conversations"
    cursor = client.get(base, headers=headers, params={"agent_id": "demo", "limit": 1}).json()["next_cursor"]
    for changed in [dict(agent_id="other", limit=1), dict(agent_id="demo", limit=2)]:
        assert client.get(base, headers=headers, params=changed | {"cursor": cursor}).status_code == 400
    for other in [_user(idp, "other-cursor"), _user(idp, "other-cursor", tenant="foreign")]:
        assert client.get(base, headers=other, params={"agent_id": "demo", "limit": 1, "cursor": cursor}).status_code == 400
    for invalid in [cursor[:-5] + "AAAAA", "[]", "", "a" * 2049]:
        assert client.get(base, headers=headers, params={"agent_id": "demo", "limit": 1, "cursor": invalid}).status_code in (400, 422)
    history = client.get(f"{base}/{ids[0]}", headers=headers, params={"limit": 1}).json()["next_cursor"]
    assert client.get(f"{base}/{ids[1]}", headers=headers, params={"limit": 1, "cursor": history}).status_code == 400
    assert client.get(f"{base}/{ids[0]}", headers=headers, params={"limit": 1, "cursor": cursor}).status_code == 400
    assert client.get(base, headers=headers, params={"agent_id": "demo", "limit": 201}).status_code == 422


def test_live_paging_does_not_claim_snapshot_or_repeat_anchor(client, idp):
    headers = _user(idp, "live-" + uuid4().hex)
    ids, _ = seed(client, headers, 3)
    base = "/v1/workbench/conversations"
    first = client.get(base, headers=headers, params={"agent_id": "demo", "limit": 1}).json()

    async def remove_anchor():
        async with db.SessionLocal() as session:
            await session.delete(await session.get(db.Conversation, ids[0]))
            await session.commit()

    client.portal.call(remove_anchor)
    after = client.get(base, headers=headers, params={"agent_id": "demo", "limit": 1, "cursor": first["next_cursor"]}).json()
    assert after["items"] == [ids[1]] and after["snapshot_guaranteed"] is False
