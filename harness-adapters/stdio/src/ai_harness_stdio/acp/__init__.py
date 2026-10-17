"""ACP 桥（S6 WP-A，D91～D97）：让讲 ACP 的 harness（goose、dsh）以 StdioHarness 子进程命令的方式接入平台。

    HARNESS_STDIO_CMD="python -m ai_harness_stdio.acp.bridge --profile goose"
    或在进程内：AcpStdioHarness("goose")

需要可选依赖 ``ai-harness-stdio[acp]``（uvicorn / starlette / httpx）。模块说明见 ``bridge``（轮次语义与
AcpProfile 接口）、``shim``（模型代理）、``toolproxy``（平台工具 MCP 代理）。

本包按需导入子模块（``python -m ai_harness_stdio.acp.bridge`` 运行时不会先把 bridge 作为普通模块导入一遍）。
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "ISOLATION_VIOLATION": "bridge",
    "AcpBridge": "bridge",
    "AcpProfile": "bridge",
    "AcpStdioHarness": "bridge",
    "AgentStartError": "bridge",
    "BaseAcpProfile": "bridge",
    "CommandProfile": "bridge",
    "IsolationError": "bridge",
    "LaunchContext": "bridge",
    "bridge_command": "bridge",
    "isolated_env": "bridge",
    "load_profile": "bridge",
    "ConnectionClosed": "jsonrpc",
    "JsonRpcConnection": "jsonrpc",
    "JsonRpcError": "jsonrpc",
    "LlmShim": "shim",
    "ScriptedBackend": "shim",
    "UpstreamBackend": "shim",
    "from_openai_messages": "shim",
    "to_openai_messages": "shim",
    "MCP_SERVER_NAME": "toolproxy",
    "McpToolProxy": "toolproxy",
    "ToolNameConflictError": "toolproxy",
    "ToolNames": "toolproxy",
    "wire_tool_name": "toolproxy",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)
