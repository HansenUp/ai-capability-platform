"""CI 门禁用的 ACP 桥探针入口（S6）：真实 ACP 桥 + fake ACP agent（本目录 fake_acp_agent.py）+ shim 脚本模式。

CI 不下载 goose / dsh（门禁对它们记 unavailable），ACP 桥本身由这里的 fake agent 过五项探针（直连与进程池两种模式）：

    PYTHONPATH=harness-adapters/stdio/tests/fakes python -m ai_harness_probe.gate --known fake_known:KNOWN
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

from ai_harness_stdio import PoolConfig
from ai_harness_stdio.acp.testing import acp_probe_factory

FAKE_AGENT = Path(__file__).resolve().with_name("fake_acp_agent.py")

probe_factory = functools.partial(acp_probe_factory, profile="cmd", agent_argv=[sys.executable, str(FAKE_AGENT)], name="fake-acp")
pooled_probe_factory = functools.partial(probe_factory, pool_config=PoolConfig(min_idle=1, max_workers=4))

KNOWN: dict[str, str] = {
    "acp-fake": "fake_known:probe_factory",
    "acp-fake-pooled": "fake_known:pooled_probe_factory",
}
