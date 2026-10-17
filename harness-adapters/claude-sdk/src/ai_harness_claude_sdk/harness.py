"""Claude Agent SDK Harness 适配器的 S1 最小实现（S1.3 真实对话 + S1.4 工具桥接）。

整体结构：每个会话一个后台"泵"任务（pump task），在**同一个 asyncio 任务里**完成 SDK client 的
connect → query → receive_response → disconnect 全生命周期（SDK 要求 client 不跨 async 上下文使用）。
泵把 SDK 消息翻译成统一 Event 放进本会话的出事件队列；`run_turn` 只负责从队列取事件并 yield。

工具桥接（平台执行，SDK 只拿结果）：
- 平台工具以进程内 MCP server（名为 ``platform``）注册给 SDK，模型看到的名字是 ``mcp__platform__<name>``；
- 工具 handler 不执行任何业务逻辑：它认领 SDK 的 tool_use id 作为 call_id，把 ``ToolUseEvent`` 放入队列，
  然后挂起等待一个 ``asyncio.Future``；
- ``run_turn`` 取到 ToolUseEvent 后紧接产出 ``UsageEvent`` 与 ``DoneEvent(stop_reason="tool_use")`` 并返回，
  此时 SDK 查询仍在后台等 Future；
- 平台（runner + toolbroker）完成闸口判定与真正执行后，把 ``{"role": "tool", "call_id", "content"}``
  追加进历史并再次调用 ``run_turn``；本适配器据此解析对应 Future，SDK 拿到结果继续推进。

因此闸口与执行都在平台侧、harness 无法绕过：SDK 的内置工具全部禁用（``tools=[]``）、只放行
``mcp__platform__*``、不加载任何本机设置/插件/外部 MCP（``setting_sources=[]`` + ``strict_mcp_config``），
平台给出的拒绝内容会原样成为 SDK 侧的工具结果——这是 ``tool_interception="enforcing"`` 的依据，
由 tests/test_harness.py 中的拦截探针（probe）测试证明。此外在运行期断言隔离（失败关闭）：CLI 的
``system/init`` 消息若报告了任何非 ``mcp__platform__`` 工具或 ``platform`` 以外的 MCP server，立即以
``HARNESS_ISOLATION_VIOLATION`` 结束查询。

会话资源：每个会话一个独立的临时 ``CLAUDE_CONFIG_DIR``（不落在 Run 工作区、不读写本机 ~/.claude），并以
``--no-session-persistence`` + ``CLAUDE_CODE_SKIP_PROMPT_HISTORY`` 关闭 CLI 的转录与输入历史落盘；会话结束
（查询结束 / 中断 / 生成器被提前关闭）时删除该目录，以及适配器自建的临时 cwd（调用方给定的 workspace_dir 不删）。

子进程环境：SDK 会把 os.environ 整体继承给 CLI，适配器只能把已知敏感变量置空（见 ``_build_env``），无法
做到白名单。生产环境建议经 ``CLAUDE_SDK_CLI_PATH`` 指向一个以最小环境（``env -i`` 之类）启动 CLI 的包装脚本。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ai_contract import (
    DoneEvent,
    ErrorEvent,
    Event,
    HarnessCapabilities,
    HookSet,
    SessionHandle,
    SessionSpec,
    SkillDir,
    TextEvent,
    ThinkingEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions, Message, ResultMessage, SdkMcpTool

PLATFORM_SERVER = "platform"  # 进程内 MCP server 名，决定模型看到的工具名前缀
TOOL_PREFIX = f"mcp__{PLATFORM_SERVER}__"  # Claude Code 对 MCP 工具的命名规则：mcp__<server>__<tool>
# 从 SDK usage 中累计的字段。Anthropic 的 input_tokens 不含缓存命中 / 写入部分，上报 UsageEvent 时三者相加，
# 与 Contract 的口径一致（input_tokens 为全部输入，cache_read_tokens 是其中命中缓存的部分）。
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
# 从本进程继承来的、会改变 CLI 行为的环境变量前缀：子进程里一律置空，避免宿主环境（例如在 Claude Code
# 里跑测试时的 ANTHROPIC_BASE_URL / CLAUDE_AGENT_SDK_MCP_NO_PREFIX / OAuth token）泄漏进隔离会话。
_SCRUB_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "AWS_", "GOOGLE_", "AZURE_", "VAULT_")
# 运行时已知的敏感变量（数据库、网关/厂商凭据、可观测性密钥）：不论宿主是否设置，子进程里一律置空。
# 网关凭据只以 ANTHROPIC_AUTH_TOKEN 显式传入一份，其来源变量本身不再透传。
_SCRUB_KEYS = (
    "DATABASE_URL",
    "DATABASE_URL_FILE",
    "RUNTIME_TEST_DATABASE_URL",
    "MODEL_GATEWAY_API_KEY",
    "MODEL_GATEWAY_API_KEY_FILE",
    "CLAUDE_SDK_AUTH_TOKEN",
    "CLAUDE_SDK_AUTH_TOKEN_FILE",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_OTEL_AUTH",
    "GATEWAY_CLIENT_SECRET",
    "LITELLM_MASTER_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "COHERE_API_KEY",
)
_DEFAULT_GATEWAY_URL = "http://localhost:4000"
_DEFAULT_GATEWAY_KEY = "sk-dev-master"  # noqa: S105 —— 本地 compose LiteLLM 的公开开发 key，与 runtime 默认值一致
_INTERRUPT_WAIT_S = 5.0  # 调 SDK interrupt 的最长等待
_DISCONNECT_WAIT_S = 10.0  # 断开 SDK client 的最长等待
_ANON_STREAM = "__anon__"  # 流式 message_start 未带 id 时的占位键


class HarnessInterruptedError(Exception):
    """会话被中断/放弃时，用来结束挂起中的工具 Future；SDK 会把它当作工具错误结果回给模型。"""


class SdkClient(Protocol):
    """适配器实际用到的 SDK client 子集；生产用 ``ClaudeSDKClient``，单元测试注入伪客户端。"""

    async def connect(self) -> None: ...

    async def query(self, prompt: str) -> None: ...

    def receive_response(self) -> AsyncIterator[Message]: ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...


ClientFactory = Callable[["ClaudeAgentOptions"], SdkClient]


def _default_client_factory(options: ClaudeAgentOptions) -> SdkClient:
    """默认工厂：真实的 ClaudeSDKClient（以子进程方式运行 Claude Code CLI）。"""
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options=options)


@dataclass(frozen=True)
class ClaudeSdkConfig:
    """适配器配置，全部来自环境变量（Runtime registry 以无参方式构造适配器）。

    - CLAUDE_SDK_BASE_URL：Anthropic Messages 兼容端点根地址，缺省取 MODEL_GATEWAY_URL，再缺省
      http://localhost:4000（compose 中的 LiteLLM）；末尾的 /v1 会被去掉，因为 CLI 自己拼 /v1/messages；
    - CLAUDE_SDK_AUTH_TOKEN：发给端点的凭据（CLI 以 ANTHROPIC_AUTH_TOKEN 发送），缺省取
      MODEL_GATEWAY_API_KEY，再缺省 sk-dev-master；
    - CLAUDE_SDK_CLI_PATH：可选，指定 Claude Code CLI；缺省用 SDK 自带的 bundled CLI，
      版本随 SDK 锁定，不受本机全局安装的 claude 升级影响；
    - CLAUDE_SDK_MAX_TURNS：SDK 内部 agent 循环的最大轮数，缺省 16；
    - CLAUDE_SDK_TURN_TIMEOUT_S：单次 run_turn 等待下一个事件、以及工具 handler 等待平台结果的
      最长秒数，缺省 300。
    """

    base_url: str
    auth_token: str
    cli_path: str | None
    max_turns: int
    turn_timeout_s: float

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ClaudeSdkConfig:
        """从环境变量读取配置。"""
        e = os.environ if env is None else env
        base = (e.get("CLAUDE_SDK_BASE_URL") or e.get("MODEL_GATEWAY_URL") or _DEFAULT_GATEWAY_URL).rstrip("/")
        base = base.removesuffix("/v1")
        return cls(
            base_url=base,
            auth_token=e.get("CLAUDE_SDK_AUTH_TOKEN") or e.get("MODEL_GATEWAY_API_KEY") or _DEFAULT_GATEWAY_KEY,
            cli_path=e.get("CLAUDE_SDK_CLI_PATH") or None,
            max_turns=int(e.get("CLAUDE_SDK_MAX_TURNS") or 16),
            turn_timeout_s=float(e.get("CLAUDE_SDK_TURN_TIMEOUT_S") or 300),
        )


@dataclass(frozen=True)
class _End:
    """队列中的轮次结束标记（只在 SDK 查询整体结束 / 出错 / 被中断时出现）。"""

    stop_reason: Literal["end_turn", "max_tokens", "error", "cancelled"]


@dataclass
class _Session:
    """单个会话的全部运行期状态。"""

    session_id: str
    spec: SessionSpec
    cwd: str
    owns_cwd: bool  # cwd 是否为适配器自建的临时目录（会话结束时删除；调用方给定的 workspace_dir 不删）
    config_dir: str  # 本会话独立的 CLAUDE_CONFIG_DIR，会话结束时删除
    queue: asyncio.Queue[Event | _End] = field(default_factory=asyncio.Queue)  # 出事件队列
    pending: dict[str, asyncio.Future[str]] = field(default_factory=dict)  # call_id -> 等平台结果的 Future
    awaiting: set[str] = field(default_factory=set)  # 已 yield 出 ToolUseEvent、下一轮应带回结果的 call_id
    # SDK AssistantMessage 中出现、尚未被 handler 认领的工具调用块：(SDK tool_use id, 平台工具名, 入参)
    blocks: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    # 等待超时而自生成了 call_id 的调用：平台工具名 -> 个数。对应的 ToolUseBlock 迟到时直接丢弃，
    # 避免它被下一次同名调用错误认领。
    skipped: dict[str, int] = field(default_factory=dict)
    block_cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task[None] | None = None  # 后台泵任务
    client: SdkClient | None = None
    seen_usage: dict[str, dict[str, int]] = field(default_factory=dict)  # message_id -> 该次 API 调用的用量
    reported: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_USAGE_KEYS, 0))  # 已随 UsageEvent 上报的量
    streamed: set[str] = field(default_factory=set)  # 已通过流式增量产出过文本/思维的 message_id
    stream_msg_id: str | None = None  # 当前流式消息的 id
    last_model: str | None = None  # SDK 回报的实际模型名
    last_error: str | None = None  # AssistantMessage.error（鉴权失败、限流等）
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=20))  # CLI stderr 尾部，用于错误信息


def _stringify(content: Any) -> str:
    """把平台的工具结果内容转成交给 SDK 的文本：字符串原样，其余转 JSON 文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _render_message(m: dict[str, Any]) -> str:
    """把一条平台历史消息渲染成一行可读文本（用于拼接历史前缀）。"""
    role = str(m.get("role", "unknown"))
    if "tool_use" in m:
        tu = m["tool_use"] or {}
        return f"assistant: [调用工具 {tu.get('name')} call_id={tu.get('call_id')} 入参={_stringify(tu.get('input'))}]"
    if role == "tool":
        return f"tool[{m.get('call_id')}]: {_stringify(m.get('content'))}"
    return f"{role}: {_stringify(m.get('content'))}"


