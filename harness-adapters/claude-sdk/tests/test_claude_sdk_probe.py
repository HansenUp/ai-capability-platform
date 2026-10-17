"""对 claude-sdk 进程内工厂运行五项契约探针（ai_harness_probe.run_probes，判据由 WP-C 实现），并核对能力门禁。

声明的 tool_interception=enforcing 需工具回灌 + Hooks 探针通过，can_resume_from_events=True 需重放续跑探针通过；
任一探针不通过或声明高于验证，本用例失败（不得以 skip/xfail 掩盖）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("claude_agent_sdk")

from ai_contract import capability_tier  # noqa: E402
from ai_harness_claude_sdk.testing import claude_sdk_probe_factory  # noqa: E402
from ai_harness_probe import ALL, run_probes  # noqa: E402


async def test_claude_sdk_passes_all_five_contract_probes() -> None:
    report = await run_probes(claude_sdk_probe_factory)
    assert [r.name for r in report.results] == list(ALL)
    assert {r.name: r.failures for r in report.results if not r.passed} == {}
    assert report.capability_violations() == []
    assert report.passed
    caps = report.capabilities
    assert (caps.name, caps.tool_interception, caps.can_resume_from_events) == ("claude-sdk", "enforcing", True)
    assert capability_tier(caps) == "L2"
