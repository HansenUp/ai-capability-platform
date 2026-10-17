"""S5 上架门禁范围（WP-C）：registry 能加载的每个 harness 都必须登记探针工厂，且工厂可导入。

claude-sdk 的工厂由 WP-B 在 ai_harness_claude_sdk.testing 提供；不可用时本测试失败（不跳过），门禁同样失败。
"""

from __future__ import annotations

import pytest

from ai_harness_probe.gate import DEFAULT_KNOWN, load_entry
from runtime.harness import registry


def test_known_covers_every_loadable_harness():
    assert set(registry.KNOWN) == {"mock", "litellm", "claude-sdk", "stdio"}
    assert load_entry(DEFAULT_KNOWN) is registry.KNOWN  # 门禁 CLI 默认遍历的就是 registry.KNOWN
    with pytest.raises(KeyError):
        registry.get("not-a-registered-harness")


@pytest.mark.parametrize("name", sorted(registry.KNOWN))
def test_probe_factory_importable(name: str):
    factory = load_entry(registry.KNOWN[name])
    assert callable(factory), f"{registry.KNOWN[name]} is not callable"
