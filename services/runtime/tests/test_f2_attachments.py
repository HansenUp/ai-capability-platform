import json
from uuid import uuid4

import pytest
from test_f2_conversations import isolated as isolated
from test_s7_run_api import _admin, _publish, _user, _wait
from test_s7_run_api import idp as idp

from runtime.state import db


@pytest.mark.parametrize(
    "item,reason",
    [
        ({"name": "bad.bin", "text": "synthetic"}, "TXT_FILENAME_REQUIRED"),
        ({"name": "../bad.txt", "text": "synthetic"}, "TXT_FILENAME_REQUIRED"),
        ({"name": "a.txt", "text": "SYNTHETIC_SCAN_REJECT"}, "SYNTHETIC_REJECTED"),
        ({"name": "a.txt", "text": "SYNTHETIC_SCAN_UNAVAILABLE"}, "SCANNER_UNAVAILABLE"),
        ({"name": "a.txt", "text": "\x00"}, "SYNTHETIC_REJECTED"),
        ({"name": "a.txt", "text": "中" * 350000}, "TOO_LARGE"),
    ],
)
def test_failed_preview_cannot_be_bypassed_at_registration(client, idp, item, reason):
    user = _user(idp, "attachment-" + uuid4().hex)
    preview = client.post("/v1/workbench/attachment-preview", headers=user, json={"attachments": [item]})
    assert preview.status_code == 200
    assert preview.json()["items"][0]["reason"] == reason
    assert "text" not in preview.json()["items"][0]
    body = {
        "attempt_id": str(uuid4()),
        "agent_id": "demo",
        "text": "question",
        "conversation_id": str(uuid4()),
        "synthetic_attachments": [item],
    }
    assert client.post("/v1/workbench/create-attempts", headers=user, json=body).status_code == 422
    assert client.get("/v1/workbench/attempts", headers=user).json()["active"] is None


def test_attachment_content_frozen_recovered_and_executed_once(client, idp):
    user = _user(idp, "attachment-owner-" + uuid4().hex)
    agent = _publish(client, {"tools": ["echo"]}, _admin(idp))
    item = {"name": "synthetic.txt", "text": "SYNTHETIC DOCUMENT ONLY"}
    body = {
        "attempt_id": str(uuid4()),
        "agent_id": agent,
        "text": "summarize synthetic",
        "conversation_id": str(uuid4()),
        "synthetic_attachments": [item],
    }
    preview = client.post("/v1/workbench/attachment-preview", headers=user, json={"attachments": [item]}).json()
    assert preview["items"][0]["status"] == "passed"
    first = client.post("/v1/workbench/create-attempts", headers=user, json=body)
    assert first.status_code == 200, first.text
    row = first.json()
    assert client.post("/v1/workbench/create-attempts", headers=user, json=body).json() == row
    assert (
        client.post(
            "/v1/workbench/create-attempts", headers=user, json=body | {"synthetic_attachments": [item | {"text": "changed"}]}
        ).status_code
        == 409
    )
    path = f"/v1/workbench/attempts/{row['attempt_id']}"
    metadata = client.get(path + "/attachments", headers=user)
    assert metadata.status_code == 200
    assert metadata.json()["items"] == preview["items"]
    assert item["text"] not in metadata.text
    assert client.get(path + "/attachments", headers=_user(idp, "other")).status_code == 404
    assert client.get(path + "/attachments", headers=_user(idp, "other", tenant="foreign")).status_code == 404
    evidence = {"fingerprint": row["fingerprint"], "revision": row["revision"], "value": body["text"]}
    done = client.post(path + "/execute", headers=user, json=evidence).json()
    assert done["status"] == "COMMITTED"
    assert client.post(path + "/execute", headers=user, json=evidence).json() == done
    _wait(client, row["run_id"], user, "completed")

    async def verify_input():
        async with db.SessionLocal() as session:
            initial = await session.get(db.RunInput, row["run_id"])
            assert item["text"] in json.dumps(initial.messages)
            saved = await session.get(db.ContinuationInput, row["attempt_id"])
            document = json.loads(saved.composed)["synthetic_attachments"][0]
            assert document["text"] == item["text"]
            assert document["sha256"] == preview["items"][0]["sha256"]

    client.portal.call(verify_input)

    assert client.get(path + "/attachments", headers=user).json()["items"] == preview["items"]
    assert client.post(path + "/acknowledge", headers=user, json={"fingerprint": done["fingerprint"], "revision": done["revision"]}).json()[
        "acknowledged"
    ]


def test_count_and_client_scan_claims_rejected(client, idp):
    user = _user(idp, "attachment-count")
    item = {"name": "test.txt", "text": "synthetic"}
    for items in [[item] * 4, [item | {"verified": True}]]:
        assert client.post("/v1/workbench/attachment-preview", headers=user, json={"attachments": items}).status_code == 422
    assert client.post("/v1/workbench/attachment-preview", json={"attachments": [item]}).status_code == 401
