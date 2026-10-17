"""Harness adapter registry. Adapters are selected per agent (falls back to the node default).
（Harness 适配器注册表：按 agent 指定的 harness 名称选择适配器，未指定时回退到节点默认 harness；
各适配器按需懒加载并缓存。）
"""

from __future__ import annotations

import os

from ai_contract import HarnessAdapter
from runtime.config import settings

_REGISTRY: dict[str, HarnessAdapter] = {}  # 进程内已加载的 harness 适配器缓存

# registry 可加载的全部 harness 名 → 其契约探针工厂入口（"模块:属性"，由门禁 `python -m ai_harness_probe.gate`
# 按需延迟导入，S5 §1.3 / D86）。新增 harness 必须同时登记探针工厂，否则 _load 拒绝加载、门禁测试失败。
KNOWN: dict[str, str] = {
    "mock": "runtime.harness.mock:probe_factory",
    "litellm": "runtime.harness.mock:litellm_probe_factory",
    "claude-sdk": "ai_harness_claude_sdk.testing:probe_factory",
    "stdio": "ai_harness_stdio.testing:probe_factory",
}


def register(name: str, adapter: HarnessAdapter) -> None:
    """将一个已构造的适配器实例注册到全局注册表。"""
    _REGISTRY[name] = adapter


def get(name: str | None = None) -> HarnessAdapter:
    """按名称获取一个适配器；若尚未加载则先懒加载，未指定名称时使用节点默认 harness。"""
    key = name or settings.harness
    if key not in _REGISTRY:
        _load(key)
    return _REGISTRY[key]


def _load(key: str) -> None:
    """按 key 懒加载并注册对应的 harness 适配器。mock 为无模型的开发/CI 适配器；litellm 为平台持有循环、
    经 LiteLLM 网关调用模型的适配器（runtime.llm）；claude-sdk/stdio 为可选依赖，
    仅在实际选用时才导入，避免未安装对应扩展包时影响其他 harness 的可用性。不在 KNOWN 中的名字一律拒绝。"""
    if key not in KNOWN:
        raise KeyError(f"unknown harness '{key}'")
    if key == "mock":
        from runtime.harness.mock import MockHarness

        register("mock", MockHarness())
    elif key == "litellm":
        from runtime.harness.litellm import LiteLLMHarness

        register("litellm", LiteLLMHarness())
    elif key == "claude-sdk":
        from ai_harness_claude_sdk import ClaudeSdkConfig, ClaudeSdkHarness  # optional dependency（可选依赖）

        # 网关凭据可能来自 Vault / 挂载文件（S2.4），不在 os.environ 里：显式交给适配器
        env = {**os.environ, "CLAUDE_SDK_AUTH_TOKEN": settings.claude_sdk_auth_token or settings.model_gateway_api_key}
        register("claude-sdk", ClaudeSdkHarness(config=ClaudeSdkConfig.from_env(env)))
    elif key == "stdio":
        from ai_harness_stdio import StdioHarness  # optional dependency（可选依赖）

        register("stdio", StdioHarness.from_env())
    else:
        raise KeyError(f"unknown harness '{key}'")


def names() -> list[str]:
    """返回当前已加载的 harness 名称列表（按字母序）。"""
    return sorted(_REGISTRY)
