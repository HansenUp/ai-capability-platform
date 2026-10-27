"""S9 WP-C：Skill 验签与原子物化、吊销、skills_lock 组装、渐进披露与平台工具。"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_s9c_helpers import SKILL_MD, FakeGateway, Signer, build, default_files, tgz, write_jwks

from runtime import gateway_client
from runtime.skills import store, tools
from runtime.skills.verify import SKILL_REVOKED, SKILL_UNAVAILABLE, SKILL_VERIFY_FAILED, SkillVerifyError, verify_dir, verify_package


@pytest.fixture
def env(tmp_path, monkeypatch):
    signer = Signer()
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("SKILL_TRUST_KEYS_FILE", str(write_jwks(tmp_path / "trust.json", signer)))
    gw = FakeGateway()
    gateway_client.set_transport_for_tests(gw.transport())
    store.revocations.reset()
    yield SimpleNamespace(signer=signer, gw=gw, tmp=tmp_path)
    gateway_client.set_transport_for_tests(None)
    store.revocations.reset()


def _publish(env, files=None, **kw):
    pkg, sha = build(env.signer, files, **kw)
    env.gw.packages[(kw.get("name", "demo-skill"), kw.get("version", "1.0.0"))] = tgz(pkg)
    return pkg, sha


def _lock(sha, name="demo-skill", version="1.0.0"):
    return {"name": name, "version": version, "manifest_sha256": sha}


# ---- 验签 ----
def test_verify_package_ok_and_hash_tamper(env):
    pkg, sha = build(env.signer)
    manifest, files = verify_package(tgz(pkg), _lock(sha))
    assert manifest.sha256 == sha and manifest.sandbox == "read-only" and files["scripts/summarize.py"] == pkg["scripts/summarize.py"]
    bad = dict(pkg) | {"references/format.md": b"tampered"}
    with pytest.raises(SkillVerifyError, match="hash mismatch") as ei:
        verify_package(tgz(bad), _lock(sha))
    assert ei.value.code == SKILL_VERIFY_FAILED


def test_verify_rejects_untrusted_key_and_lock_mismatch(env):
    other = Signer()  # 换钥：签名密钥不在信任公钥中
    pkg, sha = build(other)
    with pytest.raises(SkillVerifyError, match="not trusted"):
        verify_package(tgz(pkg), _lock(sha))
    # 用信任密钥的 kid、但签名来自另一把密钥（伪造 kid）
    pkg2, sha2 = build(env.signer)
    forged = other.sign(pkg2["manifest.json"]).replace(other.kid.encode(), env.signer.kid.encode())
    with pytest.raises(SkillVerifyError, match="signature verification failed"):
        verify_package(tgz(pkg2 | {"manifest.sig": forged}), _lock(sha2))
    with pytest.raises(SkillVerifyError, match="does not match skills_lock"):
        verify_package(tgz(pkg2), _lock("0" * 64))
    with pytest.raises(SkillVerifyError, match="lock expects"):
        verify_package(tgz(pkg2), _lock(sha2, version="2.0.0"))


def test_verify_rejects_non_canonical_manifest_and_extra_files(env):
    pkg, sha = build(env.signer)
    pretty = b" " + pkg["manifest.json"]
    with pytest.raises(SkillVerifyError, match="canonical"):
        verify_package(tgz(pkg | {"manifest.json": pretty, "manifest.sig": env.signer.sign(pretty)}), None)
    with pytest.raises(SkillVerifyError, match="not in manifest"):
        verify_package(tgz(pkg | {"scripts/extra.py": b"print(1)"}), _lock(sha))


@pytest.mark.parametrize("bad_path", ["../evil.md", "/etc/passwd", "a/../../b", "a\\b", "./x/../y"])
def test_verify_rejects_path_traversal_in_manifest(env, bad_path):
    files = default_files() | {bad_path: b"x"}
    pkg, sha = build(env.signer, files)
    with pytest.raises(SkillVerifyError) as ei:
        verify_package(tgz(pkg), _lock(sha))
    assert ei.value.code == SKILL_VERIFY_FAILED


def test_verify_package_rejects_symlink_and_hardlink_members(env):
    pkg, sha = build(env.signer)

    def add_symlink(tar):
        ti = tarfile.TarInfo("references/link.md")
        ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(ti)

    def add_hardlink(tar):
        ti = tarfile.TarInfo("references/hard.md")
        ti.type, ti.linkname = tarfile.LNKTYPE, "SKILL.md"
        tar.addfile(ti)

    def add_traversal(tar):
        ti = tarfile.TarInfo("../outside.md")
        ti.size = 1
        tar.addfile(ti, io.BytesIO(b"x"))

    for extra in (add_symlink, add_hardlink, add_traversal):
        with pytest.raises(SkillVerifyError):
            verify_package(tgz(pkg, extra=extra), _lock(sha))


def test_verify_dir_rejects_symlink_and_tamper(env):
    pkg, sha = build(env.signer)
    root = env.tmp / "d"
    for p, d in pkg.items():
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_bytes(d)
    assert verify_dir(root, _lock(sha)).name == "demo-skill"
    os.symlink("/etc/passwd", root / "references" / "evil.md")
    with pytest.raises(SkillVerifyError, match="symlink"):
        verify_dir(root, _lock(sha))
    (root / "references" / "evil.md").unlink()
    (root / "SKILL.md").write_bytes(b"changed")
    with pytest.raises(SkillVerifyError, match="hash mismatch"):
        verify_dir(root, _lock(sha))


# ---- 物化 ----
async def test_materialize_lazy_atomic_and_reuse(env):
    pkg, sha = _publish(env)
    path = await store.materialize("demo-skill", "1.0.0", sha)
    assert path == store.base_dir() / "demo-skill" / "1.0.0"
    assert (path / "scripts" / "summarize.py").read_bytes() == pkg["scripts/summarize.py"]  # 落盘后 python 读回与包内一致
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".")]  # 临时目录已清理
    await store.materialize("demo-skill", "1.0.0", sha)
    assert env.gw.downloads() == 1  # 已物化且校验通过：不再下载
    (path / "SKILL.md").write_bytes(b"tampered on disk")
    await store.materialize("demo-skill", "1.0.0", sha)  # 本地被篡改：重新下载并整体替换
    assert env.gw.downloads() == 2 and (path / "SKILL.md").read_bytes() == pkg["SKILL.md"]


async def test_materialize_tampered_package_leaves_nothing(env):
    pkg, sha = build(env.signer)
    env.gw.packages[("demo-skill", "1.0.0")] = tgz(pkg | {"SKILL.md": b"evil"})
    with pytest.raises(SkillVerifyError) as ei:
        await store.materialize("demo-skill", "1.0.0", sha)
    assert ei.value.code == SKILL_VERIFY_FAILED
    assert not (env.tmp / "skills" / "demo-skill" / "1.0.0").exists()
    with pytest.raises(SkillVerifyError) as ei:
        await store.materialize("demo-skill", "9.9.9", sha)  # 网关无此包
    assert ei.value.code == SKILL_UNAVAILABLE
    with pytest.raises(SkillVerifyError):
        await store.materialize("../evil", "1.0.0", sha)


async def test_concurrent_materialize_downloads_once(env):
    import asyncio

    _, sha = _publish(env)
    await asyncio.gather(*(store.materialize("demo-skill", "1.0.0", sha) for _ in range(4)))
    assert env.gw.downloads() == 1


# ---- 吊销 ----
async def test_revocation_blocks_and_fails_closed(env, monkeypatch):
    _, sha = _publish(env)
    env.gw.revoked = [{"name": "demo-skill", "version": "1.0.0", "manifest_sha256": sha, "revoked_at": "2026-10-18T01:00:00Z"}]
    with pytest.raises(SkillVerifyError) as ei:
        await store.materialize("demo-skill", "1.0.0", sha)
    assert ei.value.code == SKILL_REVOKED
    # 吊销列表从未拉取成功：失败关闭
    store.revocations.reset()
    env.gw.revocations_status = 500
    with pytest.raises(SkillVerifyError, match="revocation list unavailable"):
        await store.materialize("demo-skill", "1.0.0", sha)
    # 拉取成功过、刷新失败但未超过最大沿用时长：沿用旧列表
    env.gw.revocations_status, env.gw.revoked = 200, []
    await store.revocations.refresh()
    monkeypatch.setenv("SKILL_REVOCATION_TTL_S", "0")
    env.gw.revocations_status = 500
    assert (await store.materialize("demo-skill", "1.0.0", sha)).exists()


# ---- 组装（skills_lock / 渐进披露）----
def _agent(lock, **cfg):
    from runtime.state.db import AgentVersion

    av = AgentVersion(
        agent_id="s9c", version="1", tenant_id="default", system_prompt="你是助手。", model_alias="m", config={"tools": ["echo"]} | cfg
    )
    av.skills_lock = lock
    return av


async def test_assemble_with_lock_progressive(env):
    from runtime.assemble.assembler import assemble

    _, sha = _publish(env)
    a = await assemble(_agent([_lock(sha)], skills_mode="progressive"), "human:alice")
    assert [s.name for s in a.skills] == ["demo-skill"] and a.skills[0].capabilities_declared == ["cap.ticket.search"]
    assert "演示技能：总结工单要点" in a.system_prompt and "BODY-MARKER" not in a.system_prompt  # 只注入名称与描述
    names = [t.name for t in a.tools]
    assert {"load_skill", "read_skill_file", "run_skill_script"} <= set(names)
    assert a.report["skills_lock"] == [_lock(sha)] and a.report["skills_mode"] == "progressive"
    # 非渐进：不注入 load_skill / read_skill_file，脚本工具仍按清单注入
    b = await assemble(_agent([_lock(sha)]), "human:alice")
    assert "load_skill" not in [t.name for t in b.tools] and "run_skill_script" in [t.name for t in b.tools]


async def test_assemble_lock_failures(env):
    from runtime.assemble.assembler import assemble

    _, sha = _publish(env, tenant="other-tenant")
    with pytest.raises(SkillVerifyError, match="belongs to tenant"):
        await assemble(_agent([_lock(sha)]), "human:alice")
    with pytest.raises(SkillVerifyError) as ei:
        await assemble(_agent([_lock("f" * 64)]), "human:alice")
    assert ei.value.code == SKILL_VERIFY_FAILED
    with pytest.raises(SkillVerifyError, match="malformed"):
        await assemble(_agent("not-a-list"), "human:alice")


async def test_assemble_legacy_without_lock_unchanged(env):
    from runtime.assemble.assembler import assemble

    a = await assemble(_agent(None, skills=[{"name": "ticket-summary"}]), "human:alice")
    assert "skills_lock" not in a.report and not {"load_skill", "run_skill_script"} & {t.name for t in a.tools}
    assert env.gw.calls == [c for c in env.gw.calls if "/skills" not in c]  # 旧快照不访问 Skill 接口


# ---- 平台工具 ----
async def _rc(env, **kw):
    _, sha = _publish(env, **kw)
    skills = await store.load_locked([_lock(sha, version=kw.get("version", "1.0.0"))], "default")
    return SimpleNamespace(skills=skills, workspace=str(env.tmp / "ws"))


def _call(tool, **inp):
    return SimpleNamespace(tool_name=tool, input=inp)


async def test_tools_load_and_read(env):
    rc = await _rc(env)
    assert await tools.handle(rc, _call("echo")) is None
    r = await tools.handle(rc, _call("load_skill", name="demo-skill"))
    assert r.status == "ok" and "BODY-MARKER" in r.output["content"] and r.event["manifest_sha256"]
    r = await tools.handle(rc, _call("read_skill_file", name="demo-skill", path="references/format.md"))
    assert r.status == "ok" and "根因" in r.output["content"]
    for bad in ("../../etc/passwd", "/etc/passwd", "references/missing.md", "manifest.json"):
        r = await tools.handle(rc, _call("read_skill_file", name="demo-skill", path=bad))
        assert r.status == "error", bad
    assert (await tools.handle(rc, _call("load_skill", name="nope"))).status == "error"
    # 读取时逐字节核哈希：磁盘被篡改即拒绝
    (rc.skills[0].path / "references" / "format.md").write_bytes(b"tampered")
    r = await tools.handle(rc, _call("read_skill_file", name="demo-skill", path="references/format.md"))
    assert r.status == "error" and r.output["code"] == SKILL_VERIFY_FAILED


async def test_run_skill_script_level_from_manifest_only(env, monkeypatch):
    from runtime import sandbox

    rc = await _rc(env)
    seen = {}

    async def fake_run(script, level, stdin, workspace, *, files, expected_sha256):
        seen.update(script=script, level=level, stdin=stdin, files=sorted(files), sha=expected_sha256)
        return sandbox.SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False)

    monkeypatch.setattr(sandbox, "run", fake_run)
    r = await tools.handle(rc, _call("run_skill_script", name="demo-skill", script="scripts/summarize.py", stdin="hi", level="full-access"))
    assert r.status == "ok" and seen["level"] == "read-only" and r.event["sandbox_level"] == "read-only"  # 入参级别被忽略
    assert "SKILL.md" in seen["files"] and seen["sha"]
    r = await tools.handle(rc, _call("run_skill_script", name="demo-skill", script="SKILL.md"))
    assert r.status == "error" and r.output["code"] == sandbox.SANDBOX_DENIED


async def test_run_skill_script_denials(env, monkeypatch):
    rc = await _rc(env, sandbox="none", version="2.0.0")
    r = await tools.handle(rc, _call("run_skill_script", name="demo-skill", script="scripts/summarize.py"))
    assert r.status == "error" and r.output["code"] == "SANDBOX_DENIED"
    monkeypatch.setenv("SANDBOX_BACKEND", "disabled")
    rc = await _rc(env, version="3.0.0")
    r = await tools.handle(rc, _call("run_skill_script", name="demo-skill", script="scripts/summarize.py"))
    assert r.status == "error" and r.output["code"] == "SANDBOX_UNAVAILABLE"
    # 旧式开发技能（未签名）：不能执行脚本
    from ai_contract import SkillDir

    legacy = SimpleNamespace(skills=[SkillDir(name="ticket-summary", version="0", path=Path("skills/ticket-summary"))], workspace=".")
    r = await tools.handle(legacy, _call("run_skill_script", name="ticket-summary", script="scripts/summarize.py"))
    assert r.status == "error" and "unsigned" in r.output["error"]


def test_frontmatter():
    assert tools.frontmatter(SKILL_MD) == {"name": "demo-skill", "description": "演示技能：总结工单要点（只读）。"}
    assert tools.frontmatter("no header") == {}


@pytest.mark.parametrize(
    "tool,extra",
    [("load_skill", {}), ("read_skill_file", {"path": "references/format.md"}), ("run_skill_script", {"script": "scripts/summarize.py"})],
)
async def test_tools_reject_skill_revoked_after_assembly(env, tool, extra):
    rc = await _rc(env)
    store.revocations.apply({"revoked": [{"name": "demo-skill", "version": "1.0.0"}]})
    result = await tools.handle(rc, _call(tool, name="demo-skill", **extra))
    assert result.status == "error" and result.output["code"] == SKILL_REVOKED


@pytest.mark.parametrize("level", ["workspace-write", "full-access"])
async def test_scripts_with_untracked_side_effects_fail_closed(env, monkeypatch, level):
    from runtime import sandbox

    rc = await _rc(env, sandbox=level)

    async def unexpected_run(*args, **kwargs):
        pytest.fail("side-effecting script must not reach the sandbox")

    monkeypatch.setattr(sandbox, "run", unexpected_run)
    result = await tools.handle(rc, _call("run_skill_script", name="demo-skill", script="scripts/summarize.py"))
    assert result.status == "error" and result.output["code"] == "SANDBOX_DENIED"


async def test_confidential_skill_results_keep_manifest_classification(env):
    rc = await _rc(env, data_class="confidential")
    result = await tools.handle(rc, _call("read_skill_file", name="demo-skill", path="references/format.md"))
    assert result.status == "ok" and result.event["data_class"] == "confidential"


@pytest.mark.parametrize("manifest_class,expected", [("confidential", "confidential"), ("public", "internal")])
async def test_skill_broker_propagates_classification_to_log_and_taint(env, manifest_class, expected):
    from unittest.mock import AsyncMock

    from runtime.loop import toolbroker
    from runtime.loop.data_class import model_allowed, run_level

    rc = await _rc(env, data_class=manifest_class)
    rc.gate = SimpleNamespace(taint=set())
    rc.history = []
    rc.run_id = "classification-regression"
    call = SimpleNamespace(call_id="skill-read", tool_name="read_skill_file")
    entry = SimpleNamespace(tool=tools.READ_SKILL_FILE_TOOL, call=call, effective={"name": "demo-skill", "path": "references/format.md"})
    log = SimpleNamespace(append=AsyncMock())
    session = SimpleNamespace(commit=AsyncMock())
    await toolbroker._skill_tool(rc, log, session, entry)
    payload = log.append.call_args.args[2]
    assert payload["data_class"] == expected and rc.gate.taint == {expected}
    if expected == "confidential":
        assert not model_allowed("untrusted-external-model", run_level(rc.gate.taint))


@pytest.mark.parametrize("mode", ["inline", "progressive", "native", "eval-inline"])
async def test_confidential_skill_blocks_first_model_request(env, client, monkeypatch, mode):
    import dataclasses
    import json

    from runtime.assemble.assembler import assemble
    from runtime.loop import runner

    _, sha = _publish(env, data_class="confidential")
    options = {"skills_inline": True} if mode == "inline" else ({"skills_mode": "progressive"} if mode == "progressive" else {})
    agent = _agent(None, data_class="confidential") if mode == "eval-inline" else _agent([_lock(sha)], **options)
    if mode == "eval-inline":
        agent.system_prompt = "Confidential pre-publication Skill body"
    assembled = await assemble(agent, "human:alice")

    async def assemble_signed(*args, **kwargs):
        return assembled

    matrix = env.tmp / "blocked.json"
    matrix.write_text(json.dumps({"channels": {"model": {"public": ["*"], "internal": ["*"], "confidential": []}}}))
    monkeypatch.setattr(runner, "assemble", assemble_signed)
    monkeypatch.setattr(
        runner, "settings", dataclasses.replace(runner.settings, data_class_matrix_file=str(matrix), confidential_model_aliases=())
    )
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": [{"role": "user", "content": "hello"}]}) as response:
        run_id = response.headers["x-run-id"]
        events = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith("data:")]
    assert any(event.get("code") == "DATA_CLASS_MODEL_BLOCKED" for event in events)
    log = client.get(f"/v1/runs/{run_id}/events").json()
    assert not any(event["type"] == "model.request" for event in log)


@pytest.mark.parametrize("declared", [None, "secret", {}, []])
async def test_assemble_rejects_invalid_agent_data_class(env, declared):
    from runtime.assemble.assembler import assemble

    with pytest.raises(SkillVerifyError, match="config.data_class"):
        await assemble(_agent([], data_class=declared), "human:alice")


@pytest.mark.parametrize("declared,expected", [("confidential", "confidential"), ("internal", "internal"), ("public", "public")])
async def test_assemble_inherits_inline_agent_data_class_without_skill_lock(env, declared, expected):
    from runtime.assemble.assembler import assemble

    assembled = await assemble(_agent(None, data_class=declared), "human:alice")
    assert assembled.data_class == expected
