"""沙箱 L1（S9.7，D146；冻结契约 ⑤ ``runtime.sandbox.run(script_path, level, stdin, workspace) -> SandboxResult``）。

- 单元（始终运行，模块就绪后）：``SANDBOX_BACKEND=disabled`` 一律 ``SANDBOX_UNAVAILABLE``（失败关闭）；未配置
  ``SANDBOX_EGRESS_PROXY`` 时 full-access 被拒绝。
- docker（``S9_SECURITY_STACK=1``，需本机 docker 与按 digest 预拉取的 ``SANDBOX_IMAGE``）：read-only 级别断网
  （``--network none``：TCP 连接与 DNS 解析均失败）、根文件系统只读、/tmp 可写（tmpfs）。

``run`` 可为同步或异步函数（结果为 awaitable 时 await）；失败以带 ``code`` 属性的异常表达。
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any

import pytest

STACK = os.environ.get("S9_SECURITY_STACK") == "1"

NET_PROBE = """\
import json, socket
out = {}
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3).close()
    out["tcp"] = "open"
except OSError as e:
    out["tcp"] = "blocked:" + type(e).__name__
try:
    socket.getaddrinfo("example.com", 443)
    out["dns"] = "open"
except OSError as e:
    out["dns"] = "blocked:" + type(e).__name__
try:
    open("/sandbox-write-probe", "w").write("x")
    out["rootfs"] = "writable"
except OSError as e:
    out["rootfs"] = "readonly:" + type(e).__name__
try:
    open("/tmp/probe", "w").write("x")
    out["tmp"] = "writable"
except OSError as e:
    out["tmp"] = "blocked:" + type(e).__name__
print(json.dumps(out))
"""


def _sandbox() -> Any:
    mod = pytest.importorskip("runtime.sandbox", reason="runtime.sandbox（WP-C）尚未就绪")
    if getattr(mod, "__file__", None) is None:  # 只有 settings.py 的命名空间包：入口尚未落地
        pytest.skip("runtime.sandbox（WP-C）尚未就绪：缺 __init__.py / run 入口")
    return mod


async def _run(sb: Any, script: Path, level: str, stdin: bytes = b"", workspace: Path | None = None) -> Any:
    res = sb.run(script, level, stdin, workspace)
    return await res if inspect.isawaitable(res) else res


@pytest.fixture
def probe(tmp_path: Path) -> Path:
    p = tmp_path / "probe.py"
    p.write_text(NET_PROBE, encoding="utf-8")
    return p


async def test_disabled_backend_fails_closed(monkeypatch: pytest.MonkeyPatch, probe: Path) -> None:
    sb = _sandbox()
    monkeypatch.setenv("SANDBOX_BACKEND", "disabled")
    with pytest.raises(Exception) as ei:  # noqa: B017 —— 异常类型由 WP-C 定义，断言落在 code 上
        await _run(sb, probe, "read-only")
    assert getattr(ei.value, "code", None) == "SANDBOX_UNAVAILABLE", repr(ei.value)


async def test_full_access_requires_egress_proxy(monkeypatch: pytest.MonkeyPatch, probe: Path) -> None:
    sb = _sandbox()
    monkeypatch.delenv("SANDBOX_EGRESS_PROXY", raising=False)
    with pytest.raises(Exception) as ei:  # noqa: B017
        await _run(sb, probe, "full-access")
    assert str(getattr(ei.value, "code", "")).startswith("SANDBOX_"), repr(ei.value)


@pytest.mark.skipif(not STACK, reason="需本机 docker 与预拉取的沙箱镜像；S9_SECURITY_STACK=1 时运行（verify-s9 / 夜间 CI）")
async def test_read_only_sandbox_has_no_network(monkeypatch: pytest.MonkeyPatch, probe: Path) -> None:
    import json

    sb = _sandbox()
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    res = await _run(sb, probe, "read-only")
    assert res.exit_code == 0 and not res.timed_out, res
    out = json.loads(res.stdout)
    assert out["tcp"].startswith("blocked") and out["dns"].startswith("blocked"), out
    assert out["rootfs"].startswith("readonly") and out["tmp"] == "writable", out
