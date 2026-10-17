"""平台历史 ↔ Claude Code CLI 会话转录（JSONL）的结构化互转（S5 §1.2 结构化续跑，决策 D78）。

为什么要合成转录：S1 把平台历史压成一段纯文本前缀交给 CLI，工具结果里伪造的角色标记会被模型当成真实
对话轮次（S1 遗留 #13），挂起中的 SDK 查询也无法跨进程接续（#5）。本模块把平台从事件日志重建的历史
（``runtime.run.invariant.rebuild_messages`` 格式）合成为 CLI 自己的会话转录文件，CLI 以 ``--resume=<uuid>``
载入后，模型看到的是结构化的 user / assistant(tool_use) / user(tool_result) 消息，工具结果只能出现在
tool_result 块内。

续接方式（spike 实测 claude-agent-sdk 0.2.168 / CLI 2.1.301，证据见汇报与 tests/test_live.py）：
- 整段历史（含最后一条用户消息或工具结果）都写进转录，CLI 以 ``CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1`` 恢复
  时把"转录以 user 消息结尾"识别为被中断的轮次并自动续跑，不需要再经 stdin 发送任何 prompt；
- 转录以用户文本结尾时，模型收到的就是原样历史；以 tool_result 结尾时，CLI 会在同一条 user 消息里追加一段
  固定的元文本 ``CONTINUE_PROMPT``（不形成新的对话轮次），规范化时与 CLI 注入的 system-reminder 一样剔除；
- 经 stdin 流式发送 tool_result 不可行：CLI 恢复时会先为悬空的 tool_use 补一个"interrupted"错误结果，
  随后到达的 tool_result 被当作重复结果丢弃；
- ``--no-session-persistence`` 与 ``--resume`` 可以共存：CLI 读入转录，但不再写回。

转录格式是 CLI 的内部格式，因此 SDK 版本精确钉死（pyproject），并由 tests/test_transcript.py 用 SDK 公开的
``get_session_messages`` 解析合成文件、校验结构一致。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

PLATFORM_SERVER = "platform"  # 进程内 MCP server 名，决定模型看到的工具名前缀
TOOL_PREFIX = f"mcp__{PLATFORM_SERVER}__"  # Claude Code 对 MCP 工具的命名规则：mcp__<server>__<tool>
# CLI 自动续跑被中断轮次时追加在最后一条 tool_result 消息里的固定元文本（CLI 2.1.301 实测）
CONTINUE_PROMPT = "Continue from where you left off."
COMPACTED_PREFIX = "[compacted summary] "  # rebuild_messages 对上下文压缩摘要的前缀
CLI_VERSION = "2.1.301"  # 写进转录条目的 version 字段，与 SDK 0.2.168 自带 CLI 一致
_ERROR_STATUSES = frozenset({"error", "denied"})
# Anthropic / OpenAI 工具名只允许这些字符；CLI 对 MCP server 名与工具名做同样的替换（见 wire_tool_name）
_UNSAFE_TOOL_CHARS = re.compile(r"[^A-Za-z0-9_-]")
MAX_TOOL_NAME = 64  # 模型看到的完整工具名（含 mcp__platform__ 前缀）的长度上限，取 Anthropic / OpenAI 的公共下限


class TranscriptError(ValueError):
    """平台历史无法合成为合法的会话转录（例如以助手消息结尾、工具结果没有对应的调用）。"""


class ToolNameConflictError(ValueError):
    """两个平台工具名映射到同一个 CLI 工具名：无法区分模型调用的是哪一个，拒绝开启查询。"""


def wire_tool_name(name: str) -> str:
    """平台工具名 → 注册给 CLI 的 MCP 工具名（不含 ``mcp__platform__`` 前缀）。

    CLI（2.1.301 实测）构造 MCP 工具名时把 server 名与工具名中 ``[A-Za-z0-9_-]`` 以外的字符一律替换为 ``_``
    （``ticket.get`` → ``mcp__platform__ticket_get``），模型调用、权限判定与 PreToolUse 回调都用替换后的名字。
    这里预先做同样的替换，使 CLI 的规范化成为恒等；完整名超过 ``MAX_TOOL_NAME`` 时截断并追加原名哈希。
    该变换不可逆，反向一律查 ``tool_name_map`` 建立的映射表。
    """
    safe = _UNSAFE_TOOL_CHARS.sub("_", name)
    limit = MAX_TOOL_NAME - len(TOOL_PREFIX)
    if len(safe) > limit:
        safe = f"{safe[: limit - 9]}_{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"
    return safe


def tool_name_map(names: Iterable[str]) -> dict[str, str]:
    """本次查询的工具名映射表：CLI 工具名（``wire_tool_name``，不含前缀）→ 平台工具名。

    两个不同的平台名映射到同一 CLI 名时抛 ToolNameConflictError（例如同时注入 ``ticket.get`` 与 ``ticket_get``）。
    """
    out: dict[str, str] = {}
    for name in names:
        wire = wire_tool_name(name)
        if out.setdefault(wire, name) != name:
            raise ToolNameConflictError(f"platform tools {out[wire]!r} and {name!r} both map to CLI tool name {TOOL_PREFIX + wire!r}")
    return out


def _text(content: Any) -> str:
    """把消息内容转成文本：字符串原样，其余转 JSON 文本（与工具桥接交给 SDK 的结果一致）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def to_api_messages(messages: list[dict[str, Any]], statuses: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """把平台历史转换成 Anthropic Messages 格式（user / assistant 严格交替）。

    - user → user 文本；system（压缩摘要）→ user 文本块（内容原样，含 ``[compacted summary]`` 前缀）；
    - assistant 文本 → text 块；assistant tool_use → tool_use 块（工具名为 ``mcp__platform__`` + ``wire_tool_name``），
      带 reasoning 时在该助手消息开头放一个 thinking 块；
    - tool → user 消息里的 tool_result 块，内容为文本；``statuses[call_id]`` 为 denied / error 时 ``is_error=True``；
    - 相邻同侧消息合并为一条（与 CLI 发给模型的形状一致）。

    校验（失败抛 TranscriptError）：历史非空且以 user / system / tool 结尾；每个 tool 结果都对应此前的一个
    tool_use；除最后一条外每个 tool_use 都已有结果（以悬空 tool_use 结尾时说明平台没给结果）。
    """
    if not messages:
        raise TranscriptError("empty history: nothing for the model to answer")
    statuses = statuses or {}
    out: list[dict[str, Any]] = []
    open_calls: list[str] = []  # 已调用、尚无结果的 call_id
    seen_calls: set[str] = set()

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            merged = out[-1]["content"] + blocks
            if role == "assistant":  # thinking 块必须位于助手消息开头
                merged = [b for b in merged if b["type"] == "thinking"] + [b for b in merged if b["type"] != "thinking"]
            out[-1]["content"] = merged
        else:
            out.append({"role": role, "content": blocks})

    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant" and "tool_use" in m:
            tu = m.get("tool_use") or {}
            call_id = str(tu.get("call_id") or "")
            if not call_id:
                raise TranscriptError(f"message #{i}: tool_use without call_id")
            blocks: list[dict[str, Any]] = []
            if m.get("reasoning"):
                blocks.append({"type": "thinking", "thinking": str(m["reasoning"]), "signature": ""})
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": TOOL_PREFIX + wire_tool_name(str(tu.get("name"))),
                    "input": dict(tu.get("input") or {}),
                }
            )
            push("assistant", blocks)
            open_calls.append(call_id)
            seen_calls.add(call_id)
        elif role == "assistant":
            text = _text(m.get("content"))
            if text:
                push("assistant", [{"type": "text", "text": text}])
        elif role == "tool":
            call_id = str(m.get("call_id") or "")
            if call_id not in seen_calls:
                raise TranscriptError(f"message #{i}: tool result {call_id!r} has no preceding tool_use")
            if call_id in open_calls:
                open_calls.remove(call_id)
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id, "content": _text(m.get("content"))}
            if statuses.get(call_id) in _ERROR_STATUSES:
                block["is_error"] = True
            push("user", [block])
        elif role in ("user", "system"):
            if open_calls:
                raise TranscriptError(f"message #{i}: missing tool result for call(s) {', '.join(open_calls)}")
            push("user", [{"type": "text", "text": _text(m.get("content"))}])
        else:
            raise TranscriptError(f"message #{i}: unsupported role {role!r}")
    if open_calls:
        raise TranscriptError(f"missing tool result for call(s): {', '.join(open_calls)}")
    if out[-1]["role"] != "user":
        raise TranscriptError("history ends with an assistant message: nothing for the model to answer")
    # 单个纯文本块的 user 消息写成字符串内容（与 CLI 自己记录的用户输入形状一致）
    for msg in out:
        if msg["role"] == "user" and len(msg["content"]) == 1 and msg["content"][0]["type"] == "text":
            msg["content"] = msg["content"][0]["text"]
    return out


