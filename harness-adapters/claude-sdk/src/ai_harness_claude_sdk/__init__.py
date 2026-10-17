"""Claude Agent SDK 适配器（该 SDK 实际以子进程方式运行 Claude Code CLI，因此传输方式声明为 stdio）。

对应 Runtime Contract 六要素的映射关系（S1 最小实现，细节见 ``harness`` 模块文档）：
- session      → 每个 SessionHandle 一个后台 SDK 查询（ClaudeSDKClient），以 ResultMessage 结束
- event stream → SDK 消息 / 流式增量 → 统一 Event（Text / Thinking / Usage / Error / Done）
- tool inject  → 平台工具注册为进程内 MCP server ``platform``；handler 只桥接，执行与闸口在平台侧
- interrupt    → client.interrupt() + 取消后台查询 + 结束挂起的工具 Future + 删除会话临时目录（幂等）
- hooks        → HookSet.pre_model 在开启新查询前调用（只覆盖首轮，返回 deny 则不启动查询）；
                 工具闸口由平台 toolbroker 负责
- checkpoint   → resume 等同新开会话，历史由 runner 在 messages 中给出并渲染成文本前缀；
                 原生 SDK resume 留到 S5 评估

模型接入：CLI 指向 Anthropic Messages 兼容端点（默认 compose 的 LiteLLM ``/v1/messages``），
模型名用 SessionSpec.model_alias；配置项见 ``ClaudeSdkConfig``。

SDK 为可选依赖；若未安装，适配器会在构造时抛出带安装提示的 ImportError。
"""

from __future__ import annotations

from .harness import PLATFORM_SERVER, TOOL_PREFIX, ClaudeSdkConfig, ClaudeSdkHarness, HarnessInterruptedError, build_prompt

__all__ = ["PLATFORM_SERVER", "TOOL_PREFIX", "ClaudeSdkConfig", "ClaudeSdkHarness", "HarnessInterruptedError", "build_prompt"]
