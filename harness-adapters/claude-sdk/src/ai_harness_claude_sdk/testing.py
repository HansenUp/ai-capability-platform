"""进程内测试模式（S5 §1.2，决策 D80）：真实 SDK 查询层 + MCP 桥 + hook 回调，只有 CLI/模型是假的。

``ScriptedCliTransport`` 实现 SDK 的 ``Transport``（内部接口，因此 SDK 版本精确钉死），在 stdin/stdout 的
stream-json 协议层扮演 Claude Code CLI：
- 应答 ``initialize``（记录 SDK 注册的 hook 回调）、``interrupt`` 与其他 control 请求；
- 按 ``--resume`` 载入适配器合成的会话转录（``CLAUDE_CONFIG_DIR/projects/<key>/<uuid>.jsonl``），与真实 CLI
  一样在 ``CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1`` 时自动续跑（以 tool_result 结尾时追加 ``CONTINUE_PROMPT``）；
- 经 control 协议向 SDK 发 ``mcp_message``（initialize / tools/list / tools/call）调用适配器注册的进程内 MCP
  server，发 ``hook_callback`` 执行 PreToolUse 回调，并按 ``permission_mode=dontAsk`` + ``allowed_tools`` 判权限；
  与真实 CLI 一样把 MCP server 名与工具名中 ``[A-Za-z0-9_-]`` 以外的字符替换为 ``_`` 构成 ``mcp__<server>__<tool>``
  （模型、权限与 hook 看到的都是替换后的名字，tools/call 用 MCP server 列出的原名）；
- 每次"模型调用"按 ``ai_harness_probe.ModelScript`` 应答：message_start → 思维/文本增量 → 完整 assistant 消息
  （每个内容块一条，与 CLI 相同）→ 工具调用 → message_delta（工具轮的 message_delta 在工具调用发起后才到达，
  复现真实 CLI 的时序）→ result；并把这次调用实际看到的消息规范化后记入 ``received``。

``claude_sdk_probe_factory`` 符合 ``ai_harness_probe.ProbeFactory``，供 ``run_probes`` 与单元测试使用。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, Transport

from ai_harness_probe import ModelReply, ModelScript, NormalizedMessages, ScriptExhausted

from .harness import ClaudeSdkConfig, ClaudeSdkHarness
from .transcript import CONTINUE_PROMPT, from_api_messages, load_transcript, transcript_path

_CLI_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def cli_name(name: str) -> str:
    """CLI（2.1.301 bundled 实测）对 MCP server 名 / 工具名的规范化：``[a-zA-Z0-9_-]`` 以外的字符一律替换为 ``_``。"""
    return _CLI_UNSAFE.sub("_", name)


if TYPE_CHECKING:
    from ai_contract import HarnessAdapter

__all__ = ["ScriptedCliTransport", "ScriptedModel", "ScriptedProbeTarget", "claude_sdk_probe_factory", "probe_factory"]

_BUILTIN_TOOLS = ("Bash", "Read", "Edit", "Write", "Task", "WebFetch")  # options.tools 未指定时 CLI 默认带的内置工具（示意）
_CLOSE_WAIT_S = 2.0


class ScriptedModel:
    """按脚本应答的假模型：调用计数与收到的消息跨适配器重启保留（同一 ProbeTarget 内共享）。"""

    def __init__(self, script: ModelScript) -> None:
        self.script = script
        self.calls = 0
        self.received: list[NormalizedMessages] = []  # 规范化后的历史（与 rebuild_messages 同格式）
        self.raw: list[list[dict[str, Any]]] = []  # 原样的 Anthropic 格式消息（含 CLI 元文本），便于排查
        # 规范化收到的消息时还原工具名：CLI 工具名 → 脚本里的平台工具名
        self.names = {cli_name(c.name): c.name for r in script.replies for c in r.tool_calls}

    def call(self, conversation: list[dict[str, Any]]) -> ModelReply:
        """记录本次调用看到的消息并返回脚本应答；超出脚本抛 ScriptExhausted。"""
        snapshot = json.loads(json.dumps(conversation))
        self.raw.append(snapshot)
        self.received.append(from_api_messages(snapshot, self.names))
        reply = self.script.reply_for(self.calls)
        self.calls += 1
        return reply


def _tool_name(name: str, server: str) -> str:
    """脚本里的工具名 → CLI 工具名：已是完整名（mcp__ 开头）或内置工具（首字母大写，如 Bash）原样，其余视为平台工具，
    按 CLI 规则规范化（模型只能调用 CLI 列出的名字）。"""
    if name.startswith("mcp__") or name[:1].isupper():
        return name
    return f"mcp__{cli_name(server)}__{cli_name(name)}"


def _matches(matcher: Any, tool_name: str) -> bool:
    """CLI hook matcher 语义：空 / ``*`` 匹配全部，否则按正则全匹配（``A|B`` 亦可）。"""
    if matcher in (None, "", "*"):
        return True
    return re.fullmatch(str(matcher), tool_name) is not None


class ScriptedCliTransport(Transport):
    """扮演 Claude Code CLI 的 SDK Transport（只有模型是脚本化的；SDK 查询层、MCP 桥、hook 回调都是真实的）。"""

    def __init__(self, options: ClaudeAgentOptions, model: ScriptedModel, *, delta_delay_s: float = 0.05) -> None:
        self.options = options
        self.model = model
        self.delta_delay_s = delta_delay_s
        env = dict(options.env or {})
        self.config_dir = env.get("CLAUDE_CONFIG_DIR", "")
        self.cwd = str(options.cwd or "")
        self.session_id = options.resume or str(uuid.uuid4())
        self._auto_continue = env.get("CLAUDE_CODE_RESUME_INTERRUPTED_TURN") == "1"
        self.conversation: list[dict[str, Any]] = []  # 发给模型的消息（Anthropic 格式）
        self.hooks: dict[str, list[dict[str, Any]]] = {}  # initialize 时 SDK 注册的 hook 回调
        self.hook_log: list[tuple[str, str, str | None]] = []  # (hook 事件, 工具名, permissionDecision)
        self.tool_log: list[tuple[str, dict[str, Any], str, bool]] = []  # (工具名, 入参, 结果文本, is_error)
        self.interrupted = False
        self.closed = False
        self._out: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._req = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._turn: asyncio.Task[None] | None = None
        self._ready = False
        servers = options.mcp_servers if isinstance(options.mcp_servers, dict) else {}
        self._sdk_servers = [n for n, c in servers.items() if isinstance(c, dict) and c.get("type") == "sdk"]
        self._mcp_tools: dict[str, tuple[str, str]] = {}  # CLI 工具名 -> (MCP server 名, server 列出的原工具名)
        self._turns = 0  # 本次 agent 循环已发起的模型调用数
        self._usage_total: dict[str, int] = {}

    # ---- Transport 接口 ----

    async def connect(self) -> None:
        if self.options.resume:
            path = transcript_path(self.config_dir, self.cwd, self.options.resume)
            if not path.is_file():
                raise RuntimeError(f"No conversation found with session ID: {self.options.resume}")
            self.conversation = load_transcript(path)
        self._ready = True

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if line.strip():
                await self._on_input(json.loads(line))

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            msg = await self._out.get()
            if msg is None:
                return
            yield msg

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._ready = False
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=_CLOSE_WAIT_S)
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._out.put_nowait(None)

    def is_ready(self) -> bool:
        return self._ready and not self.closed

    async def end_input(self) -> None:
        return None

    def alive_tasks(self) -> list[asyncio.Task[Any]]:
        """尚未结束的内部任务（泄漏检查用）。"""
        return [t for t in self._tasks if not t.done()]

    # ---- stdin 输入（SDK → CLI） ----

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _emit(self, msg: dict[str, Any]) -> None:
        if not self.closed:
            self._out.put_nowait(msg)

    def _respond(self, request_id: str, payload: dict[str, Any]) -> None:
        self._emit({"type": "control_response", "response": {"subtype": "success", "request_id": request_id, "response": payload}})

    async def _on_input(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "control_request":
            rid, req = msg["request_id"], msg.get("request") or {}
            subtype = req.get("subtype")
            if subtype == "initialize":
                self.hooks = req.get("hooks") or {}
                self._respond(rid, {"commands": [], "models": [], "account": {}})
                self._spawn(self._boot())
            elif subtype == "interrupt":
                self.interrupted = True
                await self._stop_turn()
                self._respond(rid, {})
            else:
                self._respond(rid, {})
        elif mtype == "control_response":
            resp = msg.get("response") or {}
            fut = self._pending.pop(str(resp.get("request_id")), None)
            if fut is not None and not fut.done():
                if resp.get("subtype") == "error":
                    fut.set_exception(RuntimeError(str(resp.get("error"))))
                else:
                    fut.set_result(resp.get("response") or {})
        elif mtype == "user":
            self.conversation.append({"role": "user", "content": msg["message"]["content"]})
            self._start_turn()

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """CLI → SDK 的 control 请求（mcp_message / hook_callback），等待 SDK 的 control_response。
        被取消时（中断）照真实 CLI 发 control_cancel_request。"""
        self._req += 1
        rid = f"cli_req_{self._req}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._emit({"type": "control_request", "request_id": rid, "request": payload})
        try:
            return await fut
        except asyncio.CancelledError:
            self._pending.pop(rid, None)
            self._emit({"type": "control_cancel_request", "request_id": rid})
            raise

    async def _mcp(self, server: str, method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._req += 1
            message["id"] = self._req
        resp = await self._request({"subtype": "mcp_message", "server_name": server, "message": message})
        return dict(resp.get("mcp_response") or {})

    # ---- 会话启动与轮次 ----

    async def _boot(self) -> None:
        """连接进程内 MCP server、列出工具、发 system/init；转录以 user 消息结尾且开启自动续跑时开始一轮。"""
        tools = self.options.tools
        names: list[str] = list(tools) if isinstance(tools, list) else list(_BUILTIN_TOOLS)
        for server in self._sdk_servers:
            init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "scripted-cli", "version": "0"}}
            await self._mcp(server, "initialize", init)
            await self._mcp(server, "notifications/initialized", notify=True)
            listed = await self._mcp(server, "tools/list")
            for t in (listed.get("result") or {}).get("tools", []):
                full = f"mcp__{cli_name(server)}__{cli_name(t['name'])}"
                self._mcp_tools[full] = (server, t["name"])
                names.append(full)
        self._emit(
            {
                "type": "system",
                "subtype": "init",
                "cwd": self.cwd,
                "session_id": self.session_id,
                "tools": names,
                "mcp_servers": [{"name": n, "status": "connected", "source": "sdk"} for n in self._sdk_servers],
                "model": self.options.model,
                "permissionMode": self.options.permission_mode,
                "uuid": str(uuid.uuid4()),
            }
        )
        if self._auto_continue and self.conversation and self.conversation[-1]["role"] == "user":
            last = self.conversation[-1]
            if isinstance(last["content"], list) and any(b.get("type") == "tool_result" for b in last["content"]):
                last["content"] = [*last["content"], {"type": "text", "text": CONTINUE_PROMPT}]
            self._start_turn()

    def _start_turn(self) -> None:
        if self._turn is None or self._turn.done():
            self._turn = self._spawn(self._run_turn())

    async def _stop_turn(self) -> None:
        turn = self._turn
        if turn is not None and not turn.done():
            turn.cancel()
            await asyncio.wait({turn}, timeout=_CLOSE_WAIT_S)
            self._result("error_during_execution", is_error=True, errors=["interrupted"])

    def _stream(self, event: dict[str, Any]) -> None:
        self._emit(
            {"type": "stream_event", "uuid": str(uuid.uuid4()), "session_id": self.session_id, "event": event, "parent_tool_use_id": None}
        )

    def _assistant(self, mid: str, block: dict[str, Any], usage: dict[str, int]) -> None:
        message = {"id": mid, "type": "message", "role": "assistant", "model": self.options.model, "content": [block], "usage": usage}
        self._emit(
            {"type": "assistant", "message": message, "parent_tool_use_id": None, "session_id": self.session_id, "uuid": str(uuid.uuid4())}
        )

    def _result(self, subtype: str, *, is_error: bool = False, errors: list[str] | None = None, text: str | None = None) -> None:
        self._emit(
            {
                "type": "result",
                "subtype": subtype,
                "duration_ms": 1,
                "duration_api_ms": 1,
                "is_error": is_error,
                "num_turns": self._turns,
                "session_id": self.session_id,
                "stop_reason": None if is_error else "end_turn",
                "total_cost_usd": 0.0,
                "usage": dict(self._usage_total),
                "result": text,
                "errors": errors,
                "uuid": str(uuid.uuid4()),
            }
        )

    async def _run_turn(self) -> None:
        """CLI 的 agent 循环：模型调用 → 工具（hook → 权限 → MCP）→ 再次模型调用，直到不再调用工具。"""
        self._turns = 0
        self._usage_total = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}
        last_text = ""
        while True:
            if self._turns >= int(self.options.max_turns or 1_000_000):
                self._result("error_max_turns", is_error=True, errors=[f"max turns {self.options.max_turns}"])
                return
            self._turns += 1
            try:
                reply = self.model.call(self.conversation)
            except ScriptExhausted as e:
                self._result("error_during_execution", is_error=True, errors=[str(e)])
                return
            mid = "msg_" + uuid.uuid4().hex[:20]
            u = reply.usage
            start_usage = {
                "input_tokens": max(u.input_tokens - u.cache_read_tokens, 0),
                "cache_read_input_tokens": u.cache_read_tokens,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
            }
            for k in ("input_tokens", "cache_read_input_tokens"):
                self._usage_total[k] += start_usage[k]
            self._usage_total["output_tokens"] += u.output_tokens
            message_start: dict[str, Any] = {
                "id": mid,
                "type": "message",
                "role": "assistant",
                "model": self.options.model,
                "content": [],
                "usage": start_usage,
            }
            self._stream({"type": "message_start", "message": message_start})
            blocks: list[dict[str, Any]] = []
            idx = 0
            if reply.thinking:
                self._stream({"type": "content_block_start", "index": idx, "content_block": {"type": "thinking", "thinking": ""}})
                for piece in reply.thinking:
                    self._stream({"type": "content_block_delta", "index": idx, "delta": {"type": "thinking_delta", "thinking": piece}})
                self._stream({"type": "content_block_stop", "index": idx})
                block: dict[str, Any] = {"type": "thinking", "thinking": reply.full_thinking, "signature": mid}
                self._assistant(mid, block, start_usage)
                blocks.append(block)
                idx += 1
            if reply.text:
                self._stream({"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})
                for piece in reply.text:
                    self._stream({"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": piece}})
                if reply.hang_after_text:
                    await asyncio.Event().wait()  # 流式输出中挂起，直到被 interrupt 取消
                self._stream({"type": "content_block_stop", "index": idx})
                block = {"type": "text", "text": reply.full_text}
                self._assistant(mid, block, start_usage)
                blocks.append(block)
                last_text = reply.full_text
                idx += 1
            calls = []
            for tc in reply.tool_calls:
                name = _tool_name(tc.name, self._sdk_servers[0] if self._sdk_servers else "platform")
                block = {"type": "tool_use", "id": tc.call_id, "name": name, "input": dict(tc.input)}
                self._stream({"type": "content_block_start", "index": idx, "content_block": dict(block, input={})})
                self._stream({"type": "content_block_stop", "index": idx})
                self._assistant(mid, block, start_usage)
                blocks.append(block)
                calls.append(block)
                idx += 1
            self.conversation.append({"role": "assistant", "content": blocks})
            final_delta = {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use" if calls else "end_turn"},
                "usage": {"output_tokens": u.output_tokens},
            }
            if not calls:
                self._stream(final_delta)
                self._stream({"type": "message_stop"})
                self._result("success", text=last_text)
                return
            # 真实 CLI 在内容块结束时就发起工具调用，message_delta 晚于工具调用到达：这里延迟发出。
            delta_task = self._spawn(self._late_delta(final_delta))
            results = []
            for call in calls:  # 非只读 MCP 工具按顺序串行执行（与 CLI 相同）
                text, is_error = await self._call_tool(call)
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": text,
                    **({"is_error": True} if is_error else {}),
                }
                results.append(result_block)
                content = [dict(result_block, content=[{"type": "text", "text": text}])]
                self._emit(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": content},
                        "parent_tool_use_id": None,
                        "session_id": self.session_id,
                        "uuid": str(uuid.uuid4()),
                    }
                )
            await delta_task
            self.conversation.append({"role": "user", "content": results})

    async def _late_delta(self, delta: dict[str, Any]) -> None:
        await asyncio.sleep(self.delta_delay_s)
        self._stream(delta)
        self._stream({"type": "message_stop"})

    async def _call_tool(self, call: dict[str, Any]) -> tuple[str, bool]:
        """执行一次工具调用：PreToolUse hooks（经 SDK 回调）→ dontAsk 权限 → 进程内 MCP tools/call。"""
        name, args = str(call["name"]), dict(call["input"])
        for matcher in self.hooks.get("PreToolUse") or []:
            if not _matches(matcher.get("matcher"), name):
                continue
            for cid in matcher.get("hookCallbackIds") or []:
                hook_input = {
                    "session_id": self.session_id,
                    "transcript_path": "",
                    "cwd": self.cwd,
                    "hook_event_name": "PreToolUse",
                    "tool_name": name,
                    "tool_input": args,
                    "tool_use_id": call["id"],
                }
                out = await self._request({"subtype": "hook_callback", "callback_id": cid, "input": hook_input, "tool_use_id": call["id"]})
                specific = out.get("hookSpecificOutput") or {}
                decision = specific.get("permissionDecision")
                self.hook_log.append(("PreToolUse", name, decision))
                if decision == "deny":
                    return self._log_tool(
                        name, args, f"Hook PreToolUse denied this tool: {specific.get('permissionDecisionReason', '')}", True
                    )
        allowed = list(self.options.allowed_tools or [])
        if self.options.permission_mode == "dontAsk" and name not in allowed:
            return self._log_tool(name, args, f"Permission to use {name} has been denied.", True)
        target = self._mcp_tools.get(name)
        if target is None:
            return self._log_tool(name, args, f"No such tool available: {name}", True)
        resp = await self._mcp(target[0], "tools/call", {"name": target[1], "arguments": args})
        if "error" in resp:
            return self._log_tool(name, args, str(resp["error"].get("message")), True)
        result = resp.get("result") or {}
        text = "".join(str(c.get("text", "")) for c in result.get("content") or [] if c.get("type") == "text")
        return self._log_tool(name, args, text, bool(result.get("isError")))

    def _log_tool(self, name: str, args: dict[str, Any], text: str, is_error: bool) -> tuple[str, bool]:
        self.tool_log.append((name, args, text, is_error))
        return text, is_error


_PROBE_CONFIG = ClaudeSdkConfig(base_url="http://scripted.invalid", auth_token="scripted", cli_path=None, max_turns=16, turn_timeout_s=30.0)  # noqa: S106


class ScriptedProbeTarget:
    """``ai_harness_probe.ProbeTarget`` 的 claude-sdk 实现：真实适配器 + 真实 ClaudeSDKClient + ScriptedCliTransport。"""

    def __init__(self, script: ModelScript, *, config: ClaudeSdkConfig = _PROBE_CONFIG, delta_delay_s: float = 0.05) -> None:
        self.model = ScriptedModel(script)
        self.config = config
        self.delta_delay_s = delta_delay_s
        self.transports: list[ScriptedCliTransport] = []
        self.adapters: list[ClaudeSdkHarness] = []
        self._adapter = self._new_adapter()

    def _new_adapter(self) -> ClaudeSdkHarness:
        adapter = ClaudeSdkHarness(client_factory=self._client, config=self.config)
        self.adapters.append(adapter)
        return adapter

    def _client(self, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        transport = ScriptedCliTransport(options, self.model, delta_delay_s=self.delta_delay_s)
        self.transports.append(transport)
        return ClaudeSDKClient(options=options, transport=transport)

    @property
    def adapter(self) -> HarnessAdapter:
        return self._adapter

    @property
    def harness(self) -> ClaudeSdkHarness:
        """当前适配器的具体类型（测试检查内部状态用）。"""
        return self._adapter

    def received(self) -> list[NormalizedMessages]:
        return [list(m) for m in self.model.received]

    def leaks(self) -> list[str]:
        """残留资源：未拆除的会话、未结束的泵任务、未关闭的 CLI 传输层及其内部任务、未删除的临时目录。"""
        out: list[str] = []
        live_cwds: set[str] = set()
        for adapter in self.adapters:
            for sid, s in adapter._sessions.items():
                out.append(f"open session {sid} (adapter #{self.adapters.index(adapter)})")
                live_cwds.add(s.cwd)
                if s.task is not None and not s.task.done():
                    out.append(f"pump task of session {sid} still running")
        for i, t in enumerate(self.transports):
            if not t.closed:
                out.append(f"CLI transport #{i} (session {t.session_id}) not closed")
            if t.alive_tasks():
                out.append(f"CLI transport #{i} has {len(t.alive_tasks())} running task(s)")
            if t.config_dir and Path(t.config_dir).exists():
                out.append(f"config dir {t.config_dir} not removed")
            if Path(t.cwd).name.startswith("claude-sdk-") and t.cwd not in live_cwds and Path(t.cwd).exists():
                out.append(f"temporary cwd {t.cwd} not removed")
        return out

    async def restart(self) -> HarnessAdapter:
        """模拟平台进程重启：旧实例的全部会话随进程消失（拆除子进程与目录），换一个全新实例。"""
        await self._shutdown(self._adapter)
        self._adapter = self._new_adapter()
        return self._adapter

    async def aclose(self) -> None:
        for adapter in self.adapters:
            await self._shutdown(adapter)

    @staticmethod
    async def _shutdown(adapter: ClaudeSdkHarness) -> None:
        for s in list(adapter._sessions.values()):
            with contextlib.suppress(Exception):
                await adapter._teardown(s, "cancelled")


@asynccontextmanager
async def claude_sdk_probe_factory(script: ModelScript) -> AsyncIterator[ScriptedProbeTarget]:
    """``ai_harness_probe.ProbeFactory``：进入时得到 ScriptedProbeTarget，退出时释放全部资源。"""
    target = ScriptedProbeTarget(script)
    try:
        yield target
    finally:
        await target.aclose()


# runtime harness registry（registry.KNOWN）与探针门禁按 "ai_harness_claude_sdk.testing:probe_factory" 加载
probe_factory = claude_sdk_probe_factory
