from dataclasses import replace
from uuid import uuid4

import pytest
from test_s7_run_api import _admin, _publish, _user, _wait
from test_s7_run_api import idp as idp

from runtime.api import conversations
from runtime.state import db


@pytest.fixture(autouse=True)
def isolated(monkeypatch, client):
    monkeypatch.setattr(conversations, "settings", replace(conversations.settings, isolated_conversations=True))

    async def initialize():
        assert db.engine.url.get_backend_name() == "sqlite"
        async with db.engine.begin() as connection:
            await connection.run_sync(
                lambda sync: db.Base.metadata.create_all(sync, tables=[db.Conversation.__table__, db.ContinuationInput.__table__])
            )

    client.portal.call(initialize)


def test_persisted_conversation_snapshot_and_explicit_receipt(client, idp):
    alice = _user(idp, "f2-" + uuid4().hex)
    bob = _user(idp, "f2-other")
    agent = _publish(client, {"tools": ["echo"]}, _admin(idp))
    cid = str(uuid4())

    def prepare(text, history):
        return client.post(
            "/v1/workbench/create-attempts",
            headers=alice,
            json={"attempt_id": str(uuid4()), "agent_id": agent, "text": text, "conversation_id": cid, "selected_run_ids": history},
        )

    first = prepare("synthetic first", [])
    assert first.status_code == 200, first.text
    row = first.json()
    assert client.get(f"/v1/runs/{row['run_id']}", headers=alice).status_code == 404
    assert prepare("blocked", []).status_code == 409
    assert client.get(f"/v1/workbench/conversations/{cid}", headers=bob).status_code == 404
    assert client.get(f"/v1/workbench/conversations/{cid}", headers=alice).json()["items"] == []
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    evidence = {"fingerprint": row["fingerprint"], "revision": row["revision"]}
    assert client.post(path + "/execute", headers=alice, json=evidence | {"value": "changed"}).status_code == 409
    done = client.post(path + "/execute", headers=alice, json=evidence | {"value": "synthetic first"}).json()
    assert done["status"] == "COMMITTED"
    _wait(client, row["run_id"], alice, "completed")
    assert client.post(
        path + "/acknowledge", headers=alice, json={"fingerprint": done["fingerprint"], "revision": done["revision"]}
    ).json()["acknowledged"]
    listing = client.get(f"/v1/workbench/conversations/{cid}", headers=alice).json()
    assert listing["items"] == [{"run_id": row["run_id"], "state": "completed", "selectable": True}]
    assert prepare("bad", ["foreign-run"]).status_code == 409
    second = prepare("synthetic second", [row["run_id"]])
    assert second.status_code == 200, second.text
    saved = second.json()

    async def snapshot():
        async with db.SessionLocal() as session:
            stored = await session.get(db.ContinuationInput, saved["attempt_id"])
            assert "synthetic first" in stored.composed
            assert "synthetic second" in stored.composed
            assert stored.selected_run_ids == [row["run_id"]]
            assert await session.get(db.Run, saved["run_id"]) is None

    client.portal.call(snapshot)
    assert client.get("/v1/workbench/attempts", headers=alice).json()["active"] == saved
    # Disabling isolated mode must not execute a continuation as an ordinary create.
    old = conversations.settings
    try:
        conversations.settings = replace(old, isolated_conversations=False)
        outcome = client.post(
            f"/v1/workbench/attempts/{saved['attempt_id']}/execute",
            headers=alice,
            json={"fingerprint": saved["fingerprint"], "revision": saved["revision"], "value": "synthetic second"},
        ).json()
        assert outcome["status"] == "REJECTED_NO_EFFECT"
    finally:
        conversations.settings = old


def test_production_and_default_disable(client, idp, monkeypatch):
    alice = _user(idp, "f2-disabled")
    for settings in [replace(conversations.settings, isolated_conversations=False), replace(conversations.settings, runtime_env="prod")]:
        monkeypatch.setattr(conversations, "settings", settings)
        assert client.get("/v1/workbench/conversations?agent_id=demo", headers=alice).status_code == 404


@pytest.mark.parametrize("mismatch", ["subject", "tenant", "agent", "history", "duplicate"])
def test_history_binding_rejects_changed_scope(client, idp, mismatch):
    alice = _user(idp, "owner-" + uuid4().hex)
    agent = _publish(client, {"tools": ["echo"]}, _admin(idp))
    body = {"attempt_id": str(uuid4()), "agent_id": agent, "text": "synthetic binding", "conversation_id": str(uuid4())}
    first = client.post("/v1/workbench/create-attempts", headers=alice, json=body)
    assert first.status_code == 200
    headers = alice
    changed = dict(body)
    if mismatch == "subject":
        headers = _user(idp, "different")
    elif mismatch == "tenant":
        headers = _user(idp, "different", tenant="foreign")
    elif mismatch == "agent":
        changed["agent_id"] = "demo"
    elif mismatch == "history":
        changed["selected_run_ids"] = ["not-owned"]
    else:
        changed["selected_run_ids"] = ["same", "same"]
    response = client.post("/v1/workbench/create-attempts", headers=headers, json=changed)
    assert response.status_code in (404, 409)
    assert client.get("/v1/workbench/attempts", headers=alice).json()["active"] == first.json()


@pytest.mark.parametrize("field", ["composed", "digest", "run_id"])
def test_corrupted_snapshot_fails_without_run(client, idp, field):
    alice = _user(idp, "tamper-" + uuid4().hex)
    agent = _publish(client, {"tools": ["echo"]}, _admin(idp))
    row = client.post(
        "/v1/workbench/create-attempts",
        headers=alice,
        json={"attempt_id": str(uuid4()), "agent_id": agent, "text": "synthetic", "conversation_id": str(uuid4())},
    ).json()

    async def corrupt():
        async with db.SessionLocal() as session:
            saved = await session.get(db.ContinuationInput, row["attempt_id"])
            setattr(saved, field, "tampered")
            await session.commit()

    client.portal.call(corrupt)
    result = client.post(
        f"/v1/workbench/attempts/{row['attempt_id']}/execute",
        headers=alice,
        json={"fingerprint": row["fingerprint"], "revision": row["revision"], "value": "synthetic"},
    )
    assert result.json()["status"] == "REJECTED_NO_EFFECT"
    assert client.get(f"/v1/runs/{row['run_id']}", headers=alice).status_code == 404


@pytest.mark.parametrize(
    "extra", [{"selected_run_ids": ["old-run"]}, {"synthetic_attachments": [{"name": "test.txt", "text": "test"}]}]
)
def test_history_without_conversation_is_structured_validation_error(client, idp, extra):
    user = _user(idp, "invalid-history-" + uuid4().hex)
    response = client.post(
        "/v1/workbench/create-attempts",
        headers=user,
        json={"attempt_id": str(uuid4()), "agent_id": "demo", "text": "synthetic", **extra},
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert "history requires a conversation" in response.json()["detail"][0]["msg"]
    assert client.get("/v1/workbench/attempts", headers=user).json()["active"] is None
