"""上下文压缩事件化（DEF-2，S9 D150）。

history（模型可见的消息历史）的估算 token 数超过预算时，runner 在写 MODEL_REQUEST 之前调用 ``plan_compaction``：

1. 选边界 ``upto_index``：只在"不拆工具调用与其结果、不拆同批调用"的位置切（切点上的消息不能是工具结果，切点前一条不能是
   工具调用消息），取能让保留部分落进目标预算（预算 × CONTEXT_COMPACT_TARGET_RATIO）的最小切点；都落不进时取最大的合法切点；
2. 生成摘要：``llm`` 模式经模型网关调用摘要别名（快照 ``context.summary_model_alias``，缺省 ``THINKING_SUMMARY_MODEL_ALIAS``，
   非推理模型）；``truncate`` 模式把被替换的消息拼成文本后截断（确定性、不出网）。Run 的数据等级不被矩阵准许发给摘要模型
   （如机密污点）或调用失败时退回截断模式；
3. 结果写成 CONTEXT_COMPACTED{upto_index, summary, method, tokens, dropped_hmac, data_class}（摘要继承 Run 的数据等级；
   被替换消息只留带密钥的指纹），``invariant.rebuild_messages`` 据此重建，实时与重放同形、续跑一致。

快照配置 ``context``：``{"compaction": "llm" | "truncate" | "off", "history_budget_tokens": int, "summary_model_alias": str}``，
全部可选；缺省为 truncate（确定性、不出网），快照显式选 llm 时发布校验摘要别名在模型网关上存在；预算取
CONTEXT_HISTORY_BUDGET_TOKENS。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

from ai_contract.data_class import Level
from runtime.config import Settings, settings
from runtime.context.budget import IMAGE_REF_TOKENS, count_image_refs, estimate_tokens

logger = logging.getLogger(__name__)

METHODS = ("llm", "truncate", "off")
SUMMARY_PREFIX = "[compacted summary] "  # 与 invariant.rebuild_messages 的重建规则一致
SUMMARY_PROMPT = (
    "你是对话压缩助手。把下面这段智能体与用户、工具之间的历史压缩成一段简洁的中文摘要，供智能体继续完成任务："
    "保留用户的目标与约束、已调用过的工具及其关键结果（编号、数值、结论）、已经做出的决定和尚未完成的事项；"
    "不要编造，不要加入评价，不要输出与历史无关的内容。"
)


@dataclass(frozen=True)
class ContextConfig:
    """快照 context 配置（已校验）。"""

    method: str
    history_budget_tokens: int
    summary_model_alias: str


def config_of(agent: Any, cfg: Settings | None = None) -> ContextConfig:
    """读取 Agent 版本快照的 context 配置；不合规抛 ValueError（发布时拒绝，运行期失败关闭）。"""
    c = cfg or settings
    raw = (agent.config or {}).get("context")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("context must be an object")
    unknown = sorted(set(raw) - {"compaction", "history_budget_tokens", "summary_model_alias"})
    if unknown:
        raise ValueError(f"context has unknown keys {unknown}")
    method = raw.get("compaction", "truncate")
    if method not in METHODS:
        raise ValueError(f"context.compaction must be one of {', '.join(METHODS)}")
    budget = raw.get("history_budget_tokens", c.context_history_budget_tokens)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 256:
        raise ValueError("context.history_budget_tokens must be an integer >= 256")
    alias = raw.get("summary_model_alias", c.thinking_summary_model_alias)
    if not isinstance(alias, str) or not alias:
        raise ValueError("context.summary_model_alias must be a non-empty string")
    return ContextConfig(method, budget, alias)


def history_tokens(history: list[dict[str, Any]]) -> int:
    """history 的估算 token 数（与 CONTEXT_ASSEMBLED 的 history 区块同口径：JSON 文本 + 每张图片 IMAGE_REF_TOKENS）。"""
    return estimate_tokens(json.dumps(history, ensure_ascii=False)) + count_image_refs(history) * IMAGE_REF_TOKENS


def _is_tool_call(m: dict[str, Any]) -> bool:
    return m.get("role") == "assistant" and ("tool_use" in m or "tool_uses" in m)


def boundaries(history: list[dict[str, Any]]) -> list[int]:
    """合法切点（1 ≤ k < len）：history[k] 不是工具结果，history[k-1] 不是工具调用消息——不拆调用与结果、不拆同批。"""
    return [k for k in range(1, len(history)) if history[k].get("role") != "tool" and not _is_tool_call(history[k - 1])]


def choose_boundary(history: list[dict[str, Any]], target_tokens: int) -> int | None:
    """能让保留部分落进 target_tokens 的最小合法切点；都落不进时取最大的合法切点；没有合法切点返回 None。"""
    valid = boundaries(history)
    if not valid:
        return None
    for k in valid:
        if history_tokens(history[k:]) <= target_tokens:
            return k
    return valid[-1]


def _render(messages: list[dict[str, Any]]) -> str:
    """把被替换的消息渲染成摘要输入 / 截断摘要的文本（工具调用与结果写成一行，图片只留引用）。"""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        uses = [m["tool_use"]] if isinstance(m.get("tool_use"), dict) else (m.get("tool_uses") or [])
        if role == "assistant" and uses:
            for u in uses:
                lines.append(f"[调用工具] {u.get('name')} {json.dumps(u.get('input') or {}, ensure_ascii=False)}")
        elif role == "tool":
            content = m.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
            lines.append(f"[工具结果 {m.get('call_id')}] {text}")
        else:
            content = m.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
            lines.append(f"[{role}] {text}")
    return "\n".join(lines)


def truncate_summary(messages: list[dict[str, Any]], max_chars: int) -> str:
    """截断模式：渲染文本超过 max_chars 时保留首尾各一半（中间以省略标记连接），确定性、不出网。"""
    text = _render(messages)
    if len(text) <= max_chars:
        return text
    half = max(1, max_chars // 2)
    return f"{text[:half]}\n…[{len(text) - 2 * half} chars omitted]…\n{text[-half:]}"


def dropped_hmac(messages: list[dict[str, Any]], cfg: Settings | None = None) -> str:
    """被替换消息的规范化 JSON 的 HMAC-SHA256（密钥 HOOK_DIGEST_KEY，与 toolbroker.hmac_json 同算法）：日志只留指纹。"""
    canon = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new((cfg or settings).hook_digest_key.encode("utf-8"), canon.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Compaction:
    """一次压缩的结论（CONTEXT_COMPACTED 的 payload 与对历史的变换）。"""

    upto_index: int
    summary: str
    method: str
    tokens: dict[str, int]
    dropped_hmac: str
    data_class: str
    fallback: str | None = None  # llm 模式退回截断的原因（data_class / llm_error），有才写

    model_usage_record: dict[str, Any] | None = None  # diagnostic only, not execution-budget input

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "upto_index": self.upto_index,
            "summary": self.summary,
            "method": self.method,
            "tokens": self.tokens,
            "dropped_hmac": self.dropped_hmac,
            "data_class": self.data_class,
        }
        if self.fallback:
            out["fallback"] = self.fallback
        if self.model_usage_record is not None:
            out["model_usage_record"] = self.model_usage_record
        return out

    def apply(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """与 invariant.rebuild_messages 的 CONTEXT_COMPACTED 规则一致。"""
        return [{"role": "system", "content": f"{SUMMARY_PREFIX}{self.summary}"}, *history[self.upto_index :]]


async def _llm_summary(
    alias: str, messages: list[dict[str, Any]], max_chars: int, request_extra: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    from runtime import llm  # 延迟导入：llm 客户端依赖 config，避免包初始化时的循环导入

    resp = await llm.get_client().chat(
        alias,
        [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": _render(messages)}],
        max_tokens=max(256, min(4096, max_chars // 2)),
        temperature=0,
        extra=request_extra or None,
    )
    text = (resp.text or "").strip()
    if not text:
        raise ValueError("summary model returned empty text")
    return text[:max_chars], {
        "source": "context_compaction",
        "requested_alias": alias,
        "response_model": resp.model,
        "usage": asdict(resp.usage),
        "coverage": "response_record_only",
        "billing_verified": False,
    }


async def plan_compaction(
    history: list[dict[str, Any]],
    agent: Any,
    level: Level,
    *,
    model_allowed: Any,
    request_extra: dict[str, Any] | None = None,
    cfg: Settings | None = None,
) -> Compaction | None:
    """history 超预算时给出压缩结论（不写日志、不改 history）；未超预算、配置为 off 或没有合法切点时返回 None。
    model_allowed(alias, level) 为模型通道判定（runner 传入数据分级矩阵的判定）；request_extra 为摘要请求按等级附加的字段。
    配置不合规抛 ValueError。"""
    c = cfg or settings
    conf = config_of(agent, c)
    before = history_tokens(history)
    if conf.method == "off" or before <= conf.history_budget_tokens:
        return None
    k = choose_boundary(history, int(conf.history_budget_tokens * c.context_compact_target_ratio))
    if k is None:
        logger.warning("history over budget (%s > %s tokens) but has no compaction boundary", before, conf.history_budget_tokens)
        return None
    dropped = history[:k]
    # 摘要上限：CONTEXT_SUMMARY_MAX_CHARS 与预算的 1/4（按每 token ≈ 3 字符）取小，保证压缩后确实落回预算
    max_chars = max(200, min(c.context_summary_max_chars, conf.history_budget_tokens * 3 // 4))
    method, fallback = conf.method, None
    summary = ""
    usage_record = None
    if method == "llm" and not model_allowed(conf.summary_model_alias, level):
        method, fallback = "truncate", "data_class"  # 数据分级矩阵不准许把该等级的上下文发给摘要模型：不出网
    if method == "llm":
        try:
            summary, usage_record = await _llm_summary(conf.summary_model_alias, dropped, max_chars, request_extra or {})
        except Exception as exc:  # 摘要失败不阻断 Run：退回截断模式（确定性）
            logger.warning("compaction summary via %s failed: %s", conf.summary_model_alias, exc)
            method, fallback = "truncate", "llm_error"
    if method == "truncate":
        summary = truncate_summary(dropped, max_chars)
    result = Compaction(k, summary, method, {"before": before, "after": 0}, dropped_hmac(dropped, c), level.value, fallback)
    after = history_tokens(result.apply(history))
    return Compaction(k, summary, method, {"before": before, "after": after}, result.dropped_hmac, level.value, fallback, usage_record)
