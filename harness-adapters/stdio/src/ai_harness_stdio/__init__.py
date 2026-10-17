"""NDJSON 子进程 Harness 适配器（传输模式 B）。可对接任何遵循 PROTOCOL.md 协议的 CLI Harness
（通过瘦壳适配的 dsh / Goose，或用于测试的内置 echo harness）。

核心机制：引擎与子进程通过 stdin/stdout 交换一行一个 JSON 的帧（frame），涵盖
事件流（event）、Hooks 往返（hook / hook_result）、中断（interrupt）以及基于
历史事件重放的恢复（resume）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter

from ai_contract import (
    DoneEvent,
    ErrorEvent,
    Event,
    HarnessCapabilities,
    HookSet,
    SessionHandle,
    SessionSpec,
    SkillDir,
    ToolSpec,
    TurnInput,
    UsageEvent,
)

_EVENT = TypeAdapter(Event)  # 用于按 Event 的判别式联合类型校验/解析 JSON 帧中的 event 字段
_CLOSE_WAIT_S = 2.0  # 发送 close 帧后等待子进程退出的时长，超时即 kill
_INTERRUPT_GRACE_S = 3.0  # interrupt 后等待进行中的轮次收尾（子进程回报 done）的时长，超时直接结束子进程


@dataclass
class _Turn:
    """一个进行中的 run_turn：结束信号与是否已被中断。"""

    done: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False


class StdioHarness:
    """通过 NDJSON 帧协议驱动外部 CLI 子进程的 Harness 适配器。"""

    def __init__(self, cmd: list[str], name: str = "stdio") -> None:
        self.cmd = cmd  # 启动子进程的命令行（argv 形式）
        self.name = name  # Harness 名称，用于能力声明与 SessionHandle 标识
        self._procs: dict[str, asyncio.subprocess.Process] = {}  # session_id -> 对应的子进程句柄
        self._native: dict[str, str] = {}  # session_id -> 子进程回报的会话标识（turn 帧中使用）
        self._run_ids: dict[str, str] = {}  # session_id -> 所属 Run（适配器自行收尾时的 DoneEvent.run_id）
        self._turns: dict[str, _Turn] = {}  # session_id -> 进行中的 run_turn
        self._spawned: list[asyncio.subprocess.Process] = []  # 启动过且尚未确认退出的子进程（泄漏检查用）
        self._caps: HarnessCapabilities | None = None  # 子进程在 opened 帧中回报的能力，首次 open_session 后填充

    @classmethod
    def from_env(cls) -> StdioHarness:
        """从环境变量 HARNESS_STDIO_CMD / HARNESS_STDIO_NAME 构造实例，便于部署时配置外部 Harness 命令。"""
        cmd = os.environ.get("HARNESS_STDIO_CMD")
        if not cmd:
            raise KeyError("HARNESS_STDIO_CMD not set (e.g. 'python -m ai_harness_stdio.echo_harness')")
        return cls(shlex.split(cmd), name=os.environ.get("HARNESS_STDIO_NAME", "stdio"))

    def capabilities(self) -> HarnessCapabilities:
        """返回子进程通过 opened 帧回报的能力；若尚未建立会话，返回本适配器自身的能力声明。

        NDJSON 协议下轮次循环仍由平台驱动，工具调用以 event 帧回报后由平台执行，子进程无法绕过
        平台的拒绝（enforcing）；resume 帧接受平台重放的历史事件，因此可由事件日志重建会话。
        """
        return self._caps or HarnessCapabilities(
            name=self.name,
            version="0",
            transport="stdio",
            loop_owner="platform",  # 轮次循环由平台驱动，子进程只负责单次 turn
            tool_interception="enforcing",  # 工具由平台执行，平台的拒绝不可被绕过
            can_resume_from_events=True,  # 支持 resume 帧，可由平台事件日志重建会话
        )

    async def _send(self, proc: asyncio.subprocess.Process, frame: dict[str, Any]) -> None:
        """向子进程 stdin 写入一行 NDJSON 帧并 flush。"""
        assert proc.stdin
        proc.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode())
        await proc.stdin.drain()

    async def _recv(self, proc: asyncio.subprocess.Process) -> dict[str, Any]:
        """从子进程 stdout 读取一行 NDJSON 帧并解析；子进程提前关闭 stdout 视为异常。"""
        assert proc.stdout
        line = await proc.stdout.readline()
        if not line:
            raise RuntimeError("harness process closed stdout")
        frame: dict[str, Any] = json.loads(line)
        return frame

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        """启动子进程并发送 open 帧建立会话，记录其回报的能力。session_id 由适配器生成（子进程回报的标识
        记为 native_ref），同一适配器上的多个会话因此不会因子进程回报相同标识而互相覆盖、遗留子进程。"""
        proc = await asyncio.create_subprocess_exec(*self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        self._spawned.append(proc)
        try:
            await self._send(proc, {"frame": "open", "session": spec.model_dump(mode="json")})
            opened = await self._recv(proc)
            self._caps = HarnessCapabilities(**opened["capabilities"])
        except BaseException:
            await self._stop(proc)
            raise
        sid = uuid.uuid4().hex
        native = str(opened.get("session_id") or sid)
        self._procs[sid], self._native[sid], self._run_ids[sid] = proc, native, spec.run_id
        return SessionHandle(session_id=sid, harness=self.name, native_ref=native)

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        """发送 turn 帧驱动一次对话回合，循环读取子进程回报的帧直至收到 done 事件。

        期间若收到 hook 帧，说明子进程无权直接决策，需要引擎侧执行对应 Hook
        （如 pre_model/pre_tool）并把结果通过 hook_result 帧回灌，子进程据此
        决定是否继续（例如 deny 场景）。

        子进程中途退出、帧无法解析等情况收口为 Error → Usage → Done(error)；若本轮已被 interrupt，
        则以 Usage → Done(cancelled) 收尾。会话不存在（未知或已被 interrupt / close）时直接 Done(error)。
        """
        sid = session.session_id
        proc = self._procs.get(sid)
        run_id = self._run_ids.get(sid, sid)
        if proc is None:
            yield ErrorEvent(code="SESSION_NOT_FOUND", message=f"stdio session '{sid}' not found")
            yield UsageEvent()
            yield DoneEvent(run_id=run_id, stop_reason="error")
            return
        state = self._turns[sid] = _Turn()
        try:
            await self._send(
                proc,
                {
                    "frame": "turn",
                    "session_id": self._native.get(sid, sid),
                    "messages": turn.messages,
                    "tools": [t.model_dump(mode="json") for t in tools],
                    "skills": [{"name": s.name, "path": str(s.path)} for s in skills],
                },
            )
            while True:
                frame = await self._recv(proc)
                if frame["frame"] == "hook":
                    # 子进程请求执行钩子：按名称从 HookSet 取出对应回调并调用，再把结果回灌给子进程
                    fn = getattr(hooks, frame["hook"], None)
                    result = await fn(frame.get("payload", {})) if fn else None
                    await self._send(
                        proc, {"frame": "hook_result", "hook": frame["hook"], "call_id": frame.get("call_id"), "result": result}
                    )
                    continue
                if frame["frame"] == "event":
                    ev = _EVENT.validate_python(frame["event"])
                    yield ev
                    if ev.type == "done":
                        return
        except (RuntimeError, OSError, ValueError, KeyError) as exc:
            if not state.interrupted:
                yield ErrorEvent(code="HARNESS_ERROR", message=f"stdio harness: {type(exc).__name__}: {exc}"[:500])
            yield UsageEvent()
            yield DoneEvent(run_id=run_id, stop_reason="cancelled" if state.interrupted else "error")
        finally:
            state.done.set()
            if self._turns.get(sid) is state:
                del self._turns[sid]

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        """中断并结束会话：发送 interrupt 帧；若有进行中的轮次，等它收尾（子进程回报 done，至多
        _INTERRUPT_GRACE_S 秒），随后关闭子进程（runner 在每次 drive 结束时调用 interrupt，会话随之结束，
        不再遗留子进程）。对未知或已结束的会话幂等。"""
        sid = session.session_id
        proc = self._procs.get(sid)
        if proc is None:
            return
        state = self._turns.get(sid)
        if state is not None:
            state.interrupted = True
        if proc.returncode is None:
            with contextlib.suppress(OSError, RuntimeError):
                await self._send(proc, {"frame": "interrupt", "run_id": run_id})
        if state is not None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_INTERRUPT_GRACE_S):
                    await state.done.wait()
        await self.close(session)

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        """恢复会话：先开启新子进程会话，再发送 resume 帧携带历史事件供其重放上下文（checkpoint 重放式恢复）。"""
        handle = await self.open_session(spec)
        proc = self._procs[handle.session_id]
        await self._send(
            proc, {"frame": "resume", "session": spec.model_dump(mode="json"), "events": [e.model_dump(mode="json") for e in events]}
        )
        await self._recv(proc)  # 等待 resumed 确认帧
        return handle

    async def close(self, session: SessionHandle) -> None:
        """发送 close 帧并等待子进程退出（超时即 kill），清理会话映射。对未知或已关闭的会话幂等。"""
        sid = session.session_id
        proc = self._procs.pop(sid, None)
        self._native.pop(sid, None)
        self._run_ids.pop(sid, None)
        if proc is not None:
            await self._stop(proc)

    async def close_all(self) -> None:
        """关闭本适配器的全部会话（进程退出 / 探针重启时调用）。"""
        for sid in list(self._procs):
            await self.close(SessionHandle(session_id=sid, harness=self.name))

    def running_pids(self) -> list[int]:
        """本适配器启动过、仍在运行的子进程 pid（空列表表示没有残留子进程）。"""
        self._spawned = [p for p in self._spawned if p.returncode is None]
        return [p.pid for p in self._spawned]

    async def _stop(self, proc: asyncio.subprocess.Process) -> None:
        """请求子进程退出（close 帧），超时则 kill，并关闭 stdin。"""
        if proc.returncode is None:
            with contextlib.suppress(OSError, RuntimeError):
                await self._send(proc, {"frame": "close"})
            try:
                async with asyncio.timeout(_CLOSE_WAIT_S):
                    await proc.wait()
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        if proc.stdin is not None:
            proc.stdin.close()
