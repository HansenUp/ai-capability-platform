"""Fixture-mounted dataset API: signed identities and disposable SQLite only."""
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_s7_run_api as authkit
from fastapi import HTTPException
from test_auth import bearer

from runtime.api import auth, isolated_datasets as api
from runtime.state import db

ROOT = "/v1/isolated-datasets"

@pytest.fixture(name="idp")
def signed_provider(monkeypatch):
    yield from authkit.idp.__wrapped__(monkeypatch)

@pytest.fixture
def lab(client, monkeypatch):
    from runtime.main import app
    if not any(getattr(r, "path", "") == ROOT for r in app.routes):
        app.include_router(api.router)
    grants = {("default", "alice", "demo"), ("default", "bob", "demo")}
    async def authorize(tenant, user, agent):
        if (tenant, user, agent) not in grants:
            raise HTTPException(403, "not assigned")
    monkeypatch.setitem(app.state._state, "isolated_datasets", api.ENABLED)
    monkeypatch.setitem(app.state._state, "isolated_dataset_authorize", authorize)
    # Issuer is configured by idp before requests; install below per test.
    async def create():
        async with db.engine.begin() as connection:
            await connection.run_sync(api.Base.metadata.create_all)
    client.portal.call(create)
    return client, grants, app


def identities(lab, idp, monkeypatch):
    monkeypatch.setitem(lab[2].state._state, "isolated_dataset_bindings", {
        (auth.settings.jwt_issuer, "default", "human:alice"): "alice",
        (auth.settings.jwt_issuer, "default", "human:bob"): "bob",
    })
    return [bearer(idp.token({"sub": who, "tenant": "default"})) for who in ("alice", "bob")]


def command(action="draft", target=None):
    return {"attempt_id": str(uuid.uuid4()), "action": action, "agent_id": "demo",
            "version": target["version"] if target else uuid.uuid4().hex,
            "registration_id": target["id"] if target else None,
            "expected_revision": target["revision"] if target else 0,
            "proposal": None if action == "freeze" else {"schema_version":1, "catalog_revision":"synthetic-catalog-1",
                "scenario_id":"ticket-summary-readonly", "case_ids":["success", "failure"]}}


def receipt(row):
    return {"fingerprint":row["fingerprint"], "revision":row["revision"]}