def build_prompt(messages: list[dict[str, Any]]) -> str:
    """构造新 SDK 查询的首轮 prompt（S1 最小实现：把平台历史压成一段文本）。

    规则：以最后一条 ``role == "user"`` 消息为本轮 prompt 主体；其之前的多轮内容渲染成
    ``[对话历史]`` 前缀拼在前面；其之后已发生的工具调用与结果（续跑/审批后重放的场景）渲染成
    ``[本轮已发生的工具调用与结果]`` 拼在后面，保证模型至少看得到完整上下文、不会重复决策已执行的调用。
    原生 SDK resume（直接接续 CLI 会话转录）留到 S5 再评估。
    """
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    if last_user < 0:
        return "\n".join(_render_message(m) for m in messages)
    head, tail = messages[:last_user], messages[last_user + 1 :]
    parts: list[str] = []
    if head:
        parts.append("[对话历史]\n" + "\n".join(_render_message(m) for m in head))
    current = _stringify(messages[last_user].get("content"))
    parts.append(f"[当前用户消息]\n{current}" if head or tail else current)
    if tail:
        parts.append("[本轮已发生的工具调用与结果]\n" + "\n".join(_render_message(m) for m in tail))
    return "\n\n".join(parts)


def _input_schema(spec: ToolSpec) -> dict[str, Any]:
    """规范化工具入参 schema：SDK 只有在 schema 同时含 type 与 properties 时才按 JSON Schema 原样透传，
    否则会把顶层键误当成参数名，所以这里补齐两者。"""
    return {"type": "object", "properties": {}, **(spec.input_schema or {})}


