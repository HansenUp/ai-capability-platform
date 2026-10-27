"""沙箱 docker 后端（S9.7）：docker create → docker cp（tar 流注入 /skill）→ docker start -a -i（stdin、超时）→ docker rm -f -v。

- 容器根文件系统只读；``/skill`` 为匿名卷（docker cp 只能写入卷，read-only rootfs 下不能直接写根文件系统），文件属主 root、
  权限 0644 / 目录 0755，脚本以 65534 运行只能读不能改；``/tmp`` 为 tmpfs（noexec、nosuid、限额）。
- 输出按 ``SANDBOX_OUTPUT_MAX_BYTES`` 截断（边读边丢弃，不会因脚本刷屏耗尽内存）；超时 ``docker kill`` 并清理容器与匿名卷。
- 同时运行的容器数受 ``SANDBOX_MAX_CONCURRENCY`` 限制（默认 1，串行）。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import tarfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from runtime.sandbox.runner import SANDBOX_UNAVAILABLE, SandboxError, SandboxResult
from runtime.sandbox.settings import SandboxSettings

_sems: dict[int, tuple[int, asyncio.Semaphore]] = {}
CLEANUP_TIMEOUT_S = 30.0


def _sem(n: int) -> asyncio.Semaphore:
    key = id(asyncio.get_running_loop())
    cur = _sems.get(key)
    if cur is None or cur[0] != n:
        cur = _sems[key] = (n, asyncio.Semaphore(n))
    return cur[1]


def build_tar(bundle: Mapping[str, bytes]) -> bytes:
    """确定性 tar：目录 0755、文件 0644，属主 root，mtime 0。"""
    buf = io.BytesIO()
    dirs: set[str] = set()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in sorted(bundle):
            for parent in reversed(PurePosixPath(rel).parents):
                d = parent.as_posix()
                if d in (".", "") or d in dirs:
                    continue
                dirs.add(d)
                ti = tarfile.TarInfo(d)
                ti.type, ti.mode, ti.uid, ti.gid, ti.mtime = tarfile.DIRTYPE, 0o755, 0, 0, 0
                tar.addfile(ti)
            data = bundle[rel]
            ti = tarfile.TarInfo(rel)
            ti.size, ti.mode, ti.uid, ti.gid, ti.mtime = len(data), 0o644, 0, 0, 0
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def execution_user(level: str) -> str:
    if level == "read-only" or os.getuid() == 0:
        return "65534:65534"
    return f"{os.getuid()}:{os.getgid()}"


def create_args(
    st: SandboxSettings, level: str, workspace: Path | None, argv: Sequence[str], *, operation_id: str | None = None
) -> list[str]:
    """docker create 的参数（级别只决定网络与工作区挂载，其余加固项各级别一致）。"""
    args = [
        "create",
        "--pull",
        "never",
        "-i",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        execution_user(level),
        "--pids-limit",
        str(st.pids_limit),
        "--memory",
        st.memory,
        "--memory-swap",
        st.memory,
        "--cpus",
        st.cpus,
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={st.tmpfs_size}",  # noqa: S108 —— 容器内 tmpfs 挂载点，不是宿主临时文件
        "--mount",
        "type=volume,dst=/skill",
        "--label",
        "ai-platform.sandbox=1",
        "-e",
        "HOME=/tmp",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
    ]
    if level == "full-access":
        proxy = st.egress_proxy or ""
        args += ["--network", st.full_access_network]
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            args += ["-e", f"{k}={proxy}"]
    else:
        args += ["--network", "none"]
    if level in ("workspace-write", "full-access") and workspace is not None:
        args += ["--mount", f"type=bind,src={workspace.resolve()},dst=/workspace", "--workdir", "/workspace"]
    else:
        args += ["--workdir", "/tmp"]  # noqa: S108 —— 容器内工作目录
    if operation_id is not None:
        args += ["--label", f"ai-platform.operation={operation_id}"]
    return [*args, st.image, *argv]


async def _exec(
    st: SandboxSettings, args: Sequence[str], stdin: bytes | None = None, timeout_s: float = CLEANUP_TIMEOUT_S
) -> tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            st.docker_bin,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as e:
        raise SandboxError(SANDBOX_UNAVAILABLE, f"docker is not available: {type(e).__name__}") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout_s)
    except TimeoutError as e:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise SandboxError(SANDBOX_UNAVAILABLE, f"docker {args[0]} timed out") from e
    return proc.returncode or 0, out, err


async def _pump(stream: asyncio.StreamReader | None, cap: int) -> bytes:
    """读尽流，只保留前 cap 字节。"""
    if stream is None:
        return b""
    kept = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(kept)
        if len(kept) < cap:
            kept += chunk[: cap - len(kept)]


async def _feed(proc: asyncio.subprocess.Process, data: bytes) -> None:
    if proc.stdin is None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        if data:
            proc.stdin.write(data)
            await proc.stdin.drain()
        proc.stdin.close()


def _decode(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


async def run_container(
    argv: Sequence[str],
    bundle: Mapping[str, bytes],
    level: str,
    stdin: bytes,
    workspace: Path | None,
    st: SandboxSettings,
    timeout_s: float,
    *,
    operation_id: str | None = None,
    before_start: Callable[[], Awaitable[None]] | None = None,
) -> SandboxResult:
    if workspace is not None and level in ("workspace-write", "full-access"):
        await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
    async with _sem(st.max_concurrency):
        rc, out, err = await _exec(st, create_args(st, level, workspace, argv, operation_id=operation_id))
        if rc != 0:
            raise SandboxError(SANDBOX_UNAVAILABLE, f"docker create failed: {_decode(err).strip()[:300]}")
        cid = _decode(out).strip()
        try:
            rc, _, err = await _exec(st, ["cp", "-", f"{cid}:/skill"], stdin=build_tar(bundle))
            if rc != 0:
                raise SandboxError(SANDBOX_UNAVAILABLE, f"docker cp failed: {_decode(err).strip()[:300]}")
            if before_start is not None:
                await before_start()
            try:
                proc = await asyncio.create_subprocess_exec(
                    st.docker_bin,
                    "start",
                    "-a",
                    "-i",
                    cid,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (FileNotFoundError, PermissionError) as e:
                raise SandboxError(SANDBOX_UNAVAILABLE, f"docker is not available: {type(e).__name__}") from e
            readers = asyncio.gather(_pump(proc.stdout, st.output_max_bytes), _pump(proc.stderr, st.output_max_bytes), _feed(proc, stdin))
            timed_out = False
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout_s)
            except TimeoutError:
                timed_out = True
                await _exec(st, ["kill", cid])
                try:
                    await asyncio.wait_for(proc.wait(), CLEANUP_TIMEOUT_S)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
            out_b, err_b, _ = await readers
            exit_code = None if timed_out else proc.returncode
            return SandboxResult(exit_code=exit_code, stdout=_decode(out_b), stderr=_decode(err_b), timed_out=timed_out)
        finally:
            with contextlib.suppress(SandboxError):
                await _exec(st, ["rm", "-f", "-v", cid])
