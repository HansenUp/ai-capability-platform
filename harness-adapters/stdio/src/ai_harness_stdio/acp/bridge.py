"""ACP 桥：NDJSON（PROTOCOL.md，平台唯一协议，D91）⇄ ACP（Agent Client Protocol，stdio JSON-RPC）。

进程链：StdioHarness ⇄ NDJSON ⇄ ``python -m ai_harness_stdio.acp.bridge --profile goose`` ⇄ ACP ⇄ ``goose acp``。
一个桥进程只服务一个会话（D99，不跨会话 / 租户复用）。桥在本进程内起 127.0.0.1 临时端口的 HTTP 服务（每会话
随机 bearer）：``/v1/chat/completions`` 是 llm_shim（agent 唯一的模型），``/mcp`` 是平台工具代理（agent 唯一的
工具来源）。agent 子进程用 profile 生成的最小环境、临时 HOME / 工作目录启动，拿不到平台模型网关的密钥。

启动即预热：HTTP 服务、agent 子进程与 ACP ``initialize`` 在读第一帧之前完成；进程池的 ``hello`` 帧因此在预热完成后
才得到 ``ready``（PROTOCOL.md "进程池预热握手"）。agent 与桥同一进程组，池按进程组回收时一并结束；
ACP ``session/new``（注入 MCP）推迟到第一个 turn 帧——那时才知道平台工具。``AcpProfile.after_session_new``
做开会话后检查（goose：移除内置扩展、只留 ``platform``，残留即拒绝）。

harness 持有循环的轮次语义（见 PROTOCOL.md）：
- 每个 NDJSON turn 恰好对应 agent 的一次模型调用（shim 拒绝同一 turn 内的第二次成功调用，失败关闭）；
- 无挂起工具调用时，turn 帧启动一次 ACP ``session/prompt``；模型经 shim 流式应答，思维 / 文本增量即刻作为事件上报；
- 模型请求工具时：agent（approve 模式先 ``session/request_permission``，只对本轮模型请求的平台工具放行——
  第二道闸）调 MCP ``tools/call``，桥认领 call_id，上报 ToolUse → Usage → Done(tool_use) 并结束本 turn；该 MCP
  调用挂起，ACP prompt 仍在进行；
- 下一个 turn 帧带回同 call_id 的 tool 结果：桥以该结果应答挂起的 MCP 调用，agent 继续，下一次模型调用经 shim
  以平台历史替换 messages（结构化续跑）；prompt 结束时上报 Done(end_turn 等)；
- 续跑（新进程 resume 后历史以 tool 结果结尾）：没有挂起调用，桥直接启动新的 prompt，shim 用平台历史续上。
- interrupt：ACP ``session/cancel``，shim 立即停止流式应答并拒绝之后的任何模型请求，本 turn 以 Done(cancelled)
  收尾；随后 close 帧回收 agent 子进程与临时目录。
任何越界（非平台工具、agent 自行多次调用模型、未经模型请求的工具调用、未知的 agent 请求）都以
``ISOLATION_VIOLATION`` 失败关闭：本 turn Error → Usage → Done(error)，会话作废。

版本（S6 评审）：``AcpProfile.agent_version()`` 探测 agent 二进制的版本（进程内按 realpath + mtime 缓存）；开会话前的
能力声明与 opened 帧都用它（取不到时 opened 帧退回 ACP initialize 的 ``agentInfo.version``），不再兜底 "0"。
密钥：模型网关密钥经进程池写的 0600 文件传入（``ACP_LLM_API_KEY_FILE``），桥读完即删；Linux 上桥自身
``prctl(PR_SET_DUMPABLE, 0)``，同 UID 的 agent 不能 ptrace / 读桥的内存与环境。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route

from ai_contract import (
    DoneEvent,
    ErrorEvent,
    Event,
    HarnessCapabilities,
    SessionSpec,
    TextEvent,
    ThinkingEvent,
    ToolSpec,
    ToolUseEvent,
    UsageEvent,
)

from .. import StdioHarness
from ..pool import WORKER_DIR_ENV
from .jsonrpc import ConnectionClosed, JsonRpcConnection, JsonRpcError, as_object
from .shim import LlmShim, ModelBackend, ModelToolCall, ScriptedBackend, UpstreamBackend
from .toolproxy import MCP_SERVER_NAME, McpToolProxy, ToolCallRejected, ToolNameConflictError, ToolNames, tool_result_text

log = logging.getLogger("ai_harness_stdio.acp")

ACP_PROTOCOL_VERSION = 1
INIT_TIMEOUT_S = 60.0  # agent 启动 + initialize 的时限
SESSION_NEW_TIMEOUT_S = 60.0  # session/new（含 MCP 连接）与开会话后检查的时限
AGENT_STOP_GRACE_S = 1.0  # 关闭 stdin / SIGTERM 后等待 agent 退出的时长，超时 SIGKILL
MODEL_SETTLE_WAIT_S = 30.0  # 认领工具调用 / 应答权限请求前，等进行中的模型流结束的上限
ISOLATION_VIOLATION = "ISOLATION_VIOLATION"
VERSION_PROBE_TIMEOUT_S = 10.0  # agent_version() 运行 `<agent> --version` 的时限
StopReason = Literal["end_turn", "tool_use", "max_tokens", "cancelled", "error", "awaiting"]
_STOP_MAP: dict[str, StopReason] = {"end_turn": "end_turn", "max_tokens": "max_tokens", "cancelled": "cancelled"}
_ERROR_STATUSES = ("error", "denied")


# ---------------- profile 接口 ----------------


@dataclass
class LaunchContext:
    """一次 agent 启动的隔离环境：全部路径都在桥自建的临时目录里，会话结束即删除。"""

    root: Path  # 临时根目录
    workdir: Path  # agent 的 cwd 与 ACP session cwd
    home: Path  # agent 的 HOME
    base_url: str  # http://127.0.0.1:<port>（shim 与工具代理）
    token: str  # 每会话随机 bearer（shim 与工具代理共用）

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def openai_base_url(self) -> str:
        return f"{self.base_url}/v1"


class IsolationError(RuntimeError):
    """开会话后检查发现 agent 带有平台以外的能力（失败关闭）。"""


@runtime_checkable
class AcpProfile(Protocol):
    """一个 ACP agent 的接入配置（goose / dsh / 测试用 fake）。桥只通过这个接口与具体 agent 打交道。"""

    name: str  # harness 名（capabilities.name）

    def agent_version(self) -> str:
        """agent 二进制的版本（进程内缓存、带超时）；找不到二进制或解析失败返回空串（发布闸门据此失败关闭）。"""
        ...

    def capabilities(self, agent_version: str) -> HarnessCapabilities:
        """opened 帧回报的能力（以真实二进制探针结果为准，D98）；version 即 agent_version，不兜底。"""
        ...

    def prepare(self, ctx: LaunchContext) -> None:
        """启动前在 ctx.root 下准备配置文件（如 dsh 的 patch）。"""
        ...

    def command(self, ctx: LaunchContext) -> list[str]:
        """agent 的 argv（stdio 上讲 ACP）。找不到二进制时抛 FileNotFoundError。"""
        ...

    def env(self, ctx: LaunchContext) -> dict[str, str]:
        """agent 的完整环境（不继承桥的环境；不得含平台密钥）。"""
        ...

    def mcp_server(self, ctx: LaunchContext) -> dict[str, Any]:
        """session/new.mcpServers 里注入的平台工具代理条目。"""
        ...

    def model_tool_name(self, wire: str) -> str:
        """MCP 工具名 → agent 给模型看的函数名（goose ``platform__x``，dsh ``mcp__platform__x``）。"""
        ...

    async def after_session_new(self, rpc: JsonRpcConnection, session_id: str, ctx: LaunchContext) -> None:
        """开会话后检查；发现平台以外的扩展 / 工具时抛 IsolationError。"""
        ...


def isolated_env(ctx: LaunchContext, path: str = "/usr/bin:/bin") -> dict[str, str]:
    """各 profile 共用的最小环境：HOME / TMPDIR / XDG 全部指向临时目录；出网代理指向黑洞端口，只放行本机回环
    （agent 唯一需要访问的是本桥的 shim 与工具代理）。"""
    tmp = ctx.root / "tmp"
    tmp.mkdir(exist_ok=True)
    blackhole = "http://127.0.0.1:9"
    return {
        "PATH": path,
        "HOME": str(ctx.home),
        "TMPDIR": str(tmp),
        "XDG_CONFIG_HOME": str(ctx.home / ".config"),
        "XDG_DATA_HOME": str(ctx.home / ".local" / "share"),
        "XDG_STATE_HOME": str(ctx.home / ".local" / "state"),
        "XDG_CACHE_HOME": str(ctx.home / ".cache"),
        "LANG": "C.UTF-8",
        "HTTP_PROXY": blackhole,
        "HTTPS_PROXY": blackhole,
        "ALL_PROXY": blackhole,
        "http_proxy": blackhole,
        "https_proxy": blackhole,
        "all_proxy": blackhole,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


_VERSION_CACHE: dict[tuple[str, int], str] = {}  # (二进制 realpath, mtime_ns) → 版本


def cached_agent_version(binary: str | None, probe: Callable[[str], str]) -> str:
    """按二进制 realpath + mtime 缓存 probe(realpath) 的结果（升级二进制后自动重新探测）；失败不缓存、返回空串。"""
    if not binary:
        return ""
    real = os.path.realpath(binary)
    try:
        key = (real, os.stat(real).st_mtime_ns)
    except OSError:
        return ""
    if key not in _VERSION_CACHE:
        try:
            version = probe(real).strip()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            log.warning("acp: cannot determine agent version of %s: %s", real, exc)
            return ""
        if not version:
            return ""
        _VERSION_CACHE[key] = version
    return _VERSION_CACHE[key]


def version_from_cli(binary: str) -> str:
    """运行 ``<binary> --version``（最小环境、有超时），取输出里第一个形如 1.2.3[-rc.1] 的版本号。"""
    out = subprocess.run(  # noqa: S603 - 平台配置的 agent 二进制
        [binary, "--version"],
        capture_output=True,
        text=True,
        timeout=VERSION_PROBE_TIMEOUT_S,
        check=False,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir(), "LANG": "C.UTF-8"},
    )
    m = re.search(r"\d+\.\d+\.\d+[0-9A-Za-z.+-]*", out.stdout or out.stderr or "")
    return m.group(0) if m else ""


class BaseAcpProfile:
    """AcpProfile 的默认实现：HTTP MCP 注入、无开会话后检查、声明 harness 持有循环 + enforcing + 可重放续跑。"""

    name = "acp"
    tool_interception: Literal["none", "advisory", "enforcing"] = "enforcing"
    can_resume_from_events = True

    def agent_version(self) -> str:
        return ""

    def capabilities(self, agent_version: str) -> HarnessCapabilities:
        return HarnessCapabilities(
            name=self.name,
            version=agent_version,
            transport="stdio",
            loop_owner="harness",
            tool_interception=self.tool_interception,
            can_resume_from_events=self.can_resume_from_events,
        )

    def prepare(self, ctx: LaunchContext) -> None:
        return None

    def command(self, ctx: LaunchContext) -> list[str]:
        raise NotImplementedError

    def env(self, ctx: LaunchContext) -> dict[str, str]:
        return isolated_env(ctx)

    def mcp_server(self, ctx: LaunchContext) -> dict[str, Any]:
        return {
            "type": "http",
            "name": MCP_SERVER_NAME,
            "url": ctx.mcp_url,
            "headers": [{"name": "Authorization", "value": f"Bearer {ctx.token}"}],
        }

    def model_tool_name(self, wire: str) -> str:
        return f"{MCP_SERVER_NAME}__{wire}"

    async def after_session_new(self, rpc: JsonRpcConnection, session_id: str, ctx: LaunchContext) -> None:
        return None


class CommandProfile(BaseAcpProfile):
    """任意 ACP agent 命令（测试用 fake agent）：``--profile cmd -- <argv...>``。环境额外带上 shim 地址与令牌
    （``ACP_OPENAI_BASE_URL`` / ``ACP_OPENAI_API_KEY``），以及以 ``ACP_AGENT_`` 开头的桥环境变量（测试开关）。"""

    def __init__(self, argv: list[str], name: str = "acp") -> None:
        if not argv:
            raise ValueError("cmd profile needs the agent argv after '--'")
        self.argv = argv
        self.name = name

    def command(self, ctx: LaunchContext) -> list[str]:
        return list(self.argv)

    def env(self, ctx: LaunchContext) -> dict[str, str]:
        env = isolated_env(ctx, os.environ.get("PATH", "/usr/bin:/bin"))
        env.update({k: v for k, v in os.environ.items() if k.startswith("ACP_AGENT_")})
        env.update({"ACP_OPENAI_BASE_URL": ctx.openai_base_url, "ACP_OPENAI_API_KEY": ctx.token})
        return env


def bridge_command(profile: str, *args: str) -> list[str]:
    """ACP 桥的子进程命令（交给 StdioHarness / 进程池）：``python -m ai_harness_stdio.acp.bridge --profile <profile> ...``。"""
    return [sys.executable, "-m", "ai_harness_stdio.acp.bridge", "--profile", profile, *args]


class AcpStdioHarness(StdioHarness):
    """经 ACP 桥接入的 StdioHarness：``capabilities()`` 返回 profile 的声明（harness 持有循环），而不是 StdioHarness
    的平台持有循环默认值——registry / 门禁在开会话前就要据此定能力等级；version 取 ``profile.agent_version()``
    （发布时冻结进快照），取不到时退回最近一次 opened 帧回报的能力，都没有则为空串（发布闸门 409）。"""

    def __init__(self, profile: str, *args: str, **kwargs: Any) -> None:
        name = str(kwargs.pop("name", profile))
        self._profile = CommandProfile(["-"], name=name) if profile == "cmd" else load_profile(profile, [])
        self.profile = profile
        super().__init__(bridge_command(profile, *args), name=name, **kwargs)

    def capabilities(self) -> HarnessCapabilities:
        version = self._profile.agent_version()
        if not version and self._caps is not None:
            return self._caps
        return self._profile.capabilities(version)


def load_profile(spec: str, argv: list[str]) -> AcpProfile:
    """按名字加载 profile：goose / dsh / cmd，或 "模块:属性"（类或实例）。"""
    builtin = {"goose": "ai_harness_stdio.goose.profile:GooseProfile", "dsh": "ai_harness_stdio.dsh.profile:DshProfile"}
    if spec == "cmd":
        return CommandProfile(argv, name=os.environ.get("ACP_PROFILE_NAME", "acp"))
    module, _, attr = builtin.get(spec, spec).partition(":")
    if not attr:
        raise ValueError(f"unknown ACP profile {spec!r} (expected goose, dsh, cmd or module:attr)")
    obj: Any = getattr(importlib.import_module(module), attr)
    profile = obj() if isinstance(obj, type) else obj
    if not isinstance(profile, AcpProfile):
        raise TypeError(f"{spec} is not an AcpProfile")
    return profile


# ---------------- 桥 ----------------


class AgentStartError(RuntimeError):
    """agent 启动 / initialize 失败。"""


@dataclass
class _Turn:
    """一个进行中的 NDJSON turn。"""

    queue: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)
    finished: bool = False
    usage_sent: bool = False
    model_calls: int = 0  # 本 turn 内成功完成的模型调用数
    inflight: int = 0  # 本 turn 内已放行、尚未结束（完成 / 失败）的模型调用数


@dataclass
class _PendingCall:
    """挂起中的 MCP tools/call（等平台回灌结果）。"""

    call_id: str
    future: asyncio.Future[tuple[str, bool]]
    reported: bool  # ToolUseEvent 是否已上报给平台


class _Server(uvicorn.Server):
    """不接管进程信号：桥的生命周期由 NDJSON close 帧 / stdin EOF / 进程池的 killpg 决定。"""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _write_frame(frame: dict[str, Any]) -> None:
    sys.stdout.buffer.write((json.dumps(frame, ensure_ascii=False) + "\n").encode())
    sys.stdout.buffer.flush()


class AcpBridge:
    """一个桥进程的全部状态（实现 shim 的 ShimHost 与工具代理的 ProxyHost）。"""

    def __init__(self, profile: AcpProfile, backend: ModelBackend, *, pid_dir: Path | None = None) -> None:
        self.profile = profile
        self.backend = backend
        self.pid_dir = pid_dir
        self.token = secrets.token_urlsafe(32)
        self.cancelled = asyncio.Event()
        self._spec: SessionSpec | None = None
        self.session_id = uuid.uuid4().hex
        self.history: list[dict[str, Any]] = []
        self.tools: list[ToolSpec] = []
        self.all_tools: dict[str, ToolSpec] = {}  # 本会话见过的平台工具（MCP tools/list 的内容）
        self.names = ToolNames()
        self.ctx: LaunchContext | None = None
        self.server: _Server | None = None
        self.server_task: asyncio.Task[None] | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.rpc: JsonRpcConnection | None = None
        self.acp_session: str | None = None
        self.agent_version = ""  # opened 帧回报的版本：profile.agent_version()，取不到时为 initialize 的 agentInfo.version
        self.turn: _Turn | None = None
        self.pending: dict[str, _PendingCall] = {}
        self.expected: list[ModelToolCall] = []  # 最近一次模型调用请求、尚未被 agent 调用的工具
        self.queued: list[ToolUseEvent] = []  # 已认领、尚未上报的工具调用（同一模型应答里的第 2 个及以后）
        self.held_usage: UsageEvent | None = None  # 带工具调用的模型调用的用量：等 ToolUse 上报后再发
        self.prompt_task: asyncio.Task[Any] | None = None
        self.failure: tuple[str, str] | None = None  # 两个 turn 之间发生、留给下一个 turn 上报的错误
        self.broken = False  # 会话已作废（隔离违规 / agent 异常）
        self.last_model_error: tuple[str, str] | None = None
        # 当前没有进行中的模型流。agent 常在流式应答的 finish_reason 处就发起权限请求 / 工具调用，早于 shim 读完
        # 用量块、登记本次调用的工具（真实 DeepSeek 实测）；认领前有界等待它置位。
        self.model_settled = asyncio.Event()
        self.model_settled.set()
        self._stderr: Any = None
        self._pid_file: Path | None = None
        self._closing = False

    # ---- 生命周期 ----

    async def start(self) -> None:
        """预热：HTTP 服务、agent 子进程、ACP initialize。"""
        root = Path(tempfile.mkdtemp(prefix=f"acp-{self.profile.name}-", dir=os.environ.get(WORKER_DIR_ENV) or None))
        (root / "work").mkdir()
        (root / "home").mkdir()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        self.ctx = LaunchContext(
            root=root, workdir=root / "work", home=root / "home", base_url=f"http://127.0.0.1:{port}", token=self.token
        )
        shim, proxy = LlmShim(self, self.backend), McpToolProxy(self)
        app = Starlette(
            routes=[
                Route("/v1/chat/completions", shim.chat, methods=["POST"]),
                Route("/v1/models", shim.models, methods=["GET"]),
                Route("/mcp", proxy.endpoint, methods=["GET", "POST", "DELETE"]),
            ]
        )
        self.server = _Server(uvicorn.Config(app, log_level="warning", lifespan="off", access_log=False, timeout_graceful_shutdown=1))
        self.server_task = asyncio.get_running_loop().create_task(self.server.serve(sockets=[sock]))
        for _ in range(200):
            if self.server.started or self.server_task.done():
                break
            await asyncio.sleep(0.01)
        if not self.server.started:
            raise AgentStartError("bridge HTTP server failed to start")
        self.profile.prepare(self.ctx)
        argv = self.profile.command(self.ctx)
        self._stderr = (root / "agent.log").open("wb")
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._stderr,
            cwd=str(self.ctx.workdir),
            env=self.profile.env(self.ctx),
            limit=64 * 1024 * 1024,
        )
        if self.pid_dir is not None:
            self._pid_file = self.pid_dir / f"agent-{self.proc.pid}.pid"
            self._pid_file.write_text(str(self.proc.pid))
        assert self.proc.stdout is not None and self.proc.stdin is not None
        self.rpc = JsonRpcConnection(self.proc.stdout, self.proc.stdin, on_request=self._on_agent_request, on_notification=self._on_update)
        self.rpc.start()
        try:
            init = await self.rpc.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    "clientInfo": {"name": "ai-platform-acp-bridge", "version": "1"},
                },
                timeout_s=INIT_TIMEOUT_S,
            )
        except (JsonRpcError, ConnectionClosed, TimeoutError) as exc:
            raise AgentStartError(f"ACP initialize failed: {exc}; agent log: {self.agent_log_tail()}") from exc
        caps = (init or {}).get("agentCapabilities") or {}
        if not (caps.get("mcpCapabilities") or {}).get("http"):
            raise AgentStartError("agent does not support HTTP MCP servers (mcpCapabilities.http)")
        self.agent_version = self.profile.agent_version() or str(((init or {}).get("agentInfo") or {}).get("version") or "")

    def agent_log_tail(self, limit: int = 800) -> str:
        if self.ctx is None:
            return ""
        with contextlib.suppress(OSError):
            return (self.ctx.root / "agent.log").read_text(encoding="utf-8", errors="replace")[-limit:]
        return ""

    def open(self, spec: SessionSpec) -> dict[str, Any]:
        self._spec = spec
        caps = self.profile.capabilities(self.agent_version)
        return {"frame": "opened", "session_id": self.session_id, "capabilities": caps.model_dump(mode="json")}

    async def shutdown(self) -> None:
        """回收：拒绝一切后续请求、结束挂起调用、关闭 agent（stdin EOF → SIGTERM → SIGKILL）、HTTP 服务与临时目录。"""
        if self._closing:
            return
        self._closing = True
        self.cancelled.set()
        for p in list(self.pending.values()):
            if not p.future.done():
                p.future.set_exception(ToolCallRejected("platform session closed"))
        if self.prompt_task is not None and not self.prompt_task.done():
            self.prompt_task.cancel()
        if self.rpc is not None:
            await self.rpc.close()
        if self.proc is not None:
            await self._stop_agent(self.proc)
        if self.server is not None:
            self.server.should_exit = True
            self.server.force_exit = True
        if self.server_task is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                async with asyncio.timeout(1.0):
                    await self.server_task
            if not self.server_task.done():
                self.server_task.cancel()
                await asyncio.gather(self.server_task, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.backend.aclose()
        if self._stderr is not None:
            self._stderr.close()
        if self.ctx is not None:
            shutil.rmtree(self.ctx.root, ignore_errors=True)

    async def _stop_agent(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stdin is not None:
            with contextlib.suppress(OSError, RuntimeError):
                proc.stdin.close()
        for step in ("wait", "terminate", "kill"):
            if proc.returncode is not None:
                break
            if step == "terminate":
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
            elif step == "kill":
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(AGENT_STOP_GRACE_S if step != "kill" else 5.0):
                    await proc.wait()
        if self._pid_file is not None and proc.returncode is not None:
            self._pid_file.unlink(missing_ok=True)

    # ---- ShimHost / ProxyHost ----

    @property
    def spec(self) -> SessionSpec | None:
        return self._spec

    def platform_history(self) -> list[dict[str, Any]]:
        return self.history

    def turn_tools(self) -> list[ToolSpec]:
        return self.tools

    def mcp_tools(self) -> list[ToolSpec]:
        return list(self.all_tools.values())

    def tool_names(self) -> ToolNames:
        return self.names

    def model_tool_name(self, wire: str) -> str:
        return self.profile.model_tool_name(wire)

    def begin_model_call(self) -> str | None:
        if self.broken:
            return "platform session already failed"
        turn = self.turn
        if turn is None or turn.finished:
            reason = "agent issued a model request outside a platform turn"
        elif turn.model_calls + turn.inflight >= 1:  # 进行中的调用也计入：并发的第二个请求同样越界
            reason = "agent issued a second (or concurrent) model request within one platform turn (harness-side loop)"
        else:
            turn.inflight += 1
            self.model_settled.clear()
            return None
        self.violation(reason)
        return reason

    def emit_delta(self, event: TextEvent | ThinkingEvent) -> None:
        self._emit(event)

    def model_call_done(self, usage: UsageEvent, calls: list[ModelToolCall], finish_reason: str | None) -> None:
        if self.turn is not None:
            self.turn.model_calls += 1
            self.turn.inflight = max(0, self.turn.inflight - 1)
        self.last_model_error = None
        self.model_settled.set()
        if calls:
            self.expected.extend(calls)
            if self.held_usage is not None:  # 理论上不会发生（每 turn 一次调用）；稳妥起见累加
                usage = UsageEvent(
                    input_tokens=usage.input_tokens + self.held_usage.input_tokens,
                    output_tokens=usage.output_tokens + self.held_usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens + self.held_usage.cache_read_tokens,
                    cost=usage.cost + self.held_usage.cost,
                    model=usage.model,
                )
            self.held_usage = usage
        else:
            self._emit(usage)

    def model_call_failed(self, code: str, message: str) -> None:
        log.warning("acp shim: model call failed: %s: %s", code, message)
        if self.turn is not None:  # 失败的调用不计入次数（agent 可重试），但不再占进行中名额
            self.turn.inflight = max(0, self.turn.inflight - 1)
        self.last_model_error = (code, message)
        self.model_settled.set()

    def violation(self, message: str) -> None:
        log.error("acp bridge: isolation violation: %s", message)
        first = not self.broken
        self.broken = True
        self.model_settled.set()
        if self.turn is not None and not self.turn.finished:
            self._fail_turn(ISOLATION_VIOLATION, message)
        elif self.failure is None:
            self.failure = (ISOLATION_VIOLATION, message)
        if first:
            self._spawn_cancel()

    async def call_tool(self, wire_name: str, arguments: dict[str, Any], meta_call_id: str | None) -> tuple[str, bool]:
        if self.cancelled.is_set() or self.broken:
            raise ToolCallRejected("platform session is not accepting tool calls")
        platform = self.names.platform(wire_name)
        if platform is None:
            self.violation(f"agent called an unknown MCP tool {wire_name!r}")
            raise ToolCallRejected(f"unknown tool {wire_name!r}")
        await self._await_model_settled()
        exp = self._claim(platform, arguments, meta_call_id)
        if exp is None:
            self.violation(f"agent invoked platform tool {platform!r} without a matching model tool call in this turn")
            raise ToolCallRejected("tool call was not requested by the model")
        fut: asyncio.Future[tuple[str, bool]] = asyncio.get_running_loop().create_future()
        ev = ToolUseEvent(call_id=exp.call_id, tool_name=exp.name, input=exp.input)
        pending = self.pending[exp.call_id] = _PendingCall(exp.call_id, fut, reported=False)
        if self.turn is not None and not self.turn.finished:
            pending.reported = True
            self._emit(ev)
            self._finish("tool_use")
        else:
            self.queued.append(ev)
        try:
            return await fut
        finally:
            self.pending.pop(exp.call_id, None)

    async def _await_model_settled(self) -> None:
        """有界等待进行中的模型流结束（其工具调用登记进 expected）；中断 / 违规时立即返回。"""
        if not self.model_settled.is_set():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(MODEL_SETTLE_WAIT_S):
                    await self.model_settled.wait()

    def _claim(self, platform: str, arguments: dict[str, Any], meta_call_id: str | None) -> ModelToolCall | None:
        """按 agent 给出的模型侧 tool_call id（goose）认领；没有时按"工具名 + 入参"、再按工具名认领（dsh）。"""
        candidates = [e for e in self.expected if e.name == platform]
        pick = None
        if meta_call_id is not None:
            pick = next((e for e in candidates if e.call_id == meta_call_id), None)
        if pick is None:
            pick = next((e for e in candidates if e.input == arguments), None) or (candidates[0] if candidates else None)
        if pick is not None:
            self.expected.remove(pick)
        return pick

    # ---- 事件 ----

    def _emit(self, ev: Event) -> None:
        turn = self.turn
        if turn is None or turn.finished:
            log.debug("acp bridge: dropping %s outside an active turn", ev.type)
            return
        if isinstance(ev, UsageEvent):
            turn.usage_sent = True
        turn.queue.put_nowait(ev)

    def _finish(self, stop_reason: StopReason) -> None:
        turn = self.turn
        if turn is None or turn.finished:
            return
        if self.held_usage is not None:
            self._emit(self.held_usage)
            self.held_usage = None
        elif not turn.usage_sent:
            self._emit(UsageEvent(model=self._spec.model_alias if self._spec else None))
        run_id = self._spec.run_id if self._spec else self.session_id
        turn.queue.put_nowait(DoneEvent(run_id=run_id, stop_reason=stop_reason))
        turn.finished = True

    def _fail_turn(self, code: str, message: str) -> None:
        self._emit(ErrorEvent(code=code, message=message[:500]))
        self._finish("error")

    def _spawn_cancel(self) -> None:
        if self.rpc is not None and self.acp_session is not None and not self.rpc.closed:
            asyncio.get_running_loop().create_task(self._cancel_prompt())

    async def _cancel_prompt(self) -> None:
        if self.rpc is None or self.acp_session is None:
            return
        with contextlib.suppress(ConnectionClosed, JsonRpcError):
            await self.rpc.notify("session/cancel", {"sessionId": self.acp_session})

    # ---- turn ----

    async def run_turn(self, frame: dict[str, Any]) -> None:
        """处理一个 turn 帧，直到本 turn 的 Done 写出。"""
        turn = self.turn = _Turn()
        drain = asyncio.get_running_loop().create_task(self._drain(turn))
        try:
            await self._begin_turn(turn, frame)
        except Exception as exc:  # 任何非预期异常都收口为 Error → Usage → Done(error)
            log.exception("acp bridge: turn failed")
            self.broken = True
            self._fail_turn("HARNESS_ERROR", f"acp bridge: {type(exc).__name__}: {exc}")
        await drain

    async def _drain(self, turn: _Turn) -> None:
        while True:
            ev = await turn.queue.get()
            _write_frame({"frame": "event", "event": ev.model_dump(mode="json")})
            if isinstance(ev, DoneEvent):
                return

    async def _begin_turn(self, turn: _Turn, frame: dict[str, Any]) -> None:
        if self.failure is not None:
            code, message = self.failure
            self.failure = None
            self.broken = True
            self._fail_turn(code, message)
            return
        if self.broken or self.cancelled.is_set():
            self._fail_turn("HARNESS_ERROR", "acp session is no longer usable (failed or interrupted)")
            return
        messages = frame.get("messages")
        if not isinstance(messages, list):
            raise ValueError("turn frame without messages")
        self.history = messages
        self.tools = [ToolSpec.model_validate(t) for t in frame.get("tools") or []]
        try:
            for t in self.tools:
                self.names.add(t.name)
        except ToolNameConflictError as exc:
            self._fail_turn("TOOL_NAME_CONFLICT", str(exc))
            return
        for t in self.tools:
            self.all_tools.setdefault(t.name, t)
        if self.acp_session is None:
            try:
                await self._new_session()
            except IsolationError as exc:
                self.violation(str(exc))
                return
        statuses = as_object(frame.get("tool_statuses"))
        if self.pending:
            self._resolve_pending(messages, statuses)
            return
        if self.prompt_task is not None and not self.prompt_task.done():
            self._fail_turn("PROTOCOL_ERROR", "agent prompt still running but no tool call is waiting for a platform result")
            return
        self._start_prompt(_prompt_text(messages))

    def _resolve_pending(self, messages: list[dict[str, Any]], statuses: dict[str, Any]) -> None:
        results = {str(m.get("call_id")): m for m in messages if m.get("role") == "tool"}
        missing = [cid for cid, p in self.pending.items() if p.reported and cid not in results]
        if missing:
            self._fail_turn("PROTOCOL_ERROR", f"turn frame lacks tool results for pending calls {missing}")
            return
        for cid, p in list(self.pending.items()):
            if p.reported and not p.future.done():
                p.future.set_result((tool_result_text(results[cid].get("content")), statuses.get(cid) in _ERROR_STATUSES))
        if self.queued:  # 同一模型应答里的下一个工具调用：不经模型，直接成为本 turn 的 ToolUse
            ev = self.queued.pop(0)
            if ev.call_id in self.pending:
                self.pending[ev.call_id].reported = True
            self._emit(ev)
            self._finish("tool_use")

    async def _new_session(self) -> None:
        assert self.rpc is not None and self.ctx is not None
        params = {"cwd": str(self.ctx.workdir), "mcpServers": [self.profile.mcp_server(self.ctx)]}
        res = await self.rpc.request("session/new", params, timeout_s=SESSION_NEW_TIMEOUT_S)
        sid = (res or {}).get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise RuntimeError(f"session/new returned no sessionId: {res!r}")
        self.acp_session = sid
        async with asyncio.timeout(SESSION_NEW_TIMEOUT_S):
            await self.profile.after_session_new(self.rpc, sid, self.ctx)

    def _start_prompt(self, text: str) -> None:
        assert self.rpc is not None and self.acp_session is not None
        coro = self.rpc.request("session/prompt", {"sessionId": self.acp_session, "prompt": [{"type": "text", "text": text}]})
        self.prompt_task = asyncio.get_running_loop().create_task(coro)
        self.prompt_task.add_done_callback(self._on_prompt_done)

    def _on_prompt_done(self, task: asyncio.Task[Any]) -> None:
        if self.cancelled.is_set():
            self._finish("cancelled")
            return
        code, message = "HARNESS_ERROR", ""
        stop: StopReason | None = None
        if task.cancelled():
            message = "agent prompt was cancelled"
        elif task.exception() is not None:
            exc = task.exception()
            message = f"agent prompt failed: {exc}"
        else:
            result = task.result() or {}
            raw = str(result.get("stopReason", ""))
            stop = _STOP_MAP.get(raw)
            if stop is None:
                code, message = "AGENT_STOPPED", f"agent ended the prompt with stopReason={raw!r}"
        if stop == "end_turn" and self.turn is not None and not self.turn.finished and self.turn.model_calls == 0 and self.last_model_error:
            stop = None
            code, message = self.last_model_error
        if stop is None and self.last_model_error and code == "HARNESS_ERROR":
            code, message = self.last_model_error[0], f"{message}; last model error: {self.last_model_error[1]}"
        waiting = [cid for cid, p in self.pending.items() if not p.future.done()]
        if waiting and stop is not None:
            stop, code, message = None, "HARNESS_ERROR", f"agent ended the prompt while tool calls {waiting} await platform results"
        if self.turn is not None and not self.turn.finished:
            if stop is None:
                self._fail_turn(code, f"{message}; agent log: {self.agent_log_tail(300)}")
            else:
                self._finish(stop)
        elif stop is None or waiting:
            self.failure = self.failure or (code, message)

    # ---- interrupt ----

    async def interrupt(self) -> None:
        """中断：shim 停止流式应答并拒绝后续模型请求，ACP session/cancel，本 turn 以 Done(cancelled) 收尾。"""
        self.cancelled.set()
        self.model_settled.set()
        await self._cancel_prompt()
        self._finish("cancelled")

    # ---- agent → client ----

    async def _on_agent_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "session/request_permission":
            await self._await_model_settled()
            return self._permission(params)
        self.violation(f"agent sent an unsupported client request {method!r}")
        raise JsonRpcError(-32601, f"method not supported: {method}")

    def _permission(self, params: dict[str, Any]) -> dict[str, Any]:
        """第二道闸：只放行本轮模型请求的平台工具调用（按模型侧 tool_call id 匹配）。"""
        tool_call = as_object(params.get("toolCall"))
        cid = str(tool_call.get("toolCallId", ""))
        options = [o for o in params.get("options") or [] if isinstance(o, dict)]

        def pick(*kinds: str) -> dict[str, Any]:
            for kind in kinds:
                for o in options:
                    if o.get("kind") == kind:
                        return {"outcome": {"outcome": "selected", "optionId": o.get("optionId")}}
            return {"outcome": {"outcome": "cancelled"}}

        if self.cancelled.is_set() or self.broken:
            return pick("reject_once", "reject_always")
        if any(e.call_id == cid for e in self.expected):
            return pick("allow_once", "allow_always")
        self.violation(
            f"agent asked permission for a tool call that is not a pending platform tool call: {cid!r} ({tool_call.get('title')!r})"
        )
        return pick("reject_once", "reject_always")

    async def _on_update(self, method: str, params: dict[str, Any]) -> None:
        return None  # 事件以 shim 看到的模型流为准；ACP 的 session/update 只是 agent 的转述


def _prompt_text(messages: list[dict[str, Any]]) -> str:
    """新 prompt 的文本：最后一条是用户消息时用它（agent 自身记录更自然），否则是续跑占位（shim 会用平台历史替换）。"""
    last = messages[-1] if messages else {}
    if last.get("role") == "user":
        content = last.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            text = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            if text:
                return text
    return "Continue."


# ---------------- 入口 ----------------


async def serve(bridge: AcpBridge) -> None:
    """读 NDJSON 帧直到 close / EOF。"""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    turn_task: asyncio.Task[None] | None = None
    try:
        while True:
            line = await reader.readline()
            if not line:
                return
            if not line.strip():
                continue
            frame = json.loads(line)
            kind = frame.get("frame")
            if kind == "hello":  # 进程池预热握手：start()（agent 启动 + ACP initialize）已在读帧之前完成
                _write_frame({"frame": "ready"})
            elif kind == "open":
                _write_frame(bridge.open(SessionSpec.model_validate(frame["session"])))
            elif kind == "resume":
                _write_frame({"frame": "resumed", "session_id": bridge.session_id})
            elif kind == "turn":
                if turn_task is not None and not turn_task.done():
                    await turn_task  # 协议上不会并发 turn；稳妥起见串行
                turn_task = loop.create_task(bridge.run_turn(frame))
            elif kind == "interrupt":
                await bridge.interrupt()
            elif kind == "close":
                return
            # hook_result：本桥不声明 internal_hook_points，忽略
    finally:
        await bridge.interrupt()
        if turn_task is not None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(2.0):
                    await asyncio.shield(turn_task)
            if not turn_task.done():
                turn_task.cancel()
            await asyncio.gather(turn_task, return_exceptions=True)
        await bridge.shutdown()


def _consume_secret(name: str) -> str | None:
    """取密钥并从本进程环境移除：优先 ``<NAME>_FILE``（进程池写的 0600 文件，读完即删），其次环境变量本身。"""
    path = os.environ.pop(f"{name}_FILE", None)
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip() or None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)
    return os.environ.pop(name, None)


def _harden() -> None:
    """Linux：PR_SET_DUMPABLE=0，同 UID 的进程（含 agent）不能 ptrace 桥、读 /proc/<桥>/environ 与内存。失败忽略。"""
    if not sys.platform.startswith("linux"):
        return
    with contextlib.suppress(OSError, AttributeError):
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4


def _backend(args: argparse.Namespace) -> ModelBackend:
    key = _consume_secret("ACP_LLM_API_KEY") or _consume_secret("MODEL_GATEWAY_API_KEY")
    if args.script is not None:
        if args.calls is None:
            raise SystemExit("--script requires --calls")
        return ScriptedBackend(args.script, args.calls)
    base = os.environ.get("ACP_LLM_BASE_URL") or os.environ.get("MODEL_GATEWAY_URL")
    if not base or not key:
        raise SystemExit("ACP bridge needs ACP_LLM_BASE_URL/ACP_LLM_API_KEY (or MODEL_GATEWAY_URL/MODEL_GATEWAY_API_KEY), or --script")
    return UpstreamBackend(base, key, timeout_s=float(os.environ.get("LLM_TIMEOUT_S", "120")))


async def amain(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    agent_argv: list[str] = []
    if "--" in raw:
        i = raw.index("--")
        raw, agent_argv = raw[:i], raw[i + 1 :]
    parser = argparse.ArgumentParser(prog="python -m ai_harness_stdio.acp.bridge", description="NDJSON ⇄ ACP bridge")
    parser.add_argument("--profile", required=True, help="goose | dsh | cmd (agent argv after --) | module:attr")
    parser.add_argument("--script", type=Path, help="ModelScript JSON: scripted model (contract probes)")
    parser.add_argument("--calls", type=Path, help="with --script: append each model call's normalized messages here")
    parser.add_argument("--pid-dir", type=Path, help="record agent pids here (leak checks in tests)")
    args = parser.parse_args(raw)
    _harden()
    logging.basicConfig(level=os.environ.get("ACP_BRIDGE_LOG", "WARNING"), stream=sys.stderr, format="%(name)s %(levelname)s %(message)s")
    profile = load_profile(args.profile, agent_argv)
    bridge = AcpBridge(profile, _backend(args), pid_dir=args.pid_dir)
    try:
        await bridge.start()
    except (AgentStartError, OSError) as exc:
        print(f"acp bridge: agent start failed: {exc}", file=sys.stderr)
        await bridge.shutdown()
        return 1
    await serve(bridge)
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
