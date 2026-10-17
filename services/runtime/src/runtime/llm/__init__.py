"""LLM 子包：经 LiteLLM（Model Gateway）的 OpenAI 兼容客户端，供平台持有循环的 harness 调用模型。"""

from runtime.llm.client import (
    LLMChunk,
    LLMClient,
    LLMError,
    LLMResponse,
    LLMUsage,
    ModelInfo,
    ToolCall,
    UnknownModelAlias,
    get_client,
    set_transport_for_tests,
    to_openai_messages,
    to_openai_tool_name,
    to_openai_tools,
)

__all__ = [
    "LLMChunk",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "LLMUsage",
    "ModelInfo",
    "ToolCall",
    "UnknownModelAlias",
    "get_client",
    "set_transport_for_tests",
    "to_openai_messages",
    "to_openai_tool_name",
    "to_openai_tools",
]
