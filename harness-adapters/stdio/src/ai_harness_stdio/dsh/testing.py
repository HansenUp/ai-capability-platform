"""dsh 的契约探针工厂：真实 ``dsh --profile acp``（平台 patch）+ ACP 桥 + shim 脚本模式（探针假模型）。

dsh 或 node 不存在时工厂抛 FileNotFoundError（pytest 用例据 ``dsh_binary()`` / ``node_binary()`` skip）。
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from ai_harness_probe import ModelScript, ProbeTarget

from .. import PoolConfig
from ..acp.testing import acp_probe_factory
from .profile import dsh_binary, node_binary


def probe_factory(script: ModelScript) -> AbstractAsyncContextManager[ProbeTarget]:
    if dsh_binary() is None or node_binary() is None:
        raise FileNotFoundError("dsh or node not found (set DSH_BIN / DSH_NODE)")
    return acp_probe_factory(script, profile="dsh")


def pooled_probe_factory(script: ModelScript) -> AbstractAsyncContextManager[ProbeTarget]:
    """池模式（min_idle=1、max_workers=4）：worker 经 hello→ready 预热（agent 已启动并完成 ACP initialize）。"""
    if dsh_binary() is None or node_binary() is None:
        raise FileNotFoundError("dsh or node not found (set DSH_BIN / DSH_NODE)")
    return acp_probe_factory(script, profile="dsh", pool_config=PoolConfig(min_idle=1, max_workers=4))
