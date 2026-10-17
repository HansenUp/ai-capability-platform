"""goose 的契约探针工厂：真实 ``goose acp`` + ACP 桥 + shim 脚本模式（探针假模型，不调真实模型）。

goose 不存在时工厂抛 FileNotFoundError（门禁记为不通过 / 不可用；pytest 用例据 ``goose_binary()`` skip）。
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

from ai_harness_probe import ModelScript, ProbeTarget

from .. import PoolConfig
from ..acp.testing import acp_probe_factory
from .profile import goose_binary


def probe_factory(script: ModelScript) -> AbstractAsyncContextManager[ProbeTarget]:
    if goose_binary() is None:
        raise FileNotFoundError("goose binary not found (set GOOSE_BIN or install block-goose-cli)")
    return acp_probe_factory(script, profile="goose")


def pooled_probe_factory(script: ModelScript) -> AbstractAsyncContextManager[ProbeTarget]:
    """池模式（min_idle=1、max_workers=4）：worker 经 hello→ready 预热（agent 已启动并完成 ACP initialize）。"""
    if goose_binary() is None:
        raise FileNotFoundError("goose binary not found (set GOOSE_BIN or install block-goose-cli)")
    return acp_probe_factory(script, profile="goose", pool_config=PoolConfig(min_idle=1, max_workers=4))
