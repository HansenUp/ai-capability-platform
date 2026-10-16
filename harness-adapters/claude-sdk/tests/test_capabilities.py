"""校验 claude-sdk 适配器满足 HarnessAdapter 协议，或在 SDK 未安装时给出明确跳过原因。"""

import pytest

from ai_contract import HarnessAdapter


def test_adapter_satisfies_protocol_or_reports_missing_sdk():
    """未安装 claude-agent-sdk 时应跳过（预期情形）；安装时应满足协议且传输方式为 stdio（SDK 以子进程运行 CLI）。"""
    try:
        from ai_harness_claude_sdk import ClaudeSdkHarness

        h = ClaudeSdkHarness()
    except ImportError as e:
        pytest.skip(f"claude-agent-sdk not installed: {e}")
    assert isinstance(h, HarnessAdapter)
    assert h.capabilities().transport == "stdio"