def _isolation_violation(init: Mapping[str, Any]) -> str | None:
    """检查 CLI ``system/init`` 消息（SDK ``SystemInitData``）报告的工具与 MCP server 是否越出平台桥接。

    ``tools`` 为工具名字符串列表，``mcp_servers`` 为 ``{"name", "status", ...}`` 列表。只要出现非
    ``mcp__platform__`` 前缀的工具、或 ``platform`` 以外的 server，就返回违规描述；否则返回 None。
    """
    tools = [str(t) for t in init.get("tools") or []]
    servers = [str(m.get("name") if isinstance(m, Mapping) else m) for m in init.get("mcp_servers") or []]
    bad_tools = sorted(t for t in tools if not t.startswith(TOOL_PREFIX))
    bad_servers = sorted(n for n in servers if n != PLATFORM_SERVER)
    problems = []
    if bad_tools:
        problems.append(f"unexpected tools {bad_tools}")
    if bad_servers:
        problems.append(f"unexpected mcp servers {bad_servers}")
    return "; ".join(problems) or None


def _prepare_dirs(workspace_dir: str | None) -> tuple[str, bool, str]:
    """准备 CLI 工作目录与配置目录，返回 (cwd, cwd 是否自建, config_dir)。

    工作目录：指定了就确保存在，未指定则新建临时目录（绝不落到 runtime 进程自身的 cwd）；
    配置目录：每个会话独立的临时目录，不放在 Run 工作区内，避免 CLI 的状态文件混进用户工作区。
    """
    if workspace_dir:
        Path(workspace_dir).mkdir(parents=True, exist_ok=True)
        cwd, owns = workspace_dir, False
    else:
        cwd, owns = tempfile.mkdtemp(prefix="claude-sdk-"), True
    return cwd, owns, tempfile.mkdtemp(prefix="claude-sdk-home-")


def _cleanup_dirs(s: _Session) -> None:
    """删除会话的配置目录与自建的临时 cwd（幂等，目录不存在也不报错）。"""
    shutil.rmtree(s.config_dir, ignore_errors=True)
    if s.owns_cwd:
        shutil.rmtree(s.cwd, ignore_errors=True)