def post(c, h, path, body):
    r = c.post(ROOT+path, headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def finish(c, h, cmd):
    p=post(c,h,"/prepare",cmd)
    done=post(c,h,f"/{p['attempt_id']}/execute",receipt(p))
    assert done["status"] == "COMMITTED", done
    post(c,h,f"/{p['attempt_id']}/acknowledge",receipt(done))
    return next(r for r in c.get(ROOT+"?agent_id=demo",headers=h).json()["items"] if r["version"]==cmd["version"])


def test_save_freeze_immutable_and_exact_owner(lab,idp,monkeypatch):
    c = lab[0]
    a, b = identities(lab, idp, monkeypatch)
    row=finish(c,a,command())
    assert row["state"]=="draft"
    assert c.post(ROOT+"/prepare",headers=b,json=command("freeze",row)).status_code==403
    row=finish(c,a,command("edit",row))
    assert row["revision"]==2
    row=finish(c,a,command("freeze",row))
    assert row["state"]=="frozen"
    assert c.post(ROOT+"/prepare",headers=a,json=command("edit",row)).status_code==409
    assert c.post(ROOT+"/prepare",headers=a,json=command()|{"version":row["version"]}).status_code==409


def test_lost_response_recovery_shared_lock_and_ack(lab,idp,monkeypatch):
    c = lab[0]
    a, b = identities(lab, idp, monkeypatch)
    cmd = command()
    p = post(c, a, "/prepare", cmd)
    assert c.get(ROOT+"/ledger",headers=a).json()["active"]==p
    assert c.post(ROOT+"/prepare",headers=a,json=command()).status_code==409
    assert c.post(
        "/v1/workbench/create-attempts", headers=a,
        json={"attempt_id": str(uuid.uuid4()), "agent_id": "demo", "text": "synthetic"},
    ).status_code == 409
    assert c.post(ROOT+f"/{p['attempt_id']}/execute",headers=b,json=receipt(p)).status_code==404
    done=post(c,a,f"/{p['attempt_id']}/execute",receipt(p))
    assert post(c,a,f"/{p['attempt_id']}/execute",receipt(p))==done
    assert c.get(ROOT+"/ledger",headers=a).json()["active"]==done
    assert "snapshot" not in done["evidence"] and "proposal" not in done["command"]
    assert c.post(ROOT+f"/{p['attempt_id']}/acknowledge",headers=a,json=receipt(p)).status_code==409
    post(c,a,f"/{p['attempt_id']}/acknowledge",receipt(done))
    assert c.get(ROOT+"/ledger",headers=a).json()["active"] is None


def test_revocation_rechecked_and_fault_atomic(lab,idp,monkeypatch):
    c, grants, _ = lab
    a, _ = identities(lab, idp, monkeypatch)
    p = post(c, a, "/prepare", command())
    grants.remove(("default", "alice", "demo"))
    done=post(c,a,f"/{p['attempt_id']}/execute",receipt(p))
    assert done["status"]=="REJECTED_NO_EFFECT"
    assert c.get(ROOT+"?agent_id=demo",headers=a).status_code==403
    post(c,a,f"/{p['attempt_id']}/acknowledge",receipt(done))
    grants.add(("default","alice","demo"))
    cmd = command()
    p = post(c, a, "/prepare", cmd)

    def failure(*args, **kwargs):
        raise RuntimeError("synthetic audit fault")
    with monkeypatch.context() as patch:
        patch.setattr(db,"Audit",failure)
        with pytest.raises(RuntimeError,match="synthetic audit fault"):
            post(c,a,f"/{p['attempt_id']}/execute",receipt(p))
    assert c.get(ROOT+"/ledger",headers=a).json()["active"]["status"]=="PREPARED"
    assert not any(r["version"]==cmd["version"] for r in c.get(ROOT+"?agent_id=demo",headers=a).json()["items"])
    done=post(c,a,f"/{p['attempt_id']}/execute",receipt(p))
    post(c,a,f"/{p['attempt_id']}/acknowledge",receipt(done))


def test_parallel_prepare_exact_mapping_and_invalid_catalog(lab,idp,monkeypatch):
    c, _, app = lab
    a, _ = identities(lab, idp, monkeypatch)
    for mutate in [
        lambda v: v.update(tenant="other"),
        lambda v: v["proposal"].update(case_ids=["success", "success"]),
        lambda v: v["proposal"].update(expected_text="pass"),
        lambda v: v["proposal"].update(schema_version=True),
    ]:
        v = command()
        mutate(v)
        assert c.post(ROOT + "/prepare", headers=a, json=v).status_code == 422
    assert c.get(ROOT+"?agent_id=demo",headers={"X-Console-User":"alice","X-Console-Role":"PLATFORM_ADMIN"}).status_code==401
    foreign=bearer(idp.token({"sub":"alice","tenant":"foreign"}))
    assert c.get(ROOT+"?agent_id=demo",headers=foreign).status_code==403
    with ThreadPoolExecutor(2) as pool:
        rows=list(pool.map(lambda _:c.post(ROOT+"/prepare",headers=a,json=command()),range(2)))
    assert sorted(r.status_code for r in rows)==[200,409]
    p=next(r.json() for r in rows if r.status_code==200)
    done = post(c, a, f"/{p['attempt_id']}/execute", receipt(p))
    post(c, a, f"/{p['attempt_id']}/acknowledge", receipt(done))
    monkeypatch.setitem(app.state._state,"isolated_datasets",None)
    assert c.get(ROOT+"?agent_id=demo",headers=a).status_code==503


def test_binding_conflicts_concurrent_execute_and_evidence_tamper(lab, idp, monkeypatch):
    c = lab[0]
    a, _ = identities(lab, idp, monkeypatch)
    cmd = command()
    prepared = post(c, a, "/prepare", cmd)
    assert post(c, a, "/prepare", cmd) == prepared
    changed = cmd | {"version": "different"}
    assert c.post(ROOT + "/prepare", headers=a, json=changed).status_code == 409
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: post(c, a, f"/{prepared['attempt_id']}/execute", receipt(prepared)), range(2)))
    assert results[0] == results[1]
    done = results[0]
    rows = c.get(ROOT + "?agent_id=demo", headers=a).json()["items"]
    assert len([r for r in rows if r["version"] == cmd["version"]]) == 1

    async def corrupt():
        async with db.SessionLocal() as session:
            op = await session.get(api.Operation, prepared["attempt_id"])
            original = op.evidence
            op.evidence = original | {"subject": "human:another"}
            await session.commit()
            return original

    original = c.portal.call(corrupt)
    assert c.post(ROOT + f"/{done['attempt_id']}/acknowledge", headers=a, json=receipt(done)).status_code == 409
    assert c.get(ROOT + "/ledger", headers=a).json()["active"] is not None

    async def restore():
        async with db.SessionLocal() as session:
            op = await session.get(api.Operation, prepared["attempt_id"])
            op.evidence = original
            await session.commit()

    c.portal.call(restore)
    post(c, a, f"/{done['attempt_id']}/acknowledge", receipt(done))


def test_stale_revision_mapping_loss_and_wrong_agent(lab, idp, monkeypatch):
    c, _, app = lab
    a, _ = identities(lab, idp, monkeypatch)
    row = finish(c, a, command())
    updated = finish(c, a, command("edit", row))
    assert c.post(ROOT + "/prepare", headers=a, json=command("freeze", row)).status_code == 409
    assert c.get(ROOT + "?agent_id=not-assigned", headers=a).status_code == 403
    pending = post(c, a, "/prepare", command("freeze", updated))
    monkeypatch.setitem(app.state._state, "isolated_dataset_bindings", {})
    done = post(c, a, f"/{pending['attempt_id']}/execute", receipt(pending))
    assert done["status"] == "REJECTED_NO_EFFECT"
    assert c.get(ROOT + "?agent_id=demo", headers=a).status_code == 403
    # Receipt recovery remains scoped to the original signed subject; no dataset body.
    recovered = c.get(ROOT + "/ledger", headers=a).json()["active"]
    assert recovered["evidence"] is None and "proposal" not in recovered["command"]
    post(c, a, f"/{done['attempt_id']}/acknowledge", receipt(done))
