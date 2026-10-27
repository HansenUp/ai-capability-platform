"""沙箱 L1 入口（S9.7，D146；冻结契约 ⑤）：``run(script_path, level, stdin, workspace) -> SandboxResult``。

只执行经扫描、审批、签名的固定脚本（调用方 runtime.skills.tools 从已验签技能目录取脚本并逐字节核哈希），模型不能生成或
修改代码（§8 用户确认，与 ADR-0007 相容）。级别由调用方按清单声明传入，本模块绝不升级：

- ``read-only``：``--read-only --network none --cap-drop ALL --security-opt no-new-privileges --user 65534 --pids-limit 64
  --memory 128m`` + tmpfs ``/tmp``，不挂工作区；
- ``workspace-write``：在 read-only 的基础上加挂本 Run 工作区到 ``/workspace``（可写）；
- ``full-access``：未配置 ``SANDBOX_EGRESS_PROXY`` 一律拒绝；配置后接入 ``SANDBOX_FULL_ACCESS_NETWORK`` 并设代理环境变量；
- ``none`` 或未知级别：拒绝（SANDBOX_DENIED）。

``SANDBOX_BACKEND=disabled``、docker 不可用、镜像未按 digest 钉定或不在本机 → ``SANDBOX_UNAVAILABLE``（失败关闭）。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from runtime.sandbox import settings as sandbox_settings

SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
SANDBOX_DENIED = "SANDBOX_DENIED"
LEVELS = ("read-only", "workspace-write", "full-access")
_INTERPRETERS = {".py": ("python", "-I", "-B"), ".sh": ("sh",)}


class SandboxError(Exception):
    """沙箱无法执行（code：SANDBOX_UNAVAILABLE / SANDBOX_DENIED）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int | None  # 超时被杀时为 None
    stdout: str
    stderr: str
    timed_out: bool


def _rel_ok(rel: str) -> bool:
    parts = rel.split("/")
    return bool(rel) and not rel.startswith("/") and "\\" not in rel and all(p not in ("", ".", "..") for p in parts)


async def run(
    script_path: str | Path,
    level: str,
    stdin: str | bytes | None = None,
    workspace: str | Path | None = None,
    *,
    files: Mapping[str, bytes] | None = None,
    expected_sha256: str | None = None,
    timeout_s: float | None = None,
    operation_id: str | None = None,
    before_start: Callable[[], Awaitable[None]] | None = None,
) -> SandboxResult:
    """在一次性容器中执行脚本。

    - ``files`` 给出时：``script_path`` 是 ``files`` 内的相对路径，``files`` 整体注入 ``/skill``（技能目录的全部已核哈希文件）；
    - 否则 ``script_path`` 为本地文件路径，以 python 读取明文后作为 ``/skill/<文件名>`` 注入。
    ``expected_sha256`` 给出时脚本字节须与之一致。脚本以 tar 流经 ``docker cp`` 注入（不走绑定挂载，避开本机透明加密）。"""
    st = sandbox_settings.load()
    if level not in LEVELS:
        raise SandboxError(SANDBOX_DENIED, f"sandbox level {level!r} does not allow script execution")
    if st.backend == "disabled":
        raise SandboxError(SANDBOX_UNAVAILABLE, "SANDBOX_BACKEND=disabled")
    if st.backend != "docker":
        raise SandboxError(SANDBOX_UNAVAILABLE, f"unknown SANDBOX_BACKEND {st.backend!r}")
    if "@sha256:" not in st.image:
        raise SandboxError(SANDBOX_UNAVAILABLE, "SANDBOX_IMAGE must be pinned by digest (name@sha256:...)")
    if level == "full-access" and not st.egress_proxy:
        raise SandboxError(SANDBOX_DENIED, "full-access requires SANDBOX_EGRESS_PROXY")
    if level == "workspace-write" and not workspace:
        raise SandboxError(SANDBOX_DENIED, "workspace-write requires a run workspace")
    if files is not None:
        rel = PurePosixPath(str(script_path)).as_posix()
        if rel not in files:
            raise SandboxError(SANDBOX_DENIED, f"script {rel!r} is not part of the injected files")
        bundle = dict(files)
    else:
        p = Path(script_path)
        rel = p.name
        bundle = {rel: await asyncio.to_thread(p.read_bytes)}  # python 读取：透明加密下得到明文
    if not _rel_ok(rel) or any(not _rel_ok(k) for k in bundle):
        raise SandboxError(SANDBOX_DENIED, "illegal path in sandbox bundle")
    if expected_sha256 is not None and hashlib.sha256(bundle[rel]).hexdigest() != expected_sha256:
        raise SandboxError(SANDBOX_DENIED, f"script {rel!r} does not match its expected sha256")
    interp = _INTERPRETERS.get(PurePosixPath(rel).suffix)
    if interp is None:
        raise SandboxError(SANDBOX_DENIED, f"unsupported script type {PurePosixPath(rel).suffix!r} (allowed: .py, .sh)")
    data = stdin.encode() if isinstance(stdin, str) else (stdin or b"")
    from runtime.sandbox import docker  # 延迟导入：disabled 时不触碰 docker 后端

    return await docker.run_container(
        [*interp, f"/skill/{rel}"],
        bundle,
        level,
        data,
        Path(workspace) if workspace else None,
        st,
        timeout_s or st.timeout_s,
        operation_id=operation_id,
        before_start=before_start,
    )