class ClaudeSdkHarness:
    """基于 Claude Agent SDK 的 Harness 适配器（S1 最小实现）。

    - 会话：每个 SessionHandle 对应一个后台 SDK 查询；查询以 ResultMessage 结束后会话即释放；
    - 事件：TextBlock/文本增量 → TextEvent，ThinkingBlock/思维增量 → ThinkingEvent，
      ResultMessage → UsageEvent + DoneEvent(end_turn；stop_reason=max_tokens 时为 max_tokens)，
      ResultMessage(subtype=error_max_turns) → ErrorEvent(HARNESS_MAX_TURNS) + UsageEvent + DoneEvent(error)，
      SDK/CLI 异常 → ErrorEvent(HARNESS_ERROR) + UsageEvent + DoneEvent(error)，
      init 报告越界工具/MCP server → ErrorEvent(HARNESS_ISOLATION_VIOLATION) + UsageEvent + DoneEvent(error)；
      每个 turn 结束前必有 UsageEvent（Contract D5）；绝不产出 ToolResult/Card/InputRequired 等平台专属事件；
    - 工具：见模块文档；技能（skills）S1 暂不注入 SDK；工具闸口由平台 toolbroker 负责（harness 拿不到执行权，
      因此无需 pre_tool/post_tool）；
    - pre_model：只在开启新查询前调用一次，即**只覆盖首轮模型调用**——SDK/CLI 内部 agent 循环中后续的模型
      调用（工具结果回灌后的再次调用）不经过它。返回 ``{"deny": reason}`` 时不启动查询，产出
      ErrorEvent(PRE_MODEL_DENIED) + UsageEvent + DoneEvent(error)。
    - 生命周期：run_turn 生成器若在本轮正常结束（产出 Done）之前被关闭（aclose / 取消 / 客户端断开），
      会话会被立即拆除并释放；interrupt 对已结束或未知的会话是无操作。
    """

    block_wait_s = 5.0  # handler 等待泵看到对应 ToolUseBlock 的最长秒数；超时则自生成 call_id（测试可调小）

    def __init__(self, client_factory: ClientFactory | None = None, config: ClaudeSdkConfig | None = None) -> None:
        try:
            import claude_agent_sdk  # noqa: F401  # 仅用于探测 SDK 是否已安装
        except ImportError as e:  # pragma: no cover - depends on env
            # SDK 未安装时给出明确的安装提示，而不是让调用方看到晦涩的 ImportError
            raise ImportError("install with `pip install 'ai-harness-claude-sdk[sdk]'`") from e
        self._client_factory: ClientFactory = client_factory or _default_client_factory
        self._config = config or ClaudeSdkConfig.from_env()
        self._sessions: dict[str, _Session] = {}  # session_id -> 运行期状态

    def capabilities(self) -> HarnessCapabilities:
        """声明能力矩阵。

        - loop_owner="harness"：模型-工具循环在 SDK/CLI 内部推进；
        - tool_interception="enforcing"：工具只能经平台桥接，执行与闸口都在平台侧，拒绝不可绕过
          （拦截探针测试为证）；
        - can_resume_from_events=False：进程重启后挂起中的 SDK 查询无法接续，只能按历史文本重建，
          不满足"由事件日志精确续跑"，因此整体落在 L1（允许只读 + 低风险写）；原生 resume 留到 S5 评估；
        - supports_vision/subagents=False：S1 只发送文本 prompt，且内置 Task 等工具全部禁用。
        """
        return HarnessCapabilities(
            name="claude-sdk",
            version="0.2",
            transport="stdio",  # SDK 实际以子进程方式运行 CLI，并非真正的进程内调用
            supports_vision=False,
            supports_native_resume=False,
            supports_subagents=False,
            tool_protocols=["mcp"],
            loop_owner="harness",
            tool_interception="enforcing",
            can_resume_from_events=False,
        )

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        """创建会话：准备工作目录（不存在则创建；未指定则建临时目录）与独立配置目录，不立即启动 SDK。

        SDK 查询要等首次 run_turn 拿到工具集与消息历史后才启动。
        """
        cwd, owns_cwd, config_dir = await asyncio.to_thread(_prepare_dirs, spec.workspace_dir)
        sid = uuid.uuid4().hex
        self._sessions[sid] = _Session(session_id=sid, spec=spec, cwd=cwd, owns_cwd=owns_cwd, config_dir=config_dir)
        return SessionHandle(session_id=sid, harness="claude-sdk")

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        """续跑：等同 open_session。历史由 runner 在下一次 run_turn 的 messages 中完整给出，
        首轮 prompt 由 ``build_prompt`` 把历史渲染成文本前缀（S1 最小实现，不使用 SDK 原生 resume）。"""
        handle = await self.open_session(spec)
        return handle.model_copy(update={"native_ref": f"replayed:{len(events)}"})

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        """驱动一个平台轮次：开启新查询，或把平台工具结果交还给挂起中的查询，然后转发事件直到
        下一次工具调用（Done tool_use）或查询结束（Done end_turn / error / cancelled）。"""
        s = self._sessions.get(session.session_id)
        if s is None:
            yield ErrorEvent(code="HARNESS_ERROR", message=f"unknown or finished claude-sdk session {session.session_id}")
            yield UsageEvent()
            yield DoneEvent(run_id=session.session_id, stop_reason="error")
            return
        run_id = s.spec.run_id
        completed = False  # 是否已产出本轮的 DoneEvent；未产出就被关闭视为本轮被放弃
        try:
            if s.awaiting:
                # 续上挂起的查询：在历史里找平台给出的工具结果，逐个解析对应 Future。
                results = {
                    str(m["call_id"]): m.get("content") for m in turn.messages if m.get("role") == "tool" and m.get("call_id") in s.awaiting
                }
                missing = s.awaiting - results.keys()
                if missing:
                    usage = self._take_usage(s)
                    await self._teardown(s, "cancelled")
                    yield ErrorEvent(code="HARNESS_ERROR", message=f"missing tool result for call(s): {', '.join(sorted(missing))}")
                    yield usage
                    completed = True
                    yield DoneEvent(run_id=run_id, stop_reason="error")
                    return
                for call_id, content in results.items():
                    fut = s.pending.get(call_id)
                    if fut is not None and not fut.done():
                        fut.set_result(_stringify(content))
                s.awaiting.clear()
            elif s.task is None:
                if hooks.pre_model:
                    verdict = await hooks.pre_model({"messages": turn.messages, "model_alias": s.spec.model_alias})
                    if verdict and "deny" in verdict:
                        # 被 guard 层拒绝：不启动查询，会话保留（与 litellm 适配器一致，可再次 run_turn）。
                        yield ErrorEvent(code="PRE_MODEL_DENIED", message=str(verdict["deny"]))
                        yield UsageEvent()
                        completed = True
                        yield DoneEvent(run_id=run_id, stop_reason="error")
                        return
                options = self._build_options(s, tools)
                s.task = asyncio.create_task(self._pump(s, options, build_prompt(turn.messages)))

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._config.turn_timeout_s
            while True:
                try:
                    item = await asyncio.wait_for(s.queue.get(), timeout=max(deadline - loop.time(), 0.0))
                except TimeoutError:
                    usage = self._take_usage(s)
                    await self._teardown(s, "cancelled")
                    yield ErrorEvent(code="HARNESS_ERROR", message=f"claude-sdk turn timed out after {self._config.turn_timeout_s}s")
                    yield usage
                    completed = True
                    yield DoneEvent(run_id=run_id, stop_reason="error")
                    return
                if isinstance(item, _End):
                    self._sessions.pop(s.session_id, None)
                    if s.task is not None:
                        # 等泵的 finally 断开 CLI 并删除会话目录后再结束本轮，保证 Done 之后不残留子进程与目录。
                        # 用 asyncio.wait 而非 gather：本生成器被取消时不会连带取消泵、打断其清理。
                        await asyncio.wait({s.task})
                    completed = True
                    yield DoneEvent(run_id=run_id, stop_reason=item.stop_reason)
                    return
                yield item
                if isinstance(item, ToolUseEvent):
                    # 本轮到此为止：平台接手闸口判定与执行，SDK 查询在后台挂着等结果。
                    s.awaiting.add(item.call_id)
                    yield self._take_usage(s)
                    completed = True
                    yield DoneEvent(run_id=run_id, stop_reason="tool_use")
                    return
        finally:
            # 生成器在产出 Done 之前被关闭（aclose / GeneratorExit / 取消）：本轮被放弃，后台查询与挂起的
            # Future 已无人接手，立即拆除会话，避免 CLI 子进程与临时目录泄漏。会话已被其他路径释放时跳过。
            if not completed and self._sessions.get(s.session_id) is s:
                await self._teardown(s, "cancelled")

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        """中断：调用 SDK interrupt、结束所有挂起的工具 Future、取消后台查询并释放会话。

        幂等：会话已结束（查询正常结束、已被中断或拆除）或从未存在时直接返回，不抛异常。
        """
        s = self._sessions.get(session.session_id)
        if s is not None:
            await self._teardown(s, "cancelled")

    # ---- SDK 选项与工具桥接 ----

    def _build_env(self, s: _Session) -> dict[str, str]:
        """子进程环境：清空继承来的 ANTHROPIC_*/CLAUDE_CODE_*/CLAUDE_AGENT_SDK_*/AWS_*/GOOGLE_*/AZURE_* 与
        运行时已知敏感变量（``_SCRUB_KEYS``），再指向模型网关。

        SDK 会把 os.environ 整体继承给 CLI，只能用空串覆盖、无法删除，因此本机的 ANTHROPIC_API_KEY、
        DATABASE_URL 等一律置空；网关凭据只经 ANTHROPIC_AUTH_TOKEN 传一份。主模型与 CLI 的辅助小模型
        （haiku/small-fast 等）全部指到同一别名，避免网关不认识 Claude 原生模型名；CLAUDE_CONFIG_DIR 指到
        本会话独立的临时目录，不读写本机 ~/.claude，也不落在 Run 工作区；CLAUDE_CODE_SKIP_PROMPT_HISTORY
        关闭输入历史写盘。
        """
        alias = s.spec.model_alias
        env = {k: "" for k in os.environ if k.startswith(_SCRUB_PREFIXES)}
        env.update(dict.fromkeys(_SCRUB_KEYS, ""))
        env.update(
            {
                "ANTHROPIC_BASE_URL": self._config.base_url,
                "ANTHROPIC_AUTH_TOKEN": self._config.auth_token,
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_MODEL": alias,
                "ANTHROPIC_SMALL_FAST_MODEL": alias,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": alias,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": alias,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": alias,
                "CLAUDE_CODE_SUBAGENT_MODEL": alias,
                "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_ERROR_REPORTING": "1",
                "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
                "CLAUDE_CONFIG_DIR": s.config_dir,
            }
        )
        return env

    def _build_options(self, s: _Session, tools: list[ToolSpec]) -> ClaudeAgentOptions:
        """构造隔离的 SDK 选项：只有平台工具，没有内置工具、本机设置、插件与外部 MCP。"""
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

        sdk_tools = [self._make_tool(s, t) for t in tools]
        mcp_servers: dict[str, Any] = {}
        if sdk_tools:
            mcp_servers[PLATFORM_SERVER] = create_sdk_mcp_server(name=PLATFORM_SERVER, version="1.0.0", tools=sdk_tools)
        return ClaudeAgentOptions(
            tools=[],  # 禁用全部内置工具（Bash/Read/Edit/Task/WebFetch...）
            allowed_tools=[TOOL_PREFIX + t.name for t in tools],  # 只放行平台桥接工具
            mcp_servers=mcp_servers,
            strict_mcp_config=True,  # 不合并本机/项目 .mcp.json 里的 MCP server
            setting_sources=[],  # 不加载 ~/.claude、项目 CLAUDE.md、本地设置与其中的 hooks/插件
            permission_mode="dontAsk",  # 未在 allowed_tools 中的一律拒绝（失败关闭），不弹交互确认
            system_prompt=s.spec.system_prompt,
            model=s.spec.model_alias,
            max_turns=self._config.max_turns,
            cwd=s.cwd,
            cli_path=self._config.cli_path,
            env=self._build_env(s),
            include_partial_messages=True,  # 获得 StreamEvent 文本/思维增量
            stderr=s.stderr_tail.append,
            # CLI 的会话转录不写盘（bundled CLI 2.1.x 提供该开关；SDK 以非 TTY stdout 运行即为 print 模式）
            extra_args={"no-session-persistence": None},
        )

    def _make_tool(self, s: _Session, spec: ToolSpec) -> SdkMcpTool[Any]:
        """把一个平台 ToolSpec 包成 SDK MCP 工具。handler 只做桥接：上报调用并等待平台结果。"""
        from claude_agent_sdk import tool

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            call_id = await self._claim_call_id(s, spec.name, args)
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            s.pending[call_id] = fut
            await s.queue.put(ToolUseEvent(call_id=call_id, tool_name=spec.name, input=args))
            try:
                content = await asyncio.wait_for(fut, timeout=self._config.turn_timeout_s)
            except TimeoutError:
                # 平台迟迟不给结果（例如 Run 已挂起等审批）：放弃整个查询，避免后台 CLI 无限挂着。
                self._abandon(s, "error", "timed out waiting for platform tool result")
                return {"content": [{"type": "text", "text": "platform did not return a tool result in time"}], "is_error": True}
            finally:
                s.pending.pop(call_id, None)
            return {"content": [{"type": "text", "text": content}]}

        return tool(spec.name, spec.description, _input_schema(spec))(handler)

    async def _claim_call_id(self, s: _Session, name: str, args: dict[str, Any]) -> str:
        """认领 SDK 的 tool_use id 作为 call_id。

        SDK 的 MCP handler 拿不到 tool_use id，但 CLI 总是先把含 ToolUseBlock 的 AssistantMessage
        发出再调工具。这里等泵处理完那条消息（也就保证了它之前的文本事件都已入队、顺序正确），
        按"工具名 + 入参"匹配认领其 id；超时未见（异常路径）则自生成 call_id，并记一笔"已跳过的同名块"，
        该块之后迟到时由泵丢弃，不会被下一次同名调用错认。
        """

        def find() -> int | None:
            same_name = [i for i, (_, n, _) in enumerate(s.blocks) if n == name]
            exact = [i for i in same_name if s.blocks[i][2] == args]
            candidates = exact or same_name  # 优先入参完全一致的块，处理同名工具的并行调用
            return candidates[0] if candidates else None

        async with s.block_cond:
            try:
                await asyncio.wait_for(s.block_cond.wait_for(lambda: find() is not None), timeout=self.block_wait_s)
            except TimeoutError:
                # 超时退出时已重新持有锁；块恰好在超时边界到达的话仍正常认领。
                if (idx := find()) is not None:
                    return s.blocks.pop(idx)[0]
                s.skipped[name] = s.skipped.get(name, 0) + 1
                return "call_" + uuid.uuid4().hex[:16]
            idx = find()
            assert idx is not None  # wait_for 已保证
            return s.blocks.pop(idx)[0]

    # ---- 后台泵：SDK 消息 → 统一事件 ----

    async def _pump(self, s: _Session, options: ClaudeAgentOptions, prompt: str) -> None:
        """在单个任务内完成一次 SDK 查询的全生命周期，并把消息翻译进出事件队列。"""
        client = self._client_factory(options)
        s.client = client
        try:
            await client.connect()
            await client.query(prompt)
            async for msg in client.receive_response():
                if await self._on_message(s, msg):
                    return
            await self._fail(s, "SDK message stream ended before ResultMessage")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # SDK/CLI 的任何异常都转成 HARNESS_ERROR，而不是让泵静默死掉
            await self._fail(s, f"{type(e).__name__}: {e}")
        finally:
            s.client = None
            try:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.disconnect(), timeout=_DISCONNECT_WAIT_S)
            finally:
                # 即使断开过程被取消也要执行：结束挂起 Future，删除本会话的配置目录与自建 cwd。
                self._fail_pending(s, "claude-sdk query finished")
                _cleanup_dirs(s)

    async def _on_message(self, s: _Session, msg: Message) -> bool:
        """处理一条 SDK 消息；返回 True 表示查询已结束（ResultMessage 或隔离违规）。子代理消息一律忽略。"""
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            StreamEvent,
            SystemMessage,
            TextBlock,
            ThinkingBlock,
            ToolUseBlock,
        )

        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                violation = _isolation_violation(msg.data)
                if violation:
                    # 失败关闭：CLI 实际拿到了平台之外的工具或 MCP server，不让查询继续推进。
                    await s.queue.put(ErrorEvent(code="HARNESS_ISOLATION_VIOLATION", message=f"claude-sdk isolation violated: {violation}"))
                    await s.queue.put(self._take_usage(s))
                    await s.queue.put(_End("error"))
                    return True
        elif isinstance(msg, StreamEvent):
            if msg.parent_tool_use_id is None:
                await self._on_stream_event(s, msg.event)
        elif isinstance(msg, AssistantMessage):
            if msg.parent_tool_use_id is not None:
                return False
            s.last_model = msg.model or s.last_model
            if msg.error:
                s.last_error = str(msg.error)
            if msg.usage:
                # CLI 会把同一次 API 调用拆成多条 AssistantMessage（每个内容块一条），用量按 message_id 去重。
                self._merge_usage(s, msg.message_id or uuid.uuid4().hex, msg.usage)
            # 已经以流式增量产出过的消息，不再重复产出其完整文本/思维块。
            streamed = (msg.message_id is not None and msg.message_id in s.streamed) or _ANON_STREAM in s.streamed
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text and not streamed:
                    await s.queue.put(TextEvent(delta=block.text))
                elif isinstance(block, ThinkingBlock) and block.thinking and not streamed:
                    await s.queue.put(ThinkingEvent(delta=block.thinking))
                elif isinstance(block, ToolUseBlock):
                    # 不直接映射为 ToolUseEvent（由桥接 handler 产出），只登记 id 供 handler 认领。
                    name = block.name.removeprefix(TOOL_PREFIX)
                    async with s.block_cond:
                        if s.skipped.get(name):
                            # 对应的调用已因等待超时自生成 call_id，这个迟到的块直接丢弃。
                            s.skipped[name] -= 1
                            continue
                        s.blocks.append((block.id, name, dict(block.input)))
                        s.block_cond.notify_all()
        elif isinstance(msg, ResultMessage):
            end: _End
            if msg.subtype == "error_max_turns":
                # SDK 内部 agent 循环用尽 max_turns：单独的错误码，便于平台区分"跑太久"与一般故障。
                await s.queue.put(ErrorEvent(code="HARNESS_MAX_TURNS", message=f"claude-sdk reached max_turns={self._config.max_turns}"))
                end = _End("error")
            elif msg.is_error:
                detail = msg.result or "; ".join(msg.errors or []) or s.last_error or msg.subtype
                await s.queue.put(ErrorEvent(code="HARNESS_ERROR", message=f"claude-sdk result error: {detail}"))
                end = _End("error")
            else:
                end = _End("max_tokens" if msg.stop_reason == "max_tokens" else "end_turn")
            await s.queue.put(self._final_usage(s, msg))
            await s.queue.put(end)
            return True
        return False

    async def _on_stream_event(self, s: _Session, event: dict[str, Any]) -> None:
        """处理原始 Anthropic 流事件：message_start 记录消息 id，文本/思维增量直接入队。"""
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            s.stream_msg_id = message.get("id")
            s.streamed.discard(_ANON_STREAM)
            if s.stream_msg_id and message.get("usage"):
                self._merge_usage(s, s.stream_msg_id, message["usage"])
        elif etype == "message_delta":
            # 流式模式下 AssistantMessage 的用量取自 message_start（output_tokens 尚为 0），最终输出量在
            # message_delta 里。实测 CLI 在内容块结束时就发起工具调用，message_delta 往往晚于 ToolUseEvent，
            # 所以工具轮次中途的 output_tokens 可能仍为 0；差额由查询结束时 ResultMessage 的累计量补齐，总量不丢。
            if s.stream_msg_id and event.get("usage"):
                self._merge_usage(s, s.stream_msg_id, event["usage"])
        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            key = s.stream_msg_id or _ANON_STREAM
            if delta.get("type") == "text_delta" and delta.get("text"):
                s.streamed.add(key)
                await s.queue.put(TextEvent(delta=delta["text"]))
            elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                s.streamed.add(key)
                await s.queue.put(ThinkingEvent(delta=delta["thinking"]))

    # ---- 用量 ----

    def _merge_usage(self, s: _Session, key: str, usage: dict[str, Any]) -> None:
        """合并同一次 API 调用（按 message_id）的用量：各字段取大，多次回报不会重复累加。"""
        cur = s.seen_usage.setdefault(key, dict.fromkeys(_USAGE_KEYS, 0))
        for k in _USAGE_KEYS:
            cur[k] = max(cur[k], int(usage.get(k) or 0))

    def _seen_totals(self, s: _Session) -> dict[str, int]:
        return {k: sum(u[k] for u in s.seen_usage.values()) for k in _USAGE_KEYS}

    def _usage_event(self, s: _Session, values: dict[str, int], cost: float = 0.0) -> UsageEvent:
        return UsageEvent(
            input_tokens=values["input_tokens"] + values["cache_read_input_tokens"] + values["cache_creation_input_tokens"],
            output_tokens=values["output_tokens"],
            cache_read_tokens=values["cache_read_input_tokens"],
            cost=cost,
            model=s.last_model or s.spec.model_alias,
        )

    def _take_usage(self, s: _Session) -> UsageEvent:
        """工具调用中途的用量：取截至此刻 AssistantMessage 累计、尚未上报的增量（SDK 中途不给成本，cost=0）。"""
        seen = self._seen_totals(s)
        delta = {k: max(seen[k] - s.reported[k], 0) for k in _USAGE_KEYS}
        for k in _USAGE_KEYS:
            s.reported[k] += delta[k]
        return self._usage_event(s, delta)

    def _final_usage(self, s: _Session, result: ResultMessage) -> UsageEvent:
        """查询结束时的用量：ResultMessage 的累计用量减去中途已上报部分（与逐条累计取大，防止少记）；
        成本取整次查询的 total_cost_usd（中途轮次成本记 0，因此不会重复计费）。"""
        total = result.usage or {}
        seen = self._seen_totals(s)
        values = {k: max(int(total.get(k) or 0) - s.reported[k], seen[k] - s.reported[k], 0) for k in _USAGE_KEYS}
        for k in _USAGE_KEYS:
            s.reported[k] += values[k]
        return self._usage_event(s, values, cost=float(result.total_cost_usd or 0.0))

    # ---- 失败、放弃与清理 ----

    async def _fail(self, s: _Session, message: str) -> None:
        """泵内异常：入队 ErrorEvent + UsageEvent + 结束标记（保证该轮仍满足 D5）。"""
        tail = " | ".join(s.stderr_tail)
        await s.queue.put(ErrorEvent(code="HARNESS_ERROR", message=f"{message}{' | stderr: ' + tail if tail else ''}"))
        await s.queue.put(self._take_usage(s))
        await s.queue.put(_End("error"))

    def _fail_pending(self, s: _Session, reason: str) -> None:
        """让所有挂起中的工具 Future 以异常结束（SDK 会把它作为工具错误回给模型）。"""
        for fut in list(s.pending.values()):
            if not fut.done():
                fut.set_exception(HarnessInterruptedError(reason))

    def _abandon(self, s: _Session, stop_reason: Literal["error", "cancelled"], reason: str) -> None:
        """同步放弃会话：释放会话表项、结束挂起 Future、取消泵任务，并给可能正在等待的 run_turn
        入队一个合规的结尾（Usage + Done）。不 await 泵任务，因此可以在工具 handler 内部调用。"""
        self._sessions.pop(s.session_id, None)
        self._fail_pending(s, reason)
        if stop_reason == "error":
            s.queue.put_nowait(ErrorEvent(code="HARNESS_ERROR", message=reason))
        s.queue.put_nowait(self._take_usage(s))
        s.queue.put_nowait(_End(stop_reason))
        if s.task is not None and not s.task.done():
            s.task.cancel()

    async def _teardown(self, s: _Session, stop_reason: Literal["error", "cancelled"]) -> None:
        """异步清理：先尽力调用 SDK interrupt，再放弃会话并等待泵任务退出（其 finally 负责断开 client），
        最后删除会话目录（覆盖从未启动查询的会话；重复删除无副作用）。"""
        client, task = s.client, s.task
        if client is not None and task is not None and not task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.interrupt(), timeout=_INTERRUPT_WAIT_S)
        self._abandon(s, stop_reason, "claude-sdk session interrupted")
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        _cleanup_dirs(s)
