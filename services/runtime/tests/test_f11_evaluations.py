"""Signed test identities, disposable SQLite and the real Runtime loop with substitutes."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import test_f11_datasets as ds
from sqlalchemy import select

from runtime.api import isolated_evaluations as api
from runtime.state import db

ROOT = "/v1/isolated-evaluations"


@pytest.fixture
def idp(monkeypatch):
    yield from ds.signed_provider.__wrapped__(monkeypatch)


@pytest.fixture
def lab(client, monkeypatch):
    value = ds.lab.__wrapped__(client, monkeypatch)
    app = value[2]
    if not any(getattr(r, "path", "") == ROOT + "/prepare" for r in app.routes):
        app.include_router(api.router)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    import f11_evaluation_fixture as profile
    restore = profile.install(app)
    from dataclasses import replace

    from runtime.loop import toolbroker
    monkeypatch.setattr(toolbroker, "settings", replace(toolbroker.settings, tool_result_offload_tokens=10000))
    async def setup():
        async with db.engine.begin() as conn:
            await conn.run_sync(ds.api.Base.metadata.create_all)
    client.portal.call(setup)
    yield value
    restore()


def setup(lab, idp, monkeypatch, scenario="ticket-summary-readonly"):
    c, _, app = lab
    _, b = ds.identities(lab, idp, monkeypatch)
    actor = "alice-" + uuid.uuid4().hex
    app.state.isolated_dataset_bindings[(ds.auth.settings.jwt_issuer, "default", "human:" + actor)] = "alice"
    a = ds.bearer(idp.token({"sub": actor, "tenant": "default"}))
    agent_id = "f11-eval-" + uuid.uuid4().hex
    lab[1].add(("default", "alice", agent_id))
    cmd = ds.command()
    cmd["agent_id"] = agent_id
    cmd["proposal"]["scenario_id"] = scenario
    cmd["proposal"]["case_ids"] = sorted(api.rules.CASES[scenario])
    def save(command):
        command["agent_id"] = agent_id
        prepared = ds.post(c, a, "/prepare", command)
        done = ds.post(c, a, f"/{prepared['attempt_id']}/execute", ds.receipt(prepared))
        assert done["status"] == "COMMITTED", done
        ds.post(c, a, f"/{prepared['attempt_id']}/acknowledge", ds.receipt(done))
        return next(row for row in c.get(ds.ROOT + "?agent_id=" + agent_id, headers=a).json()["items"]
                    if row["version"] == command["version"])
    dataset = save(cmd)
    dataset = save(ds.command("freeze", dataset))
    version = uuid.uuid4().hex
    async def seed():
        async with db.SessionLocal() as session:
            session.add(db.AgentVersion(agent_id=agent_id, tenant_id="default", version=version, status="published",
                                        harness="mock", model_alias="default-chat", system_prompt="synthetic",
                                        config={"tools": [api.rules.TOOLS[scenario]]}, rollout_percent=100))
            await session.commit()
    c.portal.call(seed)
    app.state.isolated_evaluation_grants = {("default", "human:" + actor, agent_id, version)}
    command = {"attempt_id": str(uuid.uuid4()), "dataset_id": dataset["id"], "agent_id": agent_id, "agent_version": version}
    return c, a, b, command


def post(c, a, path, body):
    r = c.post(ROOT + path, headers=a, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def active(c, a):
    return c.get(ROOT + "/ledger", headers=a).json()["active"]


@pytest.mark.parametrize("scenario", sorted(api.rules.CASES))
def test_real_loop_terminal_receipt_and_ack(lab, idp, monkeypatch, scenario):
    c, a, b, cmd = setup(lab, idp, monkeypatch, scenario)
    p = post(c, a, "/prepare", cmd)
    assert c.post("/v1/workbench/create-attempts", headers=a,
                  json={"attempt_id": str(uuid.uuid4()), "agent_id": "demo", "text": "blocked"}).status_code == 409
    assert c.post(ROOT + f"/{p['attempt_id']}/execute", headers=b, json=ds.receipt(p)).status_code == 404
    done = post(c, a, f"/{p['attempt_id']}/execute", ds.receipt(p))
    assert done["status"] == "COMMITTED", done
    assert done["receipt"]["result"]["pass_rate"] == 1, done["children"]
    assert len(done["children"]) == len(api.rules.CASES[scenario])
    assert done["publish_eligible"] is False
    assert "payload" not in str(done) and "模拟工单摘要" not in str(done)
    assert post(c, a, f"/{p['attempt_id']}/execute", ds.receipt(p)) == done
    assert active(c, a) == done
    assert c.post(ROOT + f"/{p['attempt_id']}/acknowledge", headers=a, json=ds.receipt(p)).status_code == 409
    post(c, a, f"/{p['attempt_id']}/acknowledge", ds.receipt(done))
    assert active(c, a) is None


@pytest.mark.parametrize("change", ["execution", "management", "snapshot"])
def test_admission_recheck_no_effect(lab, idp, monkeypatch, change):
    c, a, _, cmd = setup(lab, idp, monkeypatch)
    p = post(c, a, "/prepare", cmd)
    if change == "execution":
        lab[2].state.isolated_evaluation_grants.clear()
    elif change == "management":
        lab[1].remove(("default", "alice", cmd["agent_id"]))
    else:
        async def mutate():
            async with db.SessionLocal() as session:
                row = await session.scalar(select(db.AgentVersion).where(db.AgentVersion.version == cmd["agent_version"]))
                row.system_prompt = "changed"
                await session.commit()
        c.portal.call(mutate)
    done = post(c, a, f"/{p['attempt_id']}/execute", ds.receipt(p))
    assert done["status"] == "REJECTED_NO_EFFECT" and done["children"] == []
    post(c, a, f"/{p['attempt_id']}/acknowledge", ds.receipt(done))


def test_partial_crash_no_resume_or_unlock(lab, idp, monkeypatch):
    c, a, _, cmd = setup(lab, idp, monkeypatch)
    profile = lab[2].state.isolated_evaluation_profile
    original = profile.execute
    calls = []
    async def crash(run_id, *args):
        calls.append(run_id)
        if len(calls) == 2:
            raise RuntimeError("synthetic second child crash")
        await original(run_id, *args)
    monkeypatch.setattr(profile, "execute", crash)
    p = post(c, a, "/prepare", cmd)
    with pytest.raises(RuntimeError, match="synthetic second child crash"):
        post(c, a, f"/{p['attempt_id']}/execute", ds.receipt(p))
    saved = active(c, a)
    assert saved["status"] == "EXECUTING" and saved["receipt"] is None
    assert sum(child["evidence"] is not None for child in saved["children"]) == 1
    assert post(c, a, f"/{p['attempt_id']}/execute", ds.receipt(saved)) == saved
    assert len(calls) == 2
    assert c.post(ROOT + f"/{p['attempt_id']}/acknowledge", headers=a, json=ds.receipt(saved)).status_code == 409
    # Each test has a distinct signed actor; leave unresolved state locked until temporary DB cleanup.


def test_parallel_execute_exactly_once(lab, idp, monkeypatch):
    c, a, _, cmd = setup(lab, idp, monkeypatch)
    p = post(c, a, "/prepare", cmd)
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda _: post(c, a, f"/{p['attempt_id']}/execute", ds.receipt(p)), range(2)))
    assert all(r["status"] in ("EXECUTING", "COMMITTED") for r in replies)
    done = active(c, a)
    assert done["status"] == "COMMITTED"
    assert len(done["children"]) == 2
    assert len({r["run_id"] for r in done["children"]}) == 2
    post(c, a, f"/{p['attempt_id']}/acknowledge", ds.receipt(done))


@pytest.mark.parametrize("change", ["missing_grant", "missing_management", "foreign", "service", "dev_header", "extra_field"])
def test_prepare_denies_missing_authority(lab, idp, monkeypatch, change):
    c, a, _, cmd = setup(lab, idp, monkeypatch)
    if change == "missing_grant":
        lab[2].state.isolated_evaluation_grants.clear()
    elif change == "missing_management":
        lab[1].remove(("default", "alice", cmd["agent_id"]))
    elif change == "foreign":
        a = ds.bearer(idp.token({"sub": "alice", "tenant": "foreign"}))
    elif change == "service":
        a = ds.bearer(idp.token({"sub": "service", "principal_type": "service"}))
    elif change == "dev_header":
        a = {"X-Console-User": "alice", "X-Console-Role": "PLATFORM_ADMIN"}
    else:
        cmd["ignore_lock"] = True
    assert c.post(ROOT + "/prepare", headers=a, json=cmd).status_code in (401, 403, 422)


def test_result_commit_failure_preserves_unresolved_children(lab, idp, monkeypatch):
    c, a, _, cmd = setup(lab, idp, monkeypatch)
    p = post(c, a, "/prepare", cmd)
    original = api.audit
    def fail(session, principal, row, action):
        if action == "evaluation.complete":
            raise RuntimeError("synthetic result commit failure")
        original(session, principal, row, action)
    monkeypatch.setattr(api, "audit", fail)
    with pytest.raises(RuntimeError, match="synthetic result commit failure"):
        post(c, a, f"/{p['attempt_id']}/execute", ds.receipt(p))
    saved = active(c, a)
    assert saved["status"] == "EXECUTING" and saved["receipt"] is None
    assert all(c["evidence"] for c in saved["children"])
    assert post(c, a, f"/{p['attempt_id']}/execute", ds.receipt(saved)) == saved
    assert c.post(ROOT + f"/{p['attempt_id']}/acknowledge", headers=a, json=ds.receipt(saved)).status_code == 409


@pytest.mark.parametrize("change", ["receipt", "child", "events"])
def test_ack_requires_unchanged_terminal_evidence(lab, idp, monkeypatch, change):
    c, a, b, cmd = setup(lab, idp, monkeypatch)
    p = post(c, a, "/prepare", cmd)
    done = post(c, a, f"/{p['attempt_id']}/execute", ds.receipt(p))
    assert c.post(ROOT + f"/{p['attempt_id']}/acknowledge", headers=b, json=ds.receipt(done)).status_code == 404
    async def mutate():
        async with db.SessionLocal() as session:
            batch = await session.get(api.Batch, p["attempt_id"])
            child = await session.scalar(select(api.Child).where(api.Child.batch_id == p["attempt_id"]))
            if change == "receipt":
                batch.receipt = dict(batch.receipt, fingerprint="0" * 64)
            elif change == "child":
                child.evidence = None
            else:
                run = await session.get(db.Run, child.run_id)
                run.last_seq += 1
            await session.commit()
    c.portal.call(mutate)
    assert c.post(ROOT + f"/{p['attempt_id']}/acknowledge", headers=a, json=ds.receipt(done)).status_code == 409
    assert active(c, a)["acknowledged"] is False


def test_fixture_does_not_change_shared_demo_versions(lab, idp, monkeypatch):
    async def versions():
        async with db.SessionLocal() as session:
            return list(await session.scalars(select(db.AgentVersion.id).where(db.AgentVersion.agent_id == "demo")))
    before = lab[0].portal.call(versions)
    setup(lab, idp, monkeypatch)
    assert lab[0].portal.call(versions) == before



def test_history_after_ack_permissions_pagination_and_comparison(lab, idp, monkeypatch):
    c, a, b, cmd = setup(lab, idp, monkeypatch)
    records = []
    for _ in range(3):
        cmd["attempt_id"] = str(uuid.uuid4())
        prepared = post(c, a, "/prepare", cmd)
        done = post(c, a, f"/{prepared['attempt_id']}/execute", ds.receipt(prepared))
        post(c, a, f"/{prepared['attempt_id']}/acknowledge", ds.receipt(done))
        records.append(prepared["attempt_id"])
    params = {"agent_id": cmd["agent_id"], "limit": 2}
    first = c.get(ROOT + "/history", headers=a, params=params)
    assert first.status_code == 200, first.text
    data = first.json()
    assert [v["attempt_id"] for v in data["items"]] == list(reversed(records[1:]))
    assert all(v["acknowledged"] for v in data["items"])
    assert data["snapshot_guaranteed"] is False
    second = c.get(ROOT + "/history", headers=a, params=params | {"before": data["next_cursor"]}).json()
    assert [v["attempt_id"] for v in second["items"]] == records[:1]
    assert second["next_cursor"] is None
    detail = c.get(ROOT + "/history/" + records[0], headers=a, params=params)
    assert detail.status_code == 200 and detail.json()["acknowledged"]
    assert "模拟工单摘要" not in detail.text and "system_prompt" not in detail.text
    pair = {"agent_id": cmd["agent_id"], "left": records[0], "right": records[1]}
    result = c.get(ROOT + "/history/compare", headers=a, params=pair)
    assert result.status_code == 200, result.text
    assert result.json()["comparison"]["pass_rate_delta"] == 0
    assert result.json()["comparison"]["publish_eligible"] is False
    assert c.get(ROOT + "/history", headers=a, params=params | {"status": "EXECUTING"}).json()["items"] == []
    for bad in ({"limit": 0}, {"limit": 51}, {"before": "invalid"}, {"status": "unknown"}):
        assert c.get(ROOT + "/history", headers=a, params=params | bad).status_code == 422
    # Management alone cannot read another actor's records, even with the same Agent assignment.
    lab[1].add(("default", "bob", cmd["agent_id"]))
    assert c.get(ROOT + "/history/" + records[0], headers=b, params=params).status_code == 404
    assert c.get(ROOT + "/history", headers=b, params=params).json()["items"] == []
    assert c.get(ROOT + "/history", headers=b, params=params | {"before": records[0]}).status_code == 404
    assert c.get(ROOT + "/history/compare", headers=b, params=pair).status_code in (403, 404)
    # Revocation affects all history paths, but neither releases nor mutates a subject lock.
    lab[1].remove(("default", "alice", cmd["agent_id"]))
    for path, query in [("/history", params), ("/history/" + records[0], params), ("/history/compare", pair)]:
        assert c.get(ROOT + path, headers=a, params=query).status_code == 403
    assert active(c, a) is None


def test_revoked_history_keeps_terminal_recovery_and_ack(lab, idp, monkeypatch):
    c, a, _, cmd = setup(lab, idp, monkeypatch)
    p = post(c, a, "/prepare", cmd)
    done = post(c, a, f"/{p['attempt_id']}/execute", ds.receipt(p))
    lab[1].remove(("default", "alice", cmd["agent_id"]))
    assert c.get(ROOT + "/history", headers=a, params={"agent_id": cmd["agent_id"]}).status_code == 403
    assert active(c, a) == done
    post(c, a, f"/{p['attempt_id']}/acknowledge", ds.receipt(done))
    assert active(c, a) is None


@pytest.mark.parametrize("identity", ["missing", "service", "foreign", "delegated"])
def test_history_requires_direct_same_scope_user(lab, idp, monkeypatch, identity):
    c, a, _, cmd = setup(lab, idp, monkeypatch)
    p = post(c, a, "/prepare", cmd)
    if identity == "missing":
        headers = {"X-User-Id": "alice", "X-Role": "platform-admin"}
    else:
        claims = {"sub": "alice", "tenant": "default"}
        if identity == "service":
            claims["client_id"] = "isolated-service"
        elif identity == "foreign":
            claims["tenant"] = "foreign"
        else:
            claims["act"] = {"sub": "gateway"}
        headers = ds.bearer(idp.token(claims))
    assert c.get(ROOT + "/history", headers=headers, params={"agent_id": cmd["agent_id"]}).status_code in (401, 403)
    assert c.get(ROOT + "/history/" + p["attempt_id"], headers=headers,
                 params={"agent_id": cmd["agent_id"]}).status_code in (401, 403, 404)



def test_history_rechecks_management_before_returning(lab, idp, monkeypatch):
    from fastapi import HTTPException

    c, a, _, cmd = setup(lab, idp, monkeypatch)
    p = post(c, a, "/prepare", cmd)
    original = lab[2].state.isolated_dataset_authorize
    calls = []
    async def revoked_during_read(*args):
        calls.append(args)
        if len(calls) == 2:
            raise HTTPException(403, "revoked during read")
        await original(*args)
    monkeypatch.setattr(lab[2].state, "isolated_dataset_authorize", revoked_during_read)
    response = c.get(ROOT + "/history/" + p["attempt_id"], headers=a, params={"agent_id": cmd["agent_id"]})
    assert response.status_code == 403
    assert len(calls) == 2
    assert active(c, a)["status"] == "PREPARED"



def test_history_distinct_versions_tied_cursor_and_incomplete_comparison(lab, idp, monkeypatch):
    c, a, _, cmd = setup(lab, idp, monkeypatch)
    first = post(c, a, "/prepare", cmd)
    done = post(c, a, f"/{first['attempt_id']}/execute", ds.receipt(first))
    post(c, a, f"/{first['attempt_id']}/acknowledge", ds.receipt(done))
    new_version = uuid.uuid4().hex
    async def seed_version():
        async with db.SessionLocal() as session:
            old = await session.scalar(select(db.AgentVersion).where(
                db.AgentVersion.agent_id == cmd["agent_id"], db.AgentVersion.version == cmd["agent_version"]))
            session.add(db.AgentVersion(agent_id=old.agent_id, tenant_id=old.tenant_id,
                                        version=new_version, status="published", harness="mock",
                                        model_alias=old.model_alias, system_prompt="another synthetic version",
                                        config=old.config, rollout_percent=100))
            await session.commit()
    c.portal.call(seed_version)
    grant = next(iter(lab[2].state.isolated_evaluation_grants))
    lab[2].state.isolated_evaluation_grants.add((*grant[:3], new_version))
    second = post(c, a, "/prepare", cmd | {"attempt_id": str(uuid.uuid4()), "agent_version": new_version})
    params = {"agent_id": cmd["agent_id"], "left": first["attempt_id"], "right": second["attempt_id"]}
    incomplete = c.get(ROOT + "/history/compare", headers=a, params=params).json()
    assert incomplete["comparison"]["reasons"] == ["INCOMPLETE_RESULT"]
    assert incomplete["comparison"]["pass_rate_delta"] is None
    assert active(c, a)["status"] == "PREPARED"  # GET never executes or ACKs.
    done = post(c, a, f"/{second['attempt_id']}/execute", ds.receipt(second))
    post(c, a, f"/{second['attempt_id']}/acknowledge", ds.receipt(done))
    result = c.get(ROOT + "/history/compare", headers=a, params=params).json()
    assert result["left"]["binding"]["agent_snapshot"] != result["right"]["binding"]["agent_snapshot"]
    assert result["comparison"]["comparable"] is True
    assert result["comparison"]["pass_rate_delta"] == 0
    async def tie_dates():
        async with db.SessionLocal() as session:
            left = await session.get(db.WorkbenchAttempt, first["attempt_id"])
            right = await session.get(db.WorkbenchAttempt, second["attempt_id"])
            right.created_at = left.created_at
            await session.commit()
    c.portal.call(tie_dates)
    query = {"agent_id": cmd["agent_id"], "limit": 1}
    page1 = c.get(ROOT + "/history", headers=a, params=query).json()
    page2 = c.get(ROOT + "/history", headers=a, params=query | {"before": page1["next_cursor"]}).json()
    assert [page1["items"][0]["attempt_id"], page2["items"][0]["attempt_id"]] == sorted(
        [first["attempt_id"], second["attempt_id"]], reverse=True)
    assert page2["next_cursor"] is None
    assert active(c, a) is None
