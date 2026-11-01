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
