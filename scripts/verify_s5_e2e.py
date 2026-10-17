#!/usr/bin/env python3
"""S5 本机出口验证（Contract 六要素完整化：Hooks、claude-sdk 结构化续跑、安全遗留）。

前置：compose 已启动 keycloak、litellm（DeepSeek）、observe profile（Langfuse）。本脚本以子进程启动：
mcp-ticket(18191，数据为临时副本：T-1001 描述里插入一个随机手机号) ← 能力网关(18182，OBO) ← Runtime(18190，
强制鉴权、工具经网关分发、TOOL_RESULT_OFFLOAD_TOKENS=300、HARNESS_LOOP_HIGH_RISK_ALLOWLIST=s5-claude-approve、
CONFIDENTIAL_MODEL_ALIASES=DeepSeek 别名)。然后按 S5 方案 §5：

1. claude-sdk 实时探针子集（进程内适配器 + 真实 CLI）：事件流、工具回灌（模型复述只出现在工具结果里的 nonce）、
   流式中 / 等待工具结果时中断后 CLI 子进程已回收、每轮用量 > 0；另经 Runtime 取消一个 claude-sdk Run，CLI 子进程回收；
2. litellm 与 claude-sdk 各发布带 hooks 的版本：deny_tool_pattern 拒绝 mail.send；redact_pii 后回答、TOOL_RESULT、
   事件日志、外置工件与 Langfuse 中都没有原始手机号；thinking_summary（llm）下 SSE 每轮至多一条 summary 思维事件且与
   MODEL_RESPONSE.thinking_summary 一致；token_budget 第二轮拒绝 → Run failed；
3. claude-sdk 审批续跑：高风险写挂起 → 他人审批 → 发起人续跑 → completed；TOOL_EXECUTED 只出现一次；结构化续跑证据
   （native_ref 不进事件日志：以续跑期间 CLI 子进程的 `--resume <uuid>` 参数为证，见 check 说明）；
4. 安全遗留（D87 修订：续跑只允许发起人）：第三人续跑 / 读事件 / 取消 403，审批人续跑 403；续跑时篡改初始消息 409；
   带机密附件的新 Run 继承污点被 DATA_CLASS_MODEL_BLOCKED 拦截（内部附件对照不拦截）；机密工具 error 结果加污点，
   续跑前后一致（Trace 内容均脱敏）；
5. Langfuse 中各 Run 均有 Trace，且 span 上有 `ai.hook.<point>.*` 属性。

用法：.venv/bin/python scripts/verify_s5_e2e.py     退出码 0 表示全部通过。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

for _k in ("NO_PROXY", "no_proxy"):  # 本机若配置了 HTTP(S)_PROXY，访问 localhost 时绕过
    os.environ[_k] = ",".join(x for x in (os.environ.get(_k, ""), "localhost,127.0.0.1") if x)

import httpx  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import devkit  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
ALIAS = os.environ.get("S5_MODEL_ALIAS", "deepseek-v4-pro-2026-08")
BLOCKED_ALIAS = "default-chat"  # 不在 CONFIDENTIAL_MODEL_ALIASES 内的已登记别名（附件污点用例在调模型前即被拦截）
TMP = Path(tempfile.mkdtemp(prefix="s5-e2e-"))
PORTS = {"mcp": 18191, "gateway": 18182, "runtime": 18190}
PHONE = "139" + "".join(secrets.choice("0123456789") for _ in range(8))  # 每次随机，避免与历史数据混淆
UID = devkit.USER_IDS
APPROVE_AGENT = "s5-claude-approve"

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  [{'通过' if ok else '失败'}] {what}")
    if not ok:
        failures.append(what)


# ---------------- 子进程与进程树 ----------------


@contextmanager
def service(name: str, app: str, app_dir: str, port: int, env: dict[str, str]) -> Iterator[tuple[str, int]]:
    """以子进程启动一个 uvicorn 服务，等 /healthz 就绪后交出（基址, pid）；退出时终止进程。"""
    base = f"http://127.0.0.1:{port}"
    try:
        httpx.get(f"{base}/healthz", timeout=1)
        raise RuntimeError(f"端口 {port} 已被占用（上次运行残留的服务？），请先结束占用进程")
    except httpx.HTTPError:
        pass
    log = open(TMP / f"{name}.log", "w")  # noqa: SIM115
    proc = subprocess.Popen(  # noqa: S603 —— 固定参数启动本仓库的服务
        [PY, "-m", "uvicorn", app, "--port", str(port), "--app-dir", str(ROOT / app_dir)],
        env=os.environ.copy() | env,
        stdout=log,
        stderr=log,
    )
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError(f"{name} 未能启动，见 {TMP / (name + '.log')}")
        yield base, proc.pid
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def descendants(root: int) -> list[tuple[int, str]]:
    """root 的全部后代进程（pid, 命令行）。"""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, check=True).stdout  # noqa: S607
    children: dict[int, list[int]] = {}
    cmds: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        cmds[pid] = parts[2] if len(parts) > 2 else ""
        children.setdefault(ppid, []).append(pid)
    found, stack = [], list(children.get(root, []))
    while stack:
        p = stack.pop()
        found.append((p, cmds.get(p, "")))
        stack += children.get(p, [])
    return found


def cli_procs(root: int) -> list[tuple[int, str]]:
    """root 名下的 Claude Code CLI 进程（SDK 自带 CLI 的路径含 claude）。"""
    return [(p, c) for p, c in descendants(root) if "claude" in c.lower()]


def wait_no_cli(root: int, timeout_s: float = 10) -> list[tuple[int, str]]:
    deadline = time.monotonic() + timeout_s
    while True:
        left = cli_procs(root)
        if not left or time.monotonic() > deadline:
            return left
        time.sleep(0.3)


class CliWatcher(threading.Thread):
    """后台轮询 root 名下的 CLI 进程命令行（用于取 `--resume <uuid>` 参数）。"""

    def __init__(self, root: int) -> None:
        super().__init__(daemon=True)
        self.root, self.cmds, self.stop = root, set[str](), threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            self.cmds |= {c for _, c in cli_procs(self.root)}
            time.sleep(0.2)


# ---------------- 身份与 Runtime 调用 ----------------

_tokens: dict[str, tuple[float, str]] = {}


def tok(user: str) -> str:
    """开发用户的访问令牌（60s 内复用，过期前重新登录）。"""
    at, t = _tokens.get(user, (0.0, ""))
    if time.monotonic() - at > 60:
        t = devkit.user_token(user)
        _tokens[user] = (time.monotonic(), t)
    return t


def auth(user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok(user)}"}


def parse_sse(r: httpx.Response) -> list[dict[str, Any]]:
    return [json.loads(line[5:].strip()) for line in r.iter_lines() if line.startswith("data:")]


def events(rt: httpx.Client, user: str, run_id: str) -> list[dict[str, Any]]:
    r = rt.get(f"/v1/runs/{run_id}/events", headers=auth(user))
    return list(r.json()) if r.status_code == 200 else []


def chat(rt: httpx.Client, user: str, agent: str, text: str, attachments: list[str] | None = None) -> dict[str, Any]:
    """流式对话：返回 {status, run_id, sse, log, messages}。"""
    messages = [{"role": "user", "content": text}]
    body: dict[str, Any] = {"agent_id": agent, "messages": messages, "stream": True}
    if attachments:
        body["attachments"] = attachments
    with rt.stream("POST", "/v1/messages", json=body, headers=auth(user)) as r:
        if r.status_code != 200:
            r.read()
            return {"status": r.status_code, "run_id": None, "sse": [], "log": [], "messages": messages}
        run_id = r.headers.get("x-run-id", "")
        sse = parse_sse(r)
    return {"status": 200, "run_id": run_id, "sse": sse, "log": events(rt, user, run_id), "messages": messages}


def resume(rt: httpx.Client, user: str, run_id: str, messages: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    with rt.stream("POST", f"/v1/runs/{run_id}/resume", json={"messages": messages}, headers=auth(user)) as r:
        if r.status_code != 200:
            r.read()
            return r.status_code, []
        return 200, parse_sse(r)


def of(log: list[dict[str, Any]], typ: str, **match: Any) -> list[dict[str, Any]]:
    return [e for e in log if e["type"] == typ and all(e["payload"].get(k) == v for k, v in match.items())]


def done_of(sse: list[dict[str, Any]]) -> dict[str, Any]:
    return next((e for e in reversed(sse) if e.get("type") == "done"), {})


def answer(sse: list[dict[str, Any]]) -> str:
    return "".join(e.get("delta", "") for e in sse if e.get("type") == "text")


def run_state(rt: httpx.Client, user: str, run_id: str) -> str:
    r = rt.get(f"/v1/runs/{run_id}", headers=auth(user))
    return str(r.json().get("state")) if r.status_code == 200 else f"HTTP {r.status_code}"


def calls(log: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    """该工具的 TOOL_REQUESTED 事件负载。"""
    return [e["payload"] for e in of(log, "tool.requested", tool=tool)]


def result_of(log: list[dict[str, Any]], call_id: str) -> dict[str, Any]:
    return next((e["payload"] for e in of(log, "tool.result", call_id=call_id)), {})


def until(desc: str, fn: Callable[[], dict[str, Any]], ok: Callable[[dict[str, Any]], bool], tries: int = 3) -> dict[str, Any]:
    """模型行为有随机性：前提不满足（如模型没调预期的工具）时重试，返回最后一次结果（前提仍不满足则由后续 check 失败）。"""
    res: dict[str, Any] = {}
    for i in range(tries):
        res = fn()
        if ok(res):
            return res
        print(f"  （{desc}：第 {i + 1} 次未满足前提，重试；run={res.get('run_id')}）")
    return res


def publish(rt: httpx.Client, agent_id: str, harness: str, tools: list[str], hooks: list[dict[str, Any]], system: str, **cfg: Any) -> int:
    """以 carol（platform-admin）登记并发布一个版本；返回发布的 HTTP 状态码。"""
    body = {
        "agent_id": agent_id,
        "version": "1",
        "model_alias": cfg.pop("model_alias", ALIAS),
        "harness": harness,
        "system_prompt": system,
        "config": {"tools": tools, "hooks": hooks} | cfg,
    }
    r = rt.post("/admin/agents", json=body, headers=auth("carol"))
    if r.status_code != 201:
        return r.status_code
    r = rt.post(f"/admin/agents/{agent_id}/versions/1/publish", json={"eval_passed": True}, headers=auth("carol"))
    if r.status_code != 200:
        print(f"    发布 {agent_id} → HTTP {r.status_code}：{r.text[:300]}")
    return r.status_code


# ---------------- Langfuse ----------------


def lf_traces(run_id: str | None, min_traces: int = 1, timeout_s: float = 120) -> list[dict[str, Any]]:
    """该 Run（Langfuse Session = run_id）的全部 Trace 详情（含 observations）；ingestion 异步，轮询等待。"""
    if not run_id:
        return []
    deadline = time.monotonic() + timeout_s
    details: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        r = devkit.langfuse("GET", "/api/public/traces", params={"sessionId": run_id, "limit": 50})
        ids = [t["id"] for t in r.json().get("data", [])] if r.status_code == 200 else []
        if len(ids) >= min_traces:
            details = []
            for tid in ids:
                d = devkit.langfuse("GET", f"/api/public/traces/{tid}")
                if d.status_code == 200:
                    details.append(d.json())
            if len(details) >= min_traces and all(d.get("observations") for d in details):
                return details
        time.sleep(3)
    return details


def lf_trace(trace_id: str, timeout_s: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = devkit.langfuse("GET", f"/api/public/traces/{trace_id}")
        if r.status_code == 200 and r.json().get("observations"):
            return dict(r.json())
        time.sleep(3)
    return {}


def find_attrs(node: Any, prefix: str, out: dict[str, str] | None = None) -> dict[str, str]:
    """递归找 observation metadata 中以 prefix 开头的属性（Langfuse 把 OTel span 属性放在 metadata 里，结构随版本略有差异）。"""
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.startswith(prefix) and not isinstance(v, dict):
                out[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            else:
                find_attrs(v, prefix, out)
    elif isinstance(node, list):
        for v in node:
            find_attrs(v, prefix, out)
    return out


def hook_attrs(traces: list[dict[str, Any]]) -> list[dict[str, str]]:
    """每个 observation 上的 ai.hook.* 属性（只保留非空的）。"""
    return [a for t in traces for o in t.get("observations", []) if (a := find_attrs(o.get("metadata"), "ai.hook."))]


def chat_obs(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in trace.get("observations", []) if str(o.get("name", "")).startswith("chat ")]


def redacted(o: dict[str, Any]) -> bool:
    """模型 span 只记元数据（ADR-0010）：有 ai.content_redacted 标记，且没有提示词 / 补全原文。"""
    flag = find_attrs(o.get("metadata"), "ai.content_redacted").get("ai.content_redacted", "")
    has_text = bool(find_attrs(o.get("metadata"), "gen_ai.prompt") or find_attrs(o.get("metadata"), "gen_ai.completion"))
    return flag.lower() == "true" and not has_text and not o.get("input") and not o.get("output")


# ---------------- 1. claude-sdk 实时探针子集（进程内适配器 + 真实 CLI） ----------------


async def live_probe() -> None:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    from ai_contract import (
        CardEvent,
        DoneEvent,
        HookSet,
        InputRequiredEvent,
        SessionSpec,
        TextEvent,
        ThinkingEvent,
        ToolResultEvent,
        ToolSpec,
        ToolUseEvent,
        TurnInput,
        UsageEvent,
    )
    from ai_harness_claude_sdk import ClaudeSdkHarness

    class RecordingClient(ClaudeSDKClient):
        def pid(self) -> int | None:
            """CLI 子进程 pid（SDK 内部属性，仅用于检查中断后子进程已回收）。"""
            process = getattr(getattr(self, "_transport", None), "_process", None)
            return getattr(process, "pid", None)

    clients: list[RecordingClient] = []

    def factory(options: ClaudeAgentOptions) -> RecordingClient:
        clients.append(RecordingClient(options=options))
        return clients[-1]

    h = ClaudeSdkHarness(client_factory=factory)
    echo = ToolSpec(
        name="echo",
        description="原样返回 text 参数。",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )

    def spec(system: str) -> SessionSpec:
        return SessionSpec(
            run_id="run_s5_live",
            agent_id="s5-live",
            agent_version="1",
            model_alias=ALIAS,
            system_prompt=system,
            subject="human:s5",
            workspace_dir=str(TMP / "live-ws" / uuid.uuid4().hex[:8]),
        )

    async def turn(handle: Any, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> list[Any]:
        return [ev async for ev in h.run_turn(handle, TurnInput(messages=messages), tools, [], HookSet())]

    def well_formed(evs: list[Any]) -> bool:
        """Done 唯一且最后、紧前是 Usage；无平台专属事件（工具结果 / 卡片 / 补充输入只能由平台产出）。"""
        return (
            len(evs) >= 2
            and isinstance(evs[-1], DoneEvent)
            and isinstance(evs[-2], UsageEvent)
            and sum(isinstance(e, DoneEvent) for e in evs) == 1
            and not any(isinstance(e, ToolResultEvent | CardEvent | InputRequiredEvent) for e in evs)
        )

    def usage_ok(evs: list[Any]) -> bool:
        u = next((e for e in evs if isinstance(e, UsageEvent)), None)
        return u is not None and u.input_tokens > 0 and u.output_tokens > 0

    def text_of(evs: list[Any]) -> str:
        return "".join(e.delta for e in evs if isinstance(e, TextEvent))

    print("== 1. claude-sdk 实时探针子集（真实 CLI，经 LiteLLM → DeepSeek）")
    handle = await h.open_session(spec("你是简洁的助手。"))
    evs = await turn(handle, [{"role": "user", "content": "用一句话介绍你自己。"}], [])
    print(f"  事件种类：{[e.type.value for e in evs][:6]}…（共 {len(evs)} 条）")
    check(
        well_formed(evs) and evs[-1].stop_reason == "end_turn" and bool(text_of(evs)),
        "事件流：文本 → Usage → Done(end_turn)，无平台专属事件",
    )
    check(usage_ok(evs), f"事件流：本轮用量 > 0（{next((e.model_dump() for e in evs if isinstance(e, UsageEvent)), None)}）")
    await h.interrupt(handle, "run_s5_live")

    # 工具回灌：nonce 只出现在平台给出的工具结果里，模型复述它说明结构化 tool_result 回到了模型
    system = "你必须先调用 echo 工具（参数 text 为用户给出的词），拿到工具结果后只回复工具返回的原文，不要任何其他内容。"
    ok_round = False
    for attempt in range(3):
        handle = await h.open_session(spec(system))
        history: list[dict[str, Any]] = [{"role": "user", "content": "请回显这个词：苹果"}]
        first = await turn(handle, history, [echo])
        uses = [e for e in first if isinstance(e, ToolUseEvent)]
        if len(uses) != 1 or first[-1].stop_reason != "tool_use":
            print(f"  （工具回灌：第 {attempt + 1} 次模型未调用 echo，重试）")
            await h.interrupt(handle, "run_s5_live")
            continue
        call, nonce = uses[0], "NONCE-" + uuid.uuid4().hex[:10]
        if text_of(first):
            history.append({"role": "assistant", "content": text_of(first)})
        history.append({"role": "assistant", "tool_use": {"call_id": call.call_id, "name": call.tool_name, "input": call.input}})
        history.append({"role": "tool", "call_id": call.call_id, "content": nonce})
        second = await turn(handle, history, [echo])
        await h.interrupt(handle, "run_s5_live")
        ok_round = nonce in text_of(second)
        if not ok_round and attempt < 2:
            print(f"  （工具回灌：第 {attempt + 1} 次回答未复述 nonce：{text_of(second)[:80]!r}，重试）")
            continue
        check(
            well_formed(first) and call.tool_name == "echo",
            "工具回灌：第一轮产出 ToolUse(echo) → Usage → Done(tool_use)，适配器不自行执行工具",
        )
        check(well_formed(second) and second[-1].stop_reason == "end_turn", "工具回灌：平台给出结果后第二轮以 Done(end_turn) 结束")
        check(ok_round, f"工具回灌：模型复述了只出现在工具结果里的 nonce（{nonce}）")
        check(not any(isinstance(e, ToolUseEvent) for e in second), "工具回灌：第二轮没有重新调用工具")
        check(usage_ok(first) and usage_ok(second), "工具回灌：两轮用量均 > 0")
        break
    else:
        check(False, "工具回灌：三次尝试模型均未调用 echo")

    # 平台工具名普遍带点（ticket.get / mail.send / ticket.close_batch）：同样必须能桥接到平台
    dotted = ToolSpec(
        name="ticket.get",
        description="按工单号查询工单详情",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    )
    dotted_uses: list[Any] = []
    for attempt in range(2):
        handle = await h.open_session(spec("你必须先调用 ticket.get 工具（参数 id 为用户给出的工单号），不要跳过工具调用。"))
        first = await turn(handle, [{"role": "user", "content": "查询工单 T-1001。"}], [dotted])
        await h.interrupt(handle, "run_s5_live")
        dotted_uses = [e for e in first if isinstance(e, ToolUseEvent)]
        if dotted_uses:
            break
        print(f"  （名称含点的工具：第 {attempt + 1} 次未产出 ToolUse，模型回答：{text_of(first)[:120]!r}）")
    check(
        bool(dotted_uses) and dotted_uses[0].tool_name == "ticket.get",
        f"工具回灌（名称含点的平台工具 ticket.get）：产出 ToolUse(ticket.get)（实际 {[e.tool_name for e in dotted_uses]}）",
    )

    # 中断：流式输出中、等待平台工具结果中两种情况，CLI 子进程均被回收
    handle = await h.open_session(spec("你是写作助手。"))
    got, streamed = asyncio.Event(), []

    async def consume() -> None:
        async for ev in h.run_turn(
            handle, TurnInput(messages=[{"role": "user", "content": "写一篇约 1500 字的散文，题目是《秋天》。"}]), [], [], HookSet()
        ):
            streamed.append(ev)
            if isinstance(ev, TextEvent | ThinkingEvent):
                got.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(got.wait(), 120)
    pid = clients[-1].pid()
    started = time.monotonic()
    await h.interrupt(handle, "run_s5_live")
    await asyncio.wait_for(task, 15)
    elapsed = time.monotonic() - started
    last = streamed[-1] if streamed else None
    check(
        isinstance(last, DoneEvent) and last.stop_reason in ("cancelled", "error") and elapsed < 5,
        f"中断（流式输出中）：{elapsed:.1f}s 内以 Done({getattr(last, 'stop_reason', None)}) 结束",
    )
    check(pid is not None and not pid_alive(pid, 5), f"中断（流式输出中）：CLI 子进程 {pid} 已回收")

    handle = await h.open_session(spec("你必须先调用 echo 工具（参数 text 为用户给出的词）。不要跳过工具调用。"))
    first = await turn(handle, [{"role": "user", "content": "请回显这个词：苹果"}], [echo])
    pid = clients[-1].pid()
    awaiting = first[-1].stop_reason == "tool_use" if first else False
    await h.interrupt(handle, "run_s5_live")
    check(awaiting and pid is not None and not pid_alive(pid, 5), f"中断（等待平台工具结果中）：CLI 子进程 {pid} 已回收")
    check(h._sessions == {}, "中断后适配器不再持有会话")
    check(not wait_no_cli(os.getpid(), 5), "探针结束后本进程名下无残留 CLI 子进程")


def pid_alive(pid: int, wait_s: float) -> bool:
    """等待至多 wait_s 秒，进程仍存在返回 True。"""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        if time.monotonic() > deadline:
            return True
        time.sleep(0.2)


# ---------------- 环境 ----------------


def ticket_data() -> Path:
    """mock 工单数据的临时副本：T-1001（alice 提交）描述开头插入一个随机手机号，作为 redact_pii 的真实来源。"""
    src = ROOT / "mcp-servers/ticket/src/mcp_ticket/data/tickets.json"
    tickets = json.loads(src.read_text(encoding="utf-8"))
    for t in tickets:
        if t["id"] == "T-1001":
            t["description"] = f"客户联系人王工，手机 {PHONE}。" + t["description"]
    dst = TMP / "tickets.json"
    dst.write_text(json.dumps(tickets, ensure_ascii=False), encoding="utf-8")
    return dst


def env_stack() -> dict[str, dict[str, str]]:
    certs = f"{devkit.REALM}/protocol/openid-connect/certs"
    return {
        "mcp": {
            "MCP_TICKET_JWKS_URL": certs,
            "MCP_TICKET_ISSUER": devkit.REALM,
            "MCP_TICKET_AUDIENCE": "mcp-ticket",
            "MCP_TICKET_DB": str(TMP / "mcp-ticket.db"),
            "MCP_TICKET_DATA": str(ticket_data()),
        },
        "gateway": {
            "KEYCLOAK_TOKEN_URL": devkit.TOKEN_URL,
            "GATEWAY_CLIENT_SECRET": "capability-gateway-dev-secret",
            "MCP_TICKET_URL": f"http://127.0.0.1:{PORTS['mcp']}",
            "POLICY_URL": "http://127.0.0.1:1",
        },
        "runtime": {
            "DATABASE_URL": f"sqlite+aiosqlite:///{TMP}/runtime.db",
            "WORKSPACE_DIR": str(TMP / "ws"),
            "HARNESS": "litellm",
            "POLICY_URL": "http://127.0.0.1:1",
            "CAPABILITY_GATEWAY_URL": f"http://127.0.0.1:{PORTS['gateway']}",
            "TOOL_DISPATCH": "gateway",
            "TOOL_RESULT_OFFLOAD_TOKENS": "300",
            "AUTH_REQUIRED": "true",
            "JWKS_URL": certs,
            "JWT_ISSUER": devkit.REALM,
            "JWT_AUDIENCE": "ai-runtime",
            "HARNESS_LOOP_HIGH_RISK_ALLOWLIST": APPROVE_AGENT,
            "CONFIDENTIAL_MODEL_ALIASES": ALIAS,
            "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
        },
    }


REDACT = {"point": "post_tool", "name": "redact_pii", "params": {}}
DENY_MAIL = {"point": "pre_tool", "name": "deny_tool_pattern", "params": {"patterns": ["mail.*"]}}
SUMMARY = {"point": "post_model", "name": "thinking_summary", "params": {"mode": "llm", "max_chars": 80, "model_alias": ALIAS}}
BUDGET = {"point": "pre_model", "name": "token_budget", "params": {"max_total_tokens": 50}}
HOOKS_SYSTEM = (
    "你是售后助手。需要工单信息时必须调用 ticket.get 查询；用户要求发邮件时必须直接调用 mail.send，不要先确认。"
    "工具被拒绝或返回脱敏内容时如实告诉用户，不要编造任何数据。"
)


# ---------------- 2. Hooks ----------------


def digest(r: dict[str, Any]) -> bool:
    """post_tool 改写输出后事件日志只留原文摘要（现为 raw_output_hmac；早期实现为 raw_output_sha256）。"""
    return bool(r.get("raw_output_hmac") or r.get("raw_output_sha256"))


def check_thinking(res: dict[str, Any], label: str) -> None:
    """summary 可见性：SSE 每轮（以 usage 事件分隔）至多一条 visibility=summary 的思维事件，与该轮 MODEL_RESPONSE 的
    thinking_summary 一致；有原始思维链的轮次必须有（llm 生成、不等于原文、不超过 max_chars 的）摘要。"""
    rounds, cur = [], []
    for ev in res["sse"]:
        if ev.get("type") == "thinking":
            cur.append(ev)
        elif ev.get("type") == "usage":
            rounds.append(cur)
            cur = []
    responses = [e["payload"] for e in of(res["log"], "model.response")]
    problems = [] if len(rounds) == len(responses) and not cur else [f"SSE 轮数 {len(rounds)} ≠ MODEL_RESPONSE {len(responses)}"]
    with_thinking = 0
    for i, (th, p) in enumerate(zip(rounds, responses, strict=False)):
        summary = p.get("thinking_summary")
        if [t.get("delta") for t in th] != ([summary] if summary else []) or any(t.get("visibility") != "summary" for t in th):
            problems.append(f"第 {i + 1} 轮 SSE 思维事件 {len(th)} 条，与摘要不一致")
        if p.get("thinking"):
            with_thinking += 1
            verdicts = [r.get("verdict") for r in p.get("hooks", []) if r.get("name") == "thinking_summary"]
            if not summary or summary == p["thinking"] or len(summary) > 80:
                problems.append(f"第 {i + 1} 轮有思维链但摘要不合规（hook 结论 {verdicts}）")
    print(f"  {label}：{len(rounds)} 轮，含思维链 {with_thinking} 轮，每轮 SSE 思维事件数 {[len(r) for r in rounds]}")
    check(with_thinking > 0 and not problems, f"{label}：thinking_summary(llm) 每轮只下发一条 summary 思维事件 {problems or ''}")


def hooks_suite(rt: httpx.Client, harness: str) -> dict[str, dict[str, Any]]:
    tag = "claude" if harness == "claude-sdk" else "litellm"
    agent, budget_agent = f"s5-hooks-{tag}", f"s5-budget-{tag}"
    print(f"== 2. Hooks（{harness}）")
    st = publish(rt, agent, harness, ["ticket.get", "mail.send"], [DENY_MAIL, REDACT, SUMMARY], HOOKS_SYSTEM, thinking_visibility="summary")
    check(st == 200, f"发布 {agent}（deny_tool_pattern + redact_pii + thinking_summary(llm)，thinking_visibility=summary）")
    st = publish(rt, budget_agent, harness, ["ticket.get"], [BUDGET, REDACT], "回答前必须先调用 ticket.get 查询用户提到的工单，再作答。")
    check(st == 200, f"发布 {budget_agent}（token_budget max_total_tokens=50）")
    out: dict[str, dict[str, Any]] = {}

    # deny_tool_pattern：mail.send 在 pre_tool 被拒，不进闸口、不执行
    res = until(
        "deny",
        lambda: chat(rt, "alice", agent, "请用 mail.send 给 ops@example.com 发一封邮件，主题“测试”，正文“hello”。"),
        lambda r: bool(calls(r["log"], "mail.send")),
    )
    out["deny"] = res
    mails = calls(res["log"], "mail.send")
    cids = {c["call_id"] for c in mails}
    hook_deny = all(any(h.get("name") == "deny_tool_pattern" and h.get("verdict") == "deny" for h in c.get("hooks", [])) for c in mails)
    decisions = [e["payload"] for e in of(res["log"], "tool.decision") if e["payload"]["call_id"] in cids]
    check(bool(mails) and hook_deny, f"deny：模型请求 mail.send（{len(mails)} 次），pre_tool 记录 deny_tool_pattern=deny")
    check(
        bool(decisions) and all(d["decision"] == "deny" and d["reason"] == "hook:deny_tool_pattern" for d in decisions),
        "deny：TOOL_DECISION 为 deny（reason=hook:deny_tool_pattern），未进闸口",
    )
    check(
        all(result_of(res["log"], c).get("status") == "denied" for c in cids)
        and not [e for e in res["log"] if e["type"] in ("tool.executing", "tool.executed") and e["payload"]["call_id"] in cids],
        "deny：TOOL_RESULT=denied，且没有 TOOL_EXECUTING / TOOL_EXECUTED",
    )
    check(
        any(e.get("type") == "tool_result" and e.get("status") == "denied" and e.get("call_id") in cids for e in res["sse"]),
        "deny：SSE 下发 tool_result(denied)",
    )

    # redact_pii：T-1001 描述里的手机号在 TOOL_RESULT、回答、事件日志中均已脱敏
    res = until(
        "redact",
        lambda: chat(rt, "alice", agent, "查询工单 T-1001 的详情，告诉我工单描述里客户联系人的手机号是多少。"),
        lambda r: any("1001" in json.dumps(c.get("input")) and result_of(r["log"], c["call_id"]) for c in calls(r["log"], "ticket.get")),
    )
    out["redact"] = res
    gets = [c for c in calls(res["log"], "ticket.get") if "1001" in json.dumps(c.get("input"))]
    results = [result_of(res["log"], c["call_id"]) for c in gets]
    ok_results = [r for r in results if r.get("status") == "ok"]
    dumped = [json.dumps(r.get("output"), ensure_ascii=False) for r in ok_results]
    check(
        bool(ok_results) and all("[REDACTED:phone]" in d and PHONE not in d for d in dumped),
        f"redact：ticket.get(T-1001) 的 TOOL_RESULT 含 [REDACTED:phone]、不含原始手机号（{len(ok_results)} 条）",
    )
    check(
        bool(ok_results)
        and all(
            digest(r) and any(h.get("name") == "redact_pii" and h.get("verdict") == "override" for h in r.get("hooks", []))
            for r in ok_results
        ),
        "redact：TOOL_RESULT 记 redact_pii=override 与原文摘要（raw_output_hmac；原文不落日志）",
    )
    check(PHONE not in answer(res["sse"]) and bool(answer(res["sse"])), "redact：回答中没有原始手机号")
    check(PHONE not in json.dumps(res["log"], ensure_ascii=False), "redact：整个 Run 事件日志中没有原始手机号")
    check_thinking(res, f"thinking({harness})")

    # token_budget：第一轮用量 0 放行，第二轮累计用量超过 50 被拒 → Run failed
    res = until(
        "budget",
        lambda: chat(rt, "alice", budget_agent, "查询工单 T-1002 的详情并总结。"),
        lambda r: len(of(r["log"], "model.request")) >= 2,
    )
    out["budget"] = res
    reqs = [e["payload"] for e in of(res["log"], "model.request")]
    verdicts = [[h.get("verdict") for h in p.get("hooks", []) if h.get("name") == "token_budget"] for p in reqs]
    errors = of(res["log"], "run.error", code="HOOK_DENIED")
    check(
        verdicts[:2] == [["allow"], ["deny"]], f"budget：MODEL_REQUEST 上 token_budget 结论依次为 {verdicts}（第一轮 allow、第二轮 deny）"
    )
    check(
        bool(errors) and errors[-1]["payload"].get("hook") == "token_budget" and errors[-1]["payload"].get("point") == "pre_model",
        "budget：RUN_ERROR HOOK_DENIED（hook=token_budget，point=pre_model）",
    )
    check(len(of(res["log"], "model.response")) == 1, "budget：被拒后没有再调用模型（只有一条 MODEL_RESPONSE）")
    check(
        done_of(res["sse"]).get("stop_reason") == "error" and run_state(rt, "alice", res["run_id"]) == "failed",
        "budget：SSE done(error)，Run 状态 failed",
    )
    return out


# ---------------- 3. claude-sdk 审批续跑 + 4. 第三人 403 ----------------


def approve_suite(rt: httpx.Client, rt_pid: int) -> dict[str, Any]:
    print("== 3. claude-sdk 审批续跑（高风险写）")
    system = (
        "你是售后助手。用户要求关闭工单时，直接调用 ticket.close_batch（ids 为工单号列表），不要先询问确认。工具返回后用一句话告知结果。"
    )
    st = publish(rt, "s5-claude-noallow", "claude-sdk", ["ticket.close_batch"], [REDACT], system)
    check(st == 409, f"未在 HARNESS_LOOP_HIGH_RISK_ALLOWLIST 中的 claude-sdk Agent 声明高风险写，发布被拒（HTTP {st}）")
    st = publish(rt, APPROVE_AGENT, "claude-sdk", ["ticket.close_batch"], [REDACT], system)
    check(st == 200, f"白名单内的 {APPROVE_AGENT}（claude-sdk + ticket.close_batch）发布成功")

    res = until(
        "approve",
        lambda: chat(rt, "alice", APPROVE_AGENT, "请关闭工单 T-1002。"),
        lambda r: any(e.get("type") == "card" for e in r["sse"]),
    )
    run_id, log = res["run_id"], res["log"]
    card: dict[str, Any] = next((e for e in res["sse"] if e.get("type") == "card"), {})
    payload = card.get("payload", {})
    call_id, phash = payload.get("call_id"), payload.get("params_hash")
    check(
        bool(card) and payload.get("tool") == "ticket.close_batch" and done_of(res["sse"]).get("stop_reason") == "awaiting",
        "高风险写挂起：SSE 下发影响预览卡片，done(awaiting)",
    )
    check(run_state(rt, "alice", run_id) == "auth-required", "Run 状态 auth-required")

    print("== 4. 安全遗留：续跑 / 读取 / 取消的身份限制")
    r = rt.get(f"/v1/runs/{run_id}/events", headers=auth("bob"))
    limited = r.json() if r.status_code == 200 else []
    check(
        r.status_code == 200 and bool(limited) and not any(e["type"] in ("model.response", "tool.result") for e in limited),
        "待审批期间有审批资格的 bob 只能读审批所需事件（不含模型输出 / 工具结果）",
    )
    body = {"call_id": call_id, "params_hash": phash, "comment": "s5"}
    check(
        rt.post(f"/v1/runs/{run_id}/approve", json=body, headers=auth("alice")).status_code == 403, "发起人 alice 不能审批自己的 Run（403）"
    )
    r = rt.post(f"/v1/runs/{run_id}/approve", json=body, headers=auth("carol"))
    check(r.status_code == 200, f"carol 审批通过（HTTP {r.status_code}）")
    check(rt.get(f"/v1/runs/{run_id}/events", headers=auth("bob")).status_code == 403, "第三人 bob（非发起人 / 审批人 / 管理员）读事件 403")
    check(rt.get(f"/v1/runs/{run_id}", headers=auth("bob")).status_code == 403, "第三人 bob 读 Run 403")
    status, _ = resume(rt, "bob", run_id, res["messages"])
    check(status == 403, f"第三人 bob 续跑 403（HTTP {status}）")
    check(rt.post(f"/v1/runs/{run_id}/cancel", headers=auth("bob")).status_code == 403, "第三人 bob 取消 403")
    dave = rt.get(f"/v1/runs/{run_id}/events", headers=auth("dave")).status_code
    check(dave == 404, f"其他租户 dave 读事件 404（不泄露存在性，HTTP {dave}）")
    status, _ = resume(rt, "carol", run_id, res["messages"])
    check(status == 403, f"审批人 carol 续跑 403（续跑只允许发起人，HTTP {status}）")

    print("== 3. （续）发起人续跑")
    watcher = CliWatcher(rt_pid)
    watcher.start()
    status, sse = resume(rt, "alice", run_id, res["messages"])
    watcher.stop.set()
    watcher.join()
    log = events(rt, "alice", run_id)
    check(status == 200 and done_of(sse).get("stop_reason") == "end_turn", f"alice 续跑：SSE done({done_of(sse).get('stop_reason')})")
    check(run_state(rt, "alice", run_id) == "completed", "续跑后 Run 状态 completed")
    executed = of(log, "tool.executed", call_id=call_id)
    executing = of(log, "tool.executing", call_id=call_id)
    check(
        len(executed) == 1 and len(executing) == 1, f"TOOL_EXECUTED 只出现一次（{len(executed)}），TOOL_EXECUTING 一次（{len(executing)}）"
    )
    check(len(calls(log, "ticket.close_batch")) == 1, "续跑后模型没有重新请求 close_batch（不重新决策）")
    result = result_of(log, call_id)
    check(result.get("status") == "ok" and "T-1002" in json.dumps(result.get("output")), f"close_batch 结果 ok（{result.get('output')}）")
    check(all("credential_subject" not in e["payload"] for e in executing), "发起人续跑不记 credential_subject（令牌主体即发起人）")
    resumed = [m.group(1) for c in watcher.cmds if (m := re.search(r"--resume[= ]([0-9a-f-]{36})", c))]
    # native_ref（SessionHandle 字段）不进事件日志，也没有查询接口；结构化续跑的直接证据是 CLI 以 --resume <uuid>
    # 载入平台合成的会话转录（适配器 native_ref = "transcript:<uuid>" 即该 uuid）。
    check(
        bool(resumed), f"结构化续跑：续跑期间 CLI 子进程以 --resume <uuid> 载入合成转录（{resumed[:2]}；native_ref 不进事件日志，以此为证）"
    )
    check(not wait_no_cli(rt_pid, 10), "续跑结束后 Runtime 名下无残留 CLI 子进程")
    return {"run_id": run_id, "traces": [done_of(res["sse"]).get("trace_id"), done_of(sse).get("trace_id")]}


def cancel_suite(rt: httpx.Client, rt_pid: int) -> str | None:
    """经 Runtime 取消一个进行中的 claude-sdk Run：第三人 403，发起人取消后 CLI 子进程回收。"""
    print("== 1/4. 经 Runtime 取消进行中的 claude-sdk Run")
    for attempt in range(3):
        body = {
            "agent_id": "s5-hooks-claude",
            "input": [{"role": "user", "content": "写一篇约 2000 字的文章，介绍售后工单处理的最佳实践。"}],
        }
        r = rt.post("/v1/runs", json=body, headers=auth("alice"))
        run_id = r.json().get("run_id") if r.status_code == 202 else None
        seen: list[tuple[int, str]] = []
        deadline = time.monotonic() + 60
        while run_id and time.monotonic() < deadline and not seen:
            seen = cli_procs(rt_pid)
            time.sleep(0.2)
        if not seen or run_state(rt, "alice", run_id or "") != "working":
            print(f"  （取消：第 {attempt + 1} 次未观察到进行中的 CLI 子进程，重试）")
            continue
        check(rt.post(f"/v1/runs/{run_id}/cancel", headers=auth("bob")).status_code == 403, "第三人 bob 取消进行中的 Run 403")
        r = rt.post(f"/v1/runs/{run_id}/cancel", headers=auth("alice"))
        check(r.status_code == 200, "发起人 alice 取消 200")
        left = wait_no_cli(rt_pid, 10)
        check(not left, f"取消后 10s 内 CLI 子进程 {[p for p, _ in seen]} 已回收（残留 {left}）")
        check(run_state(rt, "alice", run_id or "") == "canceled", "Run 状态 canceled")
        return run_id
    check(False, "取消：三次尝试均未观察到进行中的 CLI 子进程")
    return None


# ---------------- 4. 污点 ----------------


def taint_suite(rt: httpx.Client) -> dict[str, Any]:
    print("== 4. 机密工具 error 结果加污点；审批人 / 管理员续跑 403；篡改初始消息 409；续跑前后污点一致")
    system = "你是数据助手。严格按用户给出的步骤依次调用工具；每一步不论成功或失败都继续执行下一步，不要向用户确认。"
    st = publish(rt, "s5-taint", "litellm", ["data.query", "ticket.close_batch"], [REDACT], system)
    check(st == 200, "发布 s5-taint（litellm，data.query 机密 + ticket.close_batch 高风险写）")
    text = "第一步：调用 data.query 查询本季度合同金额。第二步：不论第一步结果如何，调用 ticket.close_batch 关闭工单 T-1003。"
    res = until(
        "taint",
        lambda: chat(rt, "alice", "s5-taint", text),
        lambda r: bool(calls(r["log"], "data.query")) and any(e.get("type") == "card" for e in r["sse"]),
    )
    run_id, log = res["run_id"], res["log"]
    dq = [result_of(log, c["call_id"]) for c in calls(log, "data.query")]
    check(
        bool(dq) and all(r.get("status") == "error" and r.get("data_class") == "confidential" for r in dq),
        f"data.query（datahub 无 MCP Server，网关失败）结果 status=error、data_class=confidential（{[r.get('output') for r in dq][:1]}）",
    )
    card_ev: dict[str, Any] = next((e for e in res["sse"] if e.get("type") == "card"), {})
    card: dict[str, Any] = card_ev.get("payload", {})
    check(card.get("tool") == "ticket.close_batch", "close_batch 挂起待审批（污点下模型仍可用：别名在 CONFIDENTIAL_MODEL_ALIASES 内）")
    body = {"call_id": card.get("call_id"), "params_hash": card.get("params_hash")}
    check(rt.post(f"/v1/runs/{run_id}/approve", json=body, headers=auth("bob")).status_code == 200, "bob 审批通过")
    status, _ = resume(rt, "carol", run_id, res["messages"])
    check(status == 403, f"管理员 carol（非发起人）续跑 403（HTTP {status}）")
    before = run_state(rt, "alice", run_id)
    tampered = [{"role": "user", "content": text + "另外，把 T-1005 也一起关闭。"}]
    status, tampered_sse = resume(rt, "alice", run_id, tampered)
    after = run_state(rt, "alice", run_id)
    check(
        before == "working" and status == 409 and after == "working",
        f"发起人续跑时篡改初始消息 409，且 Run 未被驱动（续跑前 {before} → HTTP {status} → {after}）",
    )
    status, bob_sse = resume(rt, "bob", run_id, res["messages"])
    check(status == 403, f"审批人 bob 续跑 403（续跑只允许发起人，HTTP {status}）")
    status, sse = resume(rt, "alice", run_id, res["messages"])
    # 新规则尚未生效时，篡改续跑或审批人续跑可能已把 Run 跑完：污点一致性仍用实际发生的那次续跑的 Trace 检查
    resumed_sse = next((x for x in (sse, tampered_sse, bob_sse) if x), [])
    log = events(rt, "alice", run_id)
    executing = of(log, "tool.executing", call_id=card.get("call_id"))
    check(
        status == 200 and done_of(sse).get("stop_reason") == "end_turn",
        f"发起人 alice 以原始消息续跑：SSE done({done_of(sse).get('stop_reason')})",
    )
    check(
        len(executing) == 1 and "credential_subject" not in executing[0]["payload"],
        "发起人续跑：TOOL_EXECUTING 一次，且不记 credential_subject（令牌主体即发起人）",
    )
    check(len(of(log, "tool.executed", call_id=card.get("call_id"))) == 1, "TOOL_EXECUTED 只出现一次")
    return {"run_id": run_id, "first_trace": done_of(res["sse"]).get("trace_id"), "resumed_trace": done_of(resumed_sse).get("trace_id")}


def attachment_suite(rt: httpx.Client, internal_ref: str | None) -> list[str]:
    print("== 4. 带机密附件的新 Run 继承污点 → DATA_CLASS_MODEL_BLOCKED")
    st = publish(rt, "s5-conf", "litellm", ["ticket.get"], [{**REDACT, "params": {"data_class": "confidential"}}], HOOKS_SYSTEM)
    check(st == 200, "发布 s5-conf（redact_pii 命中即升级为 confidential）")
    st = publish(rt, "s5-attach", "litellm", [], [], "你是助手。", model_alias=BLOCKED_ALIAS)
    check(st == 200, f"发布 s5-attach（模型别名 {BLOCKED_ALIAS}，不在 CONFIDENTIAL_MODEL_ALIASES 内）")

    def conf_ref(r: dict[str, Any]) -> str | None:
        return next(
            (
                e["payload"]["output_ref"]
                for e in of(r["log"], "tool.result", data_class="confidential")
                if e["payload"].get("output_ref") and e["payload"].get("status") == "ok"
            ),
            None,
        )

    res = until("conf", lambda: chat(rt, "alice", "s5-conf", "查询工单 T-1001 的详情并简要总结。"), lambda r: conf_ref(r) is not None)
    ref = conf_ref(res)
    check(ref is not None, f"alice 的 Run 产生机密工件（ticket.get 结果经 redact_pii 升级为 confidential 并外置：{ref}）")
    runs = [res["run_id"]]
    if ref:
        check(chat(rt, "bob", "s5-attach", "看看这个附件", attachments=[ref])["status"] == 403, "bob 引用 alice 的工件 403")
        blocked = chat(rt, "alice", "s5-attach", "请阅读附件并总结。", attachments=[ref])
        runs.append(blocked["run_id"])
        created = of(blocked["log"], "run.created")[0]["payload"] if of(blocked["log"], "run.created") else {}
        check(
            created.get("inherited_taint") == "confidential"
            and [a.get("data_class") for a in created.get("attachments", [])] == ["confidential"],
            f"RUN_CREATED 记附件分级 confidential 与 inherited_taint={created.get('inherited_taint')}",
        )
        check(
            bool(of(blocked["log"], "run.error", code="DATA_CLASS_MODEL_BLOCKED")) and not of(blocked["log"], "model.request"),
            "首轮即 DATA_CLASS_MODEL_BLOCKED，没有任何模型请求",
        )
        check(
            any(e.get("type") == "error" and e.get("code") == "DATA_CLASS_MODEL_BLOCKED" for e in blocked["sse"])
            and run_state(rt, "alice", blocked["run_id"]) == "failed",
            "SSE 下发 DATA_CLASS_MODEL_BLOCKED，Run failed",
        )
    if internal_ref:
        control = chat(rt, "alice", "s5-attach", "请阅读附件并用一句话总结。", attachments=[internal_ref])
        runs.append(control["run_id"])
        created = of(control["log"], "run.created")[0]["payload"] if of(control["log"], "run.created") else {}
        check(
            control["status"] == 200
            and created.get("inherited_taint") is None
            and [a.get("data_class") for a in created.get("attachments", [])] == ["internal"]
            and not of(control["log"], "run.error", code="DATA_CLASS_MODEL_BLOCKED")
            and bool(of(control["log"], "model.request")),
            "对照：内部分级附件不继承污点、不被拦截（照常发起模型请求）",
        )
    else:
        check(False, "对照：redact 用例未产生内部分级的外置工件")
    return [r for r in runs if r]


# ---------------- 主流程 ----------------


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # 被终止时同样走 finally，结束子进程
    print(f"临时目录 {TMP}；随机手机号 {PHONE[:3]}****{PHONE[-4:]}")
    asyncio.run(live_probe())
    e = env_stack()
    with (
        service("mcp-ticket", "mcp_ticket.main:app", "mcp-servers/ticket/src", PORTS["mcp"], e["mcp"]),
        service("gateway", "capability_gateway.main:app", "services/capability-gateway/src", PORTS["gateway"], e["gateway"]),
        service("runtime", "runtime.main:app", "services/runtime/src", PORTS["runtime"], e["runtime"]) as (runtime, rt_pid),
        httpx.Client(base_url=runtime, timeout=httpx.Timeout(30, read=600)) as rt,
    ):
        bad = publish(rt, "s5-badhook", "litellm", ["ticket.get"], [{"point": "pre_tool", "name": "my_custom_hook"}], "x")
        check(bad == 409, f"hooks 只允许平台内置命名 hook：未知 hook 发布 409（HTTP {bad}）")
        suites = {h: hooks_suite(rt, h) for h in ("litellm", "claude-sdk")}
        approval = approve_suite(rt, rt_pid)
        cancel_suite(rt, rt_pid)
        taint = taint_suite(rt)
        internal_ref = next(
            (
                ev["payload"]["output_ref"]
                for ev in of(suites["litellm"]["redact"]["log"], "tool.result", data_class="internal")
                if ev["payload"].get("output_ref")
            ),
            None,
        )
        attach_runs = attachment_suite(rt, internal_ref)
        artifacts = [p for p in (TMP / "ws").rglob("*.txt") if PHONE in p.read_text(encoding="utf-8", errors="ignore")]
        check(not artifacts, f"外置工件 / 附件副本中没有原始手机号（含手机号的文件 {len(artifacts)} 个）")

        print("== 5. Langfuse：Trace 与 hook 属性")
        all_runs: list[str] = [r for r in (approval["run_id"], taint["run_id"], *attach_runs) if r]
        for h, out in suites.items():
            deny_tr = lf_traces(out["deny"]["run_id"])
            redact_tr = lf_traces(out["redact"]["run_id"])
            budget_tr = lf_traces(out["budget"]["run_id"])
            all_runs += [out[k]["run_id"] for k in ("deny", "redact", "budget")]
            check(bool(deny_tr and redact_tr and budget_tr), f"{h}：deny / redact / budget 三个 Run 在 Langfuse 中均有 Trace")
            denied = [a for a in hook_attrs(deny_tr) if a.get("ai.hook.pre_tool.denied_by") == "deny_tool_pattern"]
            check(bool(denied), f"{h}：工具 span 上 ai.hook.pre_tool.denied_by=deny_tool_pattern")
            post_tool = [a for a in hook_attrs(redact_tr) if "redact_pii" in a.get("ai.hook.post_tool.names", "")]
            check(
                bool(post_tool) and any("override" in a.get("ai.hook.post_tool.verdicts", "") for a in post_tool),
                f"{h}：工具 span 上 ai.hook.post_tool.names 含 redact_pii、verdicts 含 override",
            )
            post_model = [a for a in hook_attrs(redact_tr) if "thinking_summary" in a.get("ai.hook.post_model.names", "")]
            check(bool(post_model), f"{h}：模型 span 上 ai.hook.post_model.names 含 thinking_summary")
            budget = [a for a in hook_attrs(budget_tr) if a.get("ai.hook.pre_model.denied_by") == "token_budget"]
            check(bool(budget), f"{h}：ai.hook.pre_model.denied_by=token_budget")
        expected = {t for t in approval["traces"] if t}
        tr = lf_traces(approval["run_id"], min_traces=2) if approval["run_id"] and len(expected) == 2 else []
        check(
            len(expected) == 2 and {t.get("id") for t in tr} >= expected,
            f"审批续跑 Run 的首跑与续跑两条 Trace 归入同一 Session（{len(tr)} 条）",
        )
        first, again = lf_trace(taint["first_trace"] or ""), lf_trace(taint["resumed_trace"] or "")
        first_chat, again_chat = chat_obs(first), chat_obs(again)
        print(f"  污点 Run：首跑模型 span 脱敏 {[redacted(o) for o in first_chat]}，续跑 {[redacted(o) for o in again_chat]}")
        check(any(redacted(o) for o in first_chat), "污点：首跑中机密 error 结果之后的模型 span 只记元数据（ai.content_redacted）")
        check(
            bool(again_chat) and all(redacted(o) for o in again_chat),
            "污点：续跑 Trace 的模型 span 同样只记元数据（重放重建的污点与实时一致）",
        )

        leaked = []
        for run_id in all_runs:
            for t in lf_traces(run_id, timeout_s=30):
                if PHONE in json.dumps(t, ensure_ascii=False):
                    leaked.append(run_id)
        check(not leaked, f"Langfuse：{len(all_runs)} 个 Run 的 Trace 中均没有原始手机号（泄露 {leaked}）")
        print(f"  日志：{TMP}")
    print("S5 端到端验证：" + ("全部通过" if not failures else f"{len(failures)} 项失败"))
    for f in failures:
        print(f"  - {f}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
