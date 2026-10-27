"""S9 WP-C：沙箱 L1（参数与失败关闭的单元用例；docker 可用且镜像已在本机时跑真实容器用例，否则 skip 并说明）。"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from runtime import sandbox
from runtime.sandbox import docker as sb_docker
from runtime.sandbox import settings as sb_settings


def _docker_ready() -> str | None:
    """真实容器用例的前置：docker 可用且默认镜像（按 digest）已在本机；不满足时返回 skip 原因。"""
    exe = shutil.which("docker")
    if not exe:
        return "docker CLI not found"
    try:
        r = subprocess.run([exe, "image", "inspect", sb_settings.DEFAULT_IMAGE], capture_output=True, timeout=20)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"docker unavailable: {type(e).__name__}"
    return None if r.returncode == 0 else "docker daemon unavailable or sandbox image not pulled"


DOCKER_SKIP = _docker_ready()
real = pytest.mark.skipif(DOCKER_SKIP is not None, reason=f"真实沙箱用例跳过：{DOCKER_SKIP}")
FILES = {"scripts/t.py": b"print('x')\n"}


# ---- 失败关闭 / 级别 ----
async def test_disabled_backend_fails_closed(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "disabled")
    with pytest.raises(sandbox.SandboxError) as ei:
        await sandbox.run("scripts/t.py", "read-only", "", None, files=FILES)
    assert ei.value.code == sandbox.SANDBOX_UNAVAILABLE


@pytest.mark.parametrize(
    ("env", "level", "workspace", "code"),
    [
        ({"SANDBOX_IMAGE": "python:3.12-slim"}, "read-only", None, "SANDBOX_UNAVAILABLE"),  # 未按 digest 钉定
        ({"SANDBOX_BACKEND": "podman"}, "read-only", None, "SANDBOX_UNAVAILABLE"),
        ({}, "none", None, "SANDBOX_DENIED"),
        ({}, "root", None, "SANDBOX_DENIED"),
        ({}, "full-access", "/tmp", "SANDBOX_DENIED"),  # noqa: S108 —— 未配 SANDBOX_EGRESS_PROXY
        ({}, "workspace-write", None, "SANDBOX_DENIED"),
        ({"SANDBOX_DOCKER_BIN": "/nonexistent/docker"}, "read-only", None, "SANDBOX_UNAVAILABLE"),
    ],
)
async def test_run_refusals(monkeypatch, env, level, workspace, code):
    monkeypatch.delenv("SANDBOX_EGRESS_PROXY", raising=False)
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(sandbox.SandboxError) as ei:
        await sandbox.run("scripts/t.py", level, "", workspace, files=FILES)
    assert ei.value.code == code


async def test_bundle_checks(monkeypatch):
    monkeypatch.setenv("SANDBOX_DOCKER_BIN", "/nonexistent/docker")
    with pytest.raises(sandbox.SandboxError, match="expected sha256"):
        await sandbox.run("scripts/t.py", "read-only", "", None, files=FILES, expected_sha256="0" * 64)
    with pytest.raises(sandbox.SandboxError, match="unsupported script type"):
        await sandbox.run("scripts/t.rb", "read-only", "", None, files={"scripts/t.rb": b""})
    with pytest.raises(sandbox.SandboxError, match="not part of"):
        await sandbox.run("scripts/other.py", "read-only", "", None, files=FILES)
    with pytest.raises(sandbox.SandboxError, match="illegal path"):
        await sandbox.run("scripts/t.py", "read-only", "", None, files=FILES | {"../x": b""})


def test_create_args_hardening_per_level(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_EGRESS_PROXY", "http://proxy.internal:3128")
    st = sb_settings.load()
    ro = sb_docker.create_args(st, "read-only", tmp_path, ["python", "/skill/t.py"])
    for flag in ("--read-only", "--cap-drop", "no-new-privileges", "65534:65534", "--pids-limit", "--memory", "never"):
        assert flag in ro
    assert ro[ro.index("--network") + 1] == "none" and not any("type=bind" in a for a in ro)  # read-only 不挂工作区
    ww = sb_docker.create_args(st, "workspace-write", tmp_path, ["python", "/skill/t.py"])
    assert ww[ww.index("--network") + 1] == "none" and any(a.startswith("type=bind,src=") and a.endswith("dst=/workspace") for a in ww)
    fa = sb_docker.create_args(st, "full-access", tmp_path, ["python", "/skill/t.py"])
    assert fa[fa.index("--network") + 1] == "bridge" and "HTTPS_PROXY=http://proxy.internal:3128" in fa
    assert ro[-3:] == [st.image, "python", "/skill/t.py"] and "@sha256:" in st.image


def test_build_tar_root_owned_readonly():
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(sb_docker.build_tar({"scripts/a.py": b"1", "SKILL.md": b"2"}))) as t:
        members = {m.name: m for m in t.getmembers()}
    assert set(members) == {"SKILL.md", "scripts", "scripts/a.py"}
    assert all(m.uid == 0 and m.gid == 0 for m in members.values())
    assert members["scripts"].mode == 0o755 and members["scripts/a.py"].mode == 0o644


# ---- 真实容器 ----
PROBE = b"""import os, socket, sys
data = sys.stdin.read()
print("stdin=" + data)
for target in ("/skill/scripts/p.py", "/etc/probe"):
    try:
        open(target, "w").write("x")
        print("WRITABLE " + target)
    except OSError:
        print("readonly " + target)
try:
    socket.create_connection(("1.1.1.1", 53), timeout=2)
    print("NETWORK OPEN")
except OSError:
    print("network blocked")
print("uid=%d" % os.getuid())
print("workspace=" + str(os.path.isdir("/workspace")))
"""


def _containers() -> int:
    exe = shutil.which("docker") or "docker"
    out = subprocess.run([exe, "ps", "-aq", "--filter", "label=ai-platform.sandbox=1"], capture_output=True, text=True, timeout=20)  # noqa: S603
    return len(out.stdout.split())


@real
async def test_real_read_only_no_network(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    before = _containers()
    res = await sandbox.run("scripts/p.py", "read-only", "hello", None, files={"scripts/p.py": PROBE})
    assert res.exit_code == 0 and not res.timed_out, res.stderr
    out = res.stdout
    assert "stdin=hello" in out and "network blocked" in out and "uid=65534" in out and "workspace=False" in out
    assert "readonly /skill/scripts/p.py" in out and "readonly /etc/probe" in out
    assert _containers() == before  # 容器与匿名卷已清理


@real
async def test_real_workspace_write(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    script = b"open('/workspace/out.txt', 'w').write('from sandbox')\nprint('ok')\n"
    ws = tmp_path / "ws"
    res = await sandbox.run("scripts/w.py", "workspace-write", "", ws, files={"scripts/w.py": script})
    assert res.exit_code == 0, res.stderr
    assert (ws / "out.txt").read_text() == "from sandbox"


@real
async def test_real_timeout_kills_and_cleans(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    before = _containers()
    res = await sandbox.run("scripts/s.py", "read-only", "", None, files={"scripts/s.py": b"import time\ntime.sleep(60)\n"}, timeout_s=3)
    assert res.timed_out and res.exit_code is None
    assert _containers() == before


@real
async def test_real_local_script_path_and_output_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    monkeypatch.setenv("SANDBOX_OUTPUT_MAX_BYTES", "100")
    p = tmp_path / "big.py"
    p.write_bytes(b"print('A' * 100000)\n")  # python 写入：透明加密下磁盘为密文，注入时 python 读明文
    res = await sandbox.run(p, "read-only", None, None)
    assert res.exit_code == 0 and res.stdout == "A" * 100
