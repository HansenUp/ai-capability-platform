"""Claude Agent SDK 适配器（该 SDK 实际以子进程方式运行 Claude Code CLI，因此传输方式声明为 stdio）。

对应 Runtime Contract 六要素的映射关系（细节见 ``harness`` 与 ``transcript`` 模块文档）：
- session      → 每个 SessionHandle 承载先后多次 SDK 查询（ClaudeSDKClient），每次以 ResultMessage 结束
- event stream → SDK 消息 / 流式增量 → 统一 Event（Text / Thinking / Usage / Error / Done）
- tool inject  → 平台工具注册为进程内 MCP server ``platform``；handler 只桥接，执行与闸口在平台侧；
                 SDK PreToolUse 回调兜底拒绝平台以外的工具
- interrupt    → client.interrupt() + 取消后台查询 + 结束挂起的工具 Future + 删除会话临时目录（幂等）
- hooks        → 四个拦截点全部由平台执行（未声明 internal_hook_points）
- checkpoint   → 平台历史合成为 CLI 会话转录，以 CLI 原生 resume 续接（``native_ref = transcript:<uuid>``）

模型接入：CLI 指向 Anthropic Messages 兼容端点（默认 compose 的 LiteLLM ``/v1/messages``），
模型名用 SessionSpec.model_alias；配置项见 ``ClaudeSdkConfig``。进程内测试模式（真实 SDK + 脚本化 CLI 传输层）
见 ``ai_harness_claude_sdk.testing``。

SDK 为可选依赖；若未安装，适配器会在构造时抛出带安装提示的 ImportError。
"""

from __future__ import annotations

from .harness import ClaudeSdkConfig, ClaudeSdkHarness, HarnessInterruptedError
from .transcript import (
    PLATFORM_SERVER,
    TOOL_PREFIX,
    ToolNameConflictError,
    TranscriptError,
    from_api_messages,
    to_api_messages,
    tool_name_map,
    wire_tool_name,
    write_transcript,
)

__all__ = [
    "PLATFORM_SERVER",
    "TOOL_PREFIX",
    "ClaudeSdkConfig",
    "ClaudeSdkHarness",
    "HarnessInterruptedError",
    "ToolNameConflictError",
    "TranscriptError",
    "from_api_messages",
    "to_api_messages",
    "tool_name_map",
    "wire_tool_name",
    "write_transcript",
]
