"""Context engineering: block budgets, stable-prefix ordering (for prompt caching) and tool-result offload.
（上下文工程：按区块（block）分配 token 预算、固定顺序保证前缀稳定（配合提示缓存 prompt caching）、
以及超大工具结果外置到工作区。）
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from ai_contract import is_image_ref

# Fixed block order. The first three blocks must be byte-stable across turns.
# （固定的区块顺序；前三个区块（system/skills/tools）必须在多轮之间保持字节级稳定，
# 这样才能让 stable_prefix_hash 保持不变，从而让 harness/模型侧的提示缓存生效。）
BLOCK_ORDER = ("system", "skills", "tools", "memory", "retrieval", "history")

# 200K 上下文窗口下的默认各区块 token 预算。
DEFAULT_BUDGET_200K = {"system": 4_000, "skills": 8_000, "tools": 12_000, "memory": 6_000, "retrieval": 16_000, "history": 150_000}

# 每张图片附件（历史里的 image_ref 引用块，S6 D102）按约 1.5k token 估算：历史文本里只有几十字节的引用，
# 模型侧却按整张图计费（Claude 约 1.1～1.6k、OpenAI 高精度约 0.8～1.1k），按引用文本估算会严重低估。
IMAGE_REF_TOKENS = 1_500


def estimate_tokens(text: str) -> int:
    """粗略估算文本的 token 数（按字符数 / 3），足够用于预算裁剪；后续可替换为真实模型分词器。"""
    # Cheap estimator good enough for budgeting; replace with the model tokenizer when available.
    return max(1, len(text) // 3)


@dataclass
class ContextPlan:
    """一次上下文组装的结果：各区块的最终文本、各区块用量、预算、稳定前缀哈希与被截断的区块列表。"""

    blocks: dict[str, str] = field(default_factory=dict)  # 各区块最终文本（可能已被截断）
    usage: dict[str, int] = field(default_factory=dict)  # 各区块估算 token 用量
    budget: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BUDGET_200K))  # 各区块预算上限
    stable_prefix_hash: str = ""  # 前三个固定区块拼接后的哈希，用于观测提示缓存是否命中
    truncated: list[str] = field(default_factory=list)  # 因超出预算而被截断的区块名列表

    def report(self) -> dict[str, Any]:
        """生成写入 CONTEXT_ASSEMBLED 事件的组装报告。"""
        return {"usage": self.usage, "budget": self.budget, "stable_prefix_hash": self.stable_prefix_hash, "truncated": self.truncated}


def count_image_refs(messages: list[dict[str, Any]]) -> int:
    """消息历史中图片引用块（image_ref）的张数。"""
    return sum(1 for m in messages if isinstance(m.get("content"), list) for part in m["content"] if is_image_ref(part))


def assemble(blocks: dict[str, str], budget: dict[str, int] | None = None, *, history_images: int = 0) -> ContextPlan:
    """按 BLOCK_ORDER 固定顺序遍历各区块：超预算的区块按字符数近似截断，并记录截断标记；
    最后对前三个稳定区块（system/skills/tools）拼接求哈希，作为稳定前缀哈希。
    history_images 为历史中的图片张数：history 区块的用量另加每张 IMAGE_REF_TOKENS（文本里只有引用）。
    """
    plan = ContextPlan(budget=dict(budget or DEFAULT_BUDGET_200K))
    for name in BLOCK_ORDER:
        text = blocks.get(name, "")
        limit = plan.budget.get(name, 0)
        extra = history_images * IMAGE_REF_TOKENS if name == "history" else 0
        if estimate_tokens(text) + extra > limit:
            text = text[: max(0, limit - extra) * 3]  # 按 estimate_tokens 的反向近似（每 token ≈ 3 字符）裁剪到预算内
            plan.truncated.append(name)
        plan.blocks[name] = text
        plan.usage[name] = estimate_tokens(text) + extra
    # 只对前三个固定区块（system/skills/tools）求哈希：这三者要求跨轮字节稳定，用于验证提示缓存前缀不变。
    prefix = "\n".join(plan.blocks[n] for n in BLOCK_ORDER[:3])
    plan.stable_prefix_hash = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
    return plan


def offload_if_large(output: Any, workspace_dir: str, threshold_tokens: int) -> tuple[Any, str | None]:
    """Write oversized tool output to the run workspace and return (summary, ref).
    （若工具输出的估算 token 数超过阈值，则整体写入本次 Run 的工作区文件，并返回
    (摘要文本, artifact:// 引用)；未超阈值则原样返回 (output, None)。）
    """
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    if estimate_tokens(text) <= threshold_tokens:
        return output, None
    os.makedirs(workspace_dir, exist_ok=True)
    ref = f"artifact://{uuid.uuid4().hex}"
    with open(os.path.join(workspace_dir, ref.split("//")[1] + ".txt"), "w", encoding="utf-8") as f:
        f.write(text)
    summary = text[:600] + f"\n…[truncated; full result at {ref}, {estimate_tokens(text)} tokens]"
    return summary, ref
