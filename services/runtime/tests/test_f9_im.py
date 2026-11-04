"""DingTalk synthetic inbox only: temporary SQLite, signed fixtures, no message delivery."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
import test_f5_discovery as authkit
from sqlalchemy import func, select

from runtime.api import isolated_im as im
from runtime.state import db

ROOT = im.PREFIX + "/events"


@pytest.fixture(name="idp")
def signed_provider(monkeypatch):
    yield from authkit.signed_provider.__wrapped__(monkeypatch)


@pytest.fixture
def lab(client, idp, monkeypatch):
    from runtime.api import auth
    from runtime.main import app

    if not any(getattr(r, "path", "") == ROOT for r in app.routes):
        app.include_router(im.router)
    tenant = "im-" + uuid4().hex
    bindings = {("dingtalk", "demo-app", "demo-org", "alice"): (tenant, "human:alice", 1)}
    monkeypatch.setitem(app.state._state, "isolated_im", im.ENABLED)
    monkeypatch.setitem(
        app.state._state, "isolated_im_sources", {(auth.settings.jwt_issuer, "im-test", "im-source"): ("demo-app", "demo-org", tenant)}
    )
    monkeypatch.setitem(app.state._state, "isolated_im_bindings", bindings)
    monkeypatch.setitem(app.state._state, "isolated_im_digest_key", b"synthetic-test-digest-key-32-bytes!")

    async def init():
        async with db.engine.begin() as connection:
            await connection.run_sync(im.Metadata.metadata.create_all)

    client.portal.call(init)
    source = authkit.who(idp, sub="im-source", client_id="im-test", tenant=tenant)
    user = authkit.who(idp, tenant=tenant)
    return client, source, user, bindings, tenant


def event(**patch):
    return {
        "profile": "isolated-dingtalk-v1",
        "platform": "dingtalk",
        "app_id": "demo-app",
        "platform_tenant_id": "demo-org",
        "event_id": uuid4().hex,
        "sender_id": "alice",
        "chat_type": "direct",
        "message_type": "text",
        "text": "DO-NOT-PERSIST-BODY",
        **patch,
    }


def recover(client, user, body):
    return client.get(
        ROOT + "/" + body["event_id"], params={k: body[k] for k in ("app_id", "platform_tenant_id", "sender_id")}, headers=user
    )


def test_duplicate_conflict_and_no_body_or_run(lab):
    client, source, user, _, _ = lab
    body = event()
    first = client.post(ROOT, headers=source, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["run_created"] is False and first.json()["message_sent"] is False
    assert client.post(ROOT, headers=source, json=body).json() == first.json()
    assert client.post(ROOT, headers=source, json={**body, "text": "changed"}).status_code == 409
    assert recover(client, user, body).json() == first.json()
    assert "DO-NOT-PERSIST" not in first.text

    async def saved():
        async with db.SessionLocal() as session:
            row = await session.get(im.Receipt, first.json()["receipt_id"])
            assert "text" not in im.Receipt.__table__.columns
            assert "DO-NOT-PERSIST" not in str(row.__dict__)
            assert await session.scalar(select(func.count()).select_from(db.Run).where(db.Run.tenant_id == row.tenant)) == 0

    client.portal.call(saved)


@pytest.mark.parametrize(
    "patch",
    [
        {"chat_type": "group"},
        {"message_type": "file"},
        {"platform": "other"},
        {"subject": "human:alice"},
        {"text": "x" * 4001},
        {"event_id": ""},
    ],
)
def test_unsupported_event(lab, patch):
    client, source, *_ = lab
    assert client.post(ROOT, headers=source, json=event(**patch)).status_code == 422


def test_identity_and_revocation(lab, idp):
    client, source, user, bindings, tenant = lab
    body = event()
    assert client.post(ROOT, headers=user, json=body).status_code == 403
    assert client.post(ROOT, headers=source, json={**body, "platform_tenant_id": "foreign"}).status_code == 403
    assert client.post(ROOT, headers=source, json=body).status_code == 200
    for wrong in [
        source,
        authkit.who(idp, sub="bob", tenant=tenant),
        authkit.who(idp, tenant="other"),
        authkit.who(idp, tenant=tenant, act="app:other"),
    ]:
        assert recover(client, wrong, body).status_code == 403
    bindings[("dingtalk", "demo-app", "demo-org", "alice")] = (tenant, "human:alice", 2)
    assert recover(client, user, body).status_code == 404
    assert client.post(ROOT, headers=source, json=body).status_code == 409
    bindings.clear()
    assert recover(client, user, body).status_code == 403


def test_concurrent_duplicate(lab):
    client, source, user, *_ = lab
    body = event()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: client.post(ROOT, headers=source, json=body), range(4)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert len({r.json()["receipt_id"] for r in results}) == 1
    assert recover(client, user, body).json() == results[0].json()


def test_disabled_and_no_development_identity(lab, monkeypatch):
    from runtime.main import app

    client, source, *_ = lab
    assert client.post(ROOT, headers={"X-Tenant-ID": "default", "X-Subject": "alice"}, json=event()).status_code in (401, 403)
    monkeypatch.setitem(app.state._state, "isolated_im", None)
    assert client.post(ROOT, headers=source, json=event()).status_code == 503


def test_revocation_during_receive_rolls_back(lab, monkeypatch):
    client, source, user, bindings, _ = lab
    body = event()
    original = im.verify_jwt
    calls = 0
    saved_binding = dict(bindings)

    async def revoke(token, request):
        nonlocal calls
        claims = await original(token, request)
        calls += 1
        if calls == 2:
            bindings.clear()
        return claims

    monkeypatch.setattr(im, "verify_jwt", revoke)
    assert client.post(ROOT, headers=source, json=body).status_code == 403
    bindings.update(saved_binding)
    monkeypatch.setattr(im, "verify_jwt", original)
    assert recover(client, user, body).status_code == 404


def test_production_and_missing_digest_key_fail_closed(lab, monkeypatch):
    from runtime.main import app

    client, source, *_ = lab
    monkeypatch.setitem(app.state._state, "isolated_im_digest_key", None)
    assert client.post(ROOT, headers=source, json=event()).status_code == 503
    monkeypatch.setitem(app.state._state, "isolated_im_digest_key", b"synthetic-test-digest-key-32-bytes!")
    monkeypatch.setattr(im, "settings", replace(im.settings, runtime_env="prod"))
    assert client.post(ROOT, headers=source, json=event()).status_code == 503


def test_concurrent_conflicting_content_preserves_winner(lab):
    client, source, user, *_ = lab
    body = event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda text: client.post(ROOT, headers=source, json={**body, "text": text}), ["first", "second"]))
    assert sorted(r.status_code for r in results) == [200, 409]
    winner = next(r.json() for r in results if r.status_code == 200)
    assert recover(client, user, body).json() == winner
