"""Reconstructability invariant: every message the model can see must be rebuildable from the
append-only event log. Checked before each model request; a mismatch refuses the call.
（可重建性不变量：模型能看到的每一条消息都必须能够从追加写事件日志中重新构建出来。该不变量
在每次向模型发起请求前都会被校验一次；一旦不匹配（说明历史被意外篡改或与日志脱节），
直接拒绝本次模型调用，防止不可审计/不可重放的上下文被发送给模型。）
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ai_contract import RunEvent, RunEventType


class InvariantViolation(RuntimeError):
    """当"即将发送给模型的历史"与"从事件日志重建的历史"不一致时抛出。"""

    pass


def message_hash(messages: list[dict[str, Any]]) -> str:
    """对消息列表做规范化 JSON 序列化（按 key 排序、无多余空白）后取 SHA-256 哈希，用于比较等价性。"""
    canon = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def rebuild_messages(log: list[RunEvent], initial_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministically rebuild the model-visible message list from the event log.
    （确定性地从事件日志重建模型可见的消息列表：从初始消息出发，按日志顺序依次应用
    上下文压缩、模型响应、工具请求、工具结果四类事件，得到与当时驱动循环中完全一致的历史。）
    """
    msgs: list[dict[str, Any]] = list(initial_messages)
    compacted_upto = 0
    for ev in log:
        if ev.type == RunEventType.CONTEXT_COMPACTED:
            # 上下文被压缩：丢弃 upto_index 之前的原始消息，替换为一条压缩摘要系统消息。
            compacted_upto = int(ev.payload.get("upto_index", 0))
            summary = ev.payload.get("summary", "")
            msgs = [{"role": "system", "content": f"[compacted summary] {summary}"}] + msgs[compacted_upto:]
        elif ev.type == RunEventType.MODEL_RESPONSE and ev.payload.get("text"):
            msgs.append({"role": "assistant", "content": ev.payload["text"]})
        elif ev.type == RunEventType.TOOL_REQUESTED:
            msgs.append(
                {
                    "role": "assistant",
                    "tool_use": {"call_id": ev.payload["call_id"], "name": ev.payload["tool"], "input": ev.payload.get("input", {})},
                }
            )
        elif ev.type == RunEventType.TOOL_RESULT:
            # 工具结果优先使用 output_ref（外置引用），没有外置时才回退到内联的 output 内容。
            msgs.append(
                {"role": "tool", "call_id": ev.payload["call_id"], "content": ev.payload.get("output_ref") or ev.payload.get("output")}
            )
    return msgs


def check(log: list[RunEvent], initial_messages: list[dict[str, Any]], about_to_send: list[dict[str, Any]]) -> str:
    """Return the hash if `about_to_send` equals the rebuilt history, else raise InvariantViolation.
    （校验即将发送给模型的 about_to_send 是否与"从日志重建的历史"完全一致；一致则返回其哈希值
    （供调用方记录到 MODEL_REQUEST 事件中），不一致则抛出 InvariantViolation。）
    """
    rebuilt = rebuild_messages(log, initial_messages)
    h1, h2 = message_hash(rebuilt), message_hash(about_to_send)
    if h1 != h2:
        raise InvariantViolation(f"model-visible context not reconstructable from event log ({h1[:8]} != {h2[:8]})")
    return h2