def _is_cli_meta(text: str) -> bool:
    """CLI 自行注入、不属于平台历史的文本块（续跑元文本、system-reminder）。"""
    stripped = text.strip()
    return stripped == CONTINUE_PROMPT or stripped.startswith("<system-reminder>")


def _block_text(content: Any) -> str:
    """tool_result 的内容：字符串原样；内容块列表拼接其中的 text。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text", "")) for b in content if isinstance(b, Mapping) and b.get("type") == "text")
    return _text(content)


def from_api_messages(api_messages: list[Mapping[str, Any]], names: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """把模型实际收到的 Anthropic 格式消息规范化为平台历史格式（``to_api_messages`` 的逆变换）。

    ``names`` 为 ``tool_name_map`` 形式的映射表（CLI 工具名 → 平台工具名），用于还原工具名；不在表中的原样保留。

    规则见 ``ai_harness_probe.target``：助手文本在前、tool_use 在后；产生 tool_use 的那条助手消息的思维链记在
    第一个 tool_use 的 ``reasoning`` 上；CLI 注入的元文本（``CONTINUE_PROMPT``、system-reminder）剔除；
    ``[compacted summary]`` 开头的用户文本还原为 system 消息。
    """
    out: list[dict[str, Any]] = []
    for msg in api_messages:
        role, content = msg.get("role"), msg.get("content")
        if role == "user":
            if isinstance(content, str):
                if not _is_cli_meta(content):
                    out.append({"role": "user", "content": content})
                continue
            for b in content or []:
                if b.get("type") == "tool_result":
                    out.append({"role": "tool", "call_id": str(b.get("tool_use_id")), "content": _block_text(b.get("content"))})
                elif b.get("type") == "text" and not _is_cli_meta(str(b.get("text", ""))):
                    text = str(b.get("text", ""))
                    if text.startswith(COMPACTED_PREFIX):
                        out.append({"role": "system", "content": text})
                    else:
                        out.append({"role": "user", "content": text})
        elif role == "assistant":
            if isinstance(content, str):
                if content:
                    out.append({"role": "assistant", "content": content})
                continue
            blocks = list(content or [])
            thinking = "".join(str(b.get("thinking", "")) for b in blocks if b.get("type") == "thinking")
            text = "".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
            if text:
                out.append({"role": "assistant", "content": text})
            first = True
            for b in blocks:
                if b.get("type") != "tool_use":
                    continue
                wire = str(b.get("name", "")).removeprefix(TOOL_PREFIX)
                name = (names or {}).get(wire, wire)
                item: dict[str, Any] = {
                    "role": "assistant",
                    "tool_use": {"call_id": str(b.get("id")), "name": name, "input": dict(b.get("input") or {})},
                }
                if first and thinking:
                    item["reasoning"] = thinking
                first = False
                out.append(item)
    return out


def _sanitize(path: str) -> str:
    """与 CLI / SDK ``project_key_for_directory`` 相同的项目目录名（这里只覆盖不超长的路径）。"""
    from claude_agent_sdk import project_key_for_directory

    return project_key_for_directory(path)


def transcript_path(config_dir: str | Path, cwd: str | Path, session_id: str) -> Path:
    """CLI 读取会话转录的位置：``<CLAUDE_CONFIG_DIR>/projects/<project_key_for_directory(cwd)>/<uuid>.jsonl``。"""
    return Path(config_dir) / "projects" / _sanitize(str(cwd)) / f"{session_id}.jsonl"


def transcript_entries(api_messages: list[dict[str, Any]], *, session_id: str, cwd: str, model: str) -> list[dict[str, Any]]:
    """为每条消息生成一个转录条目，以 parentUuid 串成单链（CLI 恢复时从叶子沿 parentUuid 回溯）。"""
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    entries: list[dict[str, Any]] = []
    parent: str | None = None
    for msg in api_messages:
        entry_uuid = str(uuid.uuid4())
        message: dict[str, Any]
        if msg["role"] == "assistant":
            msg_id = "msg_" + uuid.uuid4().hex[:24]
            content = [dict(b, signature=msg_id) if b["type"] == "thinking" else b for b in msg["content"]]
            message = {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": content,
                "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        else:
            message = {"role": "user", "content": msg["content"]}
        entries.append(
            {
                "parentUuid": parent,
                "isSidechain": False,
                "type": msg["role"],
                "message": message,
                "uuid": entry_uuid,
                "timestamp": now,
                "userType": "external",
                "cwd": cwd,
                "sessionId": session_id,
                "version": CLI_VERSION,
            }
        )
        parent = entry_uuid
    return entries


def write_transcript(
    config_dir: str | Path,
    cwd: str,
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    model: str,
    statuses: Mapping[str, str] | None = None,
) -> Path:
    """把平台历史合成为 CLI 会话转录并写盘，返回文件路径。历史不合法时抛 TranscriptError（不写文件）。"""
    api = to_api_messages(messages, statuses)
    path = transcript_path(config_dir, cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = (json.dumps(e, ensure_ascii=False) + "\n" for e in transcript_entries(api, session_id=session_id, cwd=cwd, model=model))
    path.write_text("".join(lines), encoding="utf-8")
    return path


def load_transcript(path: str | Path) -> list[dict[str, Any]]:
    """读回转录中的对话消息（Anthropic 格式），按 CLI 的方式从最后一个 user/assistant 条目沿 parentUuid 回溯。"""
    entries: dict[str, dict[str, Any]] = {}
    last: str | None = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if isinstance(entry, dict) and entry.get("type") in ("user", "assistant") and isinstance(entry.get("uuid"), str):
            entries[entry["uuid"]] = entry
            last = entry["uuid"]
    chain: list[dict[str, Any]] = []
    cur = entries.get(last) if last else None
    while cur is not None:
        chain.append(cur)
        parent = cur.get("parentUuid")
        cur = entries.get(parent) if parent else None
    chain.reverse()
    return [{"role": e["message"]["role"], "content": e["message"]["content"]} for e in chain]
