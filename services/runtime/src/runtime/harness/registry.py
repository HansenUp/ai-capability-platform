"""Harness adapter registry. Adapters are selected per agent (falls back to the node default).
（Harness 适配器注册表：按 agent 指定的 harness 名称选择适配器，未指定时回退到节点默认 harness；
各适配器按需懒加载并缓存。）
"""

from __future__ import annotations

import os
import shutil

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
    # S6：经 ACP 桥接入（D91）。门禁用真实二进制 + 脚本化 shim；缺二进制时门禁记 unavailable（见 ai_harness_probe.gate）
    "goose": "ai_harness_stdio.goose.testing:probe_factory",
    "dsh": "ai_harness_stdio.dsh.testing:probe_factory",
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
    经 LiteLLM 网关调用模型的适配器（runtime.llm）；claude-sdk/stdio/goose/dsh 为可选依赖，
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
    elif key in ("goose", "dsh"):
        register(key, _acp_harness(key))
    else:
        raise KeyError(f"unknown harness '{key}'")


def _acp_harness(profile: str) -> HarnessAdapter:
    """goose / dsh：经 ACP 桥的 StdioHarness + 每个 harness 一个进程池（S6 §3）。

    用 AcpStdioHarness 而不是 StdioHarness：后者在开会话前 capabilities() 报告平台持有循环（loop_owner=platform，
    capability_tier 直接 L2），发布闸门会据此放过高风险写白名单（D79）；前者开会话前即按 profile 报告 harness 持有循环。
    模型网关地址与密钥（可能来自 Vault / 挂载文件，不在 os.environ）只交给桥进程（shim 转发用），agent 子进程拿不到：
    桥不继承 Runtime 的环境（池的白名单），密钥经池为每个 worker 写的 0600 文件传入（桥读完即删），不进环境变量。"""
    from ai_harness_stdio import PoolConfig, WorkerPool  # optional dependency（可选依赖：ai-harness-stdio[acp]）
    from ai_harness_stdio.acp.bridge import AcpStdioHarness, bridge_command

    env = {"ACP_LLM_BASE_URL": settings.model_gateway_url}
    if profile == "goose":
        env["GOOSE_BIN"] = shutil.which(settings.goose_bin) or settings.goose_bin
    else:
        env["DSH_BIN"] = settings.dsh_bin
    config = PoolConfig(
        min_idle=settings.harness_pool_min_idle,
        max_workers=settings.harness_pool_max_workers,
        acquire_timeout_s=settings.harness_pool_acquire_timeout_s,
        rss_limit_mb=settings.harness_pool_rss_limit_mb,
        env=env,
        secret_env={"ACP_LLM_API_KEY": settings.model_gateway_api_key},
    )
    return AcpStdioHarness(profile, pool=WorkerPool(bridge_command(profile), config))


def names() -> list[str]:
    """返回当前已加载的 harness 名称列表（按字母序）。"""
    return sorted(_REGISTRY)
