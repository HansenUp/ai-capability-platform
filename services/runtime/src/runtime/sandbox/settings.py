"""沙箱 L1 的配置（S9.7，D146）。每次 ``load()`` 现读环境变量（便于测试）；``config.py`` 归 WP-B，故独立成模块。

- ``SANDBOX_BACKEND``：``docker``（默认）| ``disabled``（一律 SANDBOX_UNAVAILABLE，失败关闭）。
- ``SANDBOX_IMAGE``：沙箱镜像，必须按 digest 钉定（含 ``@sha256:``），否则拒绝运行；默认 python:3.12-slim 的本机 digest。
  运行时以 ``--pull never`` 创建容器，镜像须预先拉取（Runtime 自身不出网拉镜像）。
- ``SANDBOX_TIMEOUT_S``（默认 30）、``SANDBOX_MEMORY``（默认 128m）、``SANDBOX_PIDS_LIMIT``（默认 64）、
  ``SANDBOX_TMPFS_SIZE``（默认 16m）、``SANDBOX_CPUS``（默认 1）、
  ``SANDBOX_OUTPUT_MAX_BYTES``（stdout / stderr 各自截断上限，默认 64 KiB）。
- ``SANDBOX_MAX_CONCURRENCY``：同时运行的沙箱容器数（默认 1，串行；本机 VM 内存紧）。
- ``SANDBOX_EGRESS_PROXY``：full-access 级别的出站代理（未配置即拒绝 full-access）；``SANDBOX_FULL_ACCESS_NETWORK``：
  full-access 容器所接入的 docker 网络（默认 bridge，应配为只能到达代理的内部网络）。
- ``SANDBOX_DOCKER_BIN``：docker 可执行文件（默认 docker）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_IMAGE = "python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"  # python:3.12-slim（2026-10-18 拉取）


def _e(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class SandboxSettings:
    backend: str = field(default_factory=lambda: _e("SANDBOX_BACKEND", "docker"))
    image: str = field(default_factory=lambda: _e("SANDBOX_IMAGE", DEFAULT_IMAGE))
    timeout_s: float = field(default_factory=lambda: float(_e("SANDBOX_TIMEOUT_S", "30")))
    memory: str = field(default_factory=lambda: _e("SANDBOX_MEMORY", "128m"))
    pids_limit: int = field(default_factory=lambda: int(_e("SANDBOX_PIDS_LIMIT", "64")))
    tmpfs_size: str = field(default_factory=lambda: _e("SANDBOX_TMPFS_SIZE", "16m"))
    cpus: str = field(default_factory=lambda: _e("SANDBOX_CPUS", "1"))
    output_max_bytes: int = field(default_factory=lambda: int(_e("SANDBOX_OUTPUT_MAX_BYTES", str(64 * 1024))))
    max_concurrency: int = field(default_factory=lambda: max(1, int(_e("SANDBOX_MAX_CONCURRENCY", "1"))))
    egress_proxy: str | None = field(default_factory=lambda: os.environ.get("SANDBOX_EGRESS_PROXY") or None)
    full_access_network: str = field(default_factory=lambda: _e("SANDBOX_FULL_ACCESS_NETWORK", "bridge"))
    docker_bin: str = field(default_factory=lambda: _e("SANDBOX_DOCKER_BIN", "docker"))


def load() -> SandboxSettings:
    return SandboxSettings()
