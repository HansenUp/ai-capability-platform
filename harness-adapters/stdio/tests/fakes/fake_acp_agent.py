"""CI 用的假 ACP agent（仿 goose 1.54 的行为；不调用任何真实模型 / 二进制）。

    python fake_acp_agent.py      # stdio 上讲 ACP；由 ACP 桥以 ``--profile cmd -- python fake_acp_agent.py`` 启动

行为：``initialize`` 报告 HTTP MCP 能力；``session/new`` 连接注入的 HTTP MCP（initialize → tools/list）；
``session/prompt`` 跑一个 agent 循环：经 ``ACP_OPENAI_BASE_URL`` 调模型（流式），模型要工具时先
``session/request_permission``，获准后经 MCP ``tools/call``（``_meta.agent-tool-call-request-id`` 带模型侧 id），
再把结果交回模型，直到模型不再要工具；``session/cancel`` 立即中止进行中的模型 / 工具请求，prompt 以 cancelled 结束。

越界开关（环境变量，用于隔离违规用例）：
- ``ACP_AGENT_ROGUE_TOOL=1``：模型请求里多带一个非平台工具 ``shell``；
- ``ACP_AGENT_EXTRA_MODEL_CALL=1``：模型答完后自行再调一次模型（harness 侧多轮）；
- ``ACP_AGENT_SELF_TOOL=1``：不经模型请求，自行调用第一个平台工具；
- ``ACP_AGENT_ROGUE_PERMISSION=1``：为一个不存在的工具调用请求权限。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any

import httpx

FLAG = {
    k: os.environ.get(k) == "1"
    for k in ("ACP_AGENT_ROGUE_TOOL", "ACP_AGENT_EXTRA_MODEL_CALL", "ACP_AGENT_SELF_TOOL", "ACP_AGENT_ROGUE_PERMISSION")
}


class Agent:
    def __init__(self) -> None:
        self.out_lock = asyncio.Lock()
        self.next_id = 0
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(600.0), trust_env=False)
        self.base = os.environ["ACP_OPENAI_BASE_URL"]
        self.key = os.environ["ACP_OPENAI_API_KEY"]

    async def write(self, msg: dict[str, Any]) -> None:
        async with self.out_lock:
            sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self.next_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = fut
        await self.write({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        return await fut

    async def notify_update(self, sid: str, update: dict[str, Any]) -> None:
        await self.write({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": update}})

    # ---- MCP client ----

    async def mcp(self, s: dict[str, Any], method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> Any:
        s["mcp_id"] += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            msg["id"] = s["mcp_id"]
        headers = {**s["mcp_headers"], "accept": "application/json, text/event-stream"}
        if s.get("mcp_session"):
            headers["mcp-session-id"] = s["mcp_session"]
        resp = await self.http.post(s["mcp_url"], json=msg, headers=headers)
        if notify:
            return None
        if resp.headers.get("mcp-session-id"):
            s["mcp_session"] = resp.headers["mcp-session-id"]
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"mcp error: {data['error']}")
        return data["result"]

    # ---- ACP handlers ----

    async def on_initialize(self, params: dict[str, Any]) -> Any:
        return {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": False, "mcpCapabilities": {"http": True, "sse": False}},
            "agentInfo": {"name": "fake-acp", "version": "0.1.0"},
        }

    async def on_session_new(self, params: dict[str, Any]) -> Any:
        sid = uuid.uuid4().hex
        servers = params.get("mcpServers") or []
        if len(servers) != 1 or servers[0].get("type") != "http":
            raise ValueError("fake agent expects exactly one HTTP MCP server")
        srv = servers[0]
        s: dict[str, Any] = {
            "mcp_url": srv["url"],
            "mcp_headers": {h["name"]: h["value"] for h in srv.get("headers") or []},
            "mcp_id": 0,
            "server": srv["name"],
            "history": [],
            "task": None,
        }
        await self.mcp(
            s, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fake", "version": "1"}}
        )
        await self.mcp(s, "notifications/initialized", notify=True)
        s["tools"] = (await self.mcp(s, "tools/list"))["tools"]
        self.sessions[sid] = s
        return {"sessionId": sid}

    async def on_prompt(self, params: dict[str, Any]) -> Any:
        sid = params["sessionId"]
        s = self.sessions[sid]
        text = "".join(p.get("text", "") for p in params.get("prompt") or [])
        s["history"].append({"role": "user", "content": text})
        task = s["task"] = asyncio.current_task()
        try:
            return {"stopReason": await self.loop(sid, s)}
        except asyncio.CancelledError:
            if task is not None and task.cancelling():
                task.uncancel()
            return {"stopReason": "cancelled"}
        finally:
            s["task"] = None

    async def loop(self, sid: str, s: dict[str, Any]) -> str:
        if FLAG["ACP_AGENT_SELF_TOOL"] and s["tools"]:
            await self.mcp(s, "tools/call", {"name": s["tools"][0]["name"], "arguments": {}})
        rounds = 0
        while True:
            rounds += 1
            text, calls = await self.model(sid, s)
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            s["history"].append(msg)
            if not calls:
                if FLAG["ACP_AGENT_EXTRA_MODEL_CALL"] and rounds == 1:
                    s["history"].append({"role": "user", "content": "keep going"})
                    continue
                return "end_turn"
            for c in calls:
                tool = c["function"]["name"].removeprefix(s["server"] + "__")
                perm_id = "ghost-call" if FLAG["ACP_AGENT_ROGUE_PERMISSION"] else c["id"]
                perm = await self.request(
                    "session/request_permission",
                    {
                        "sessionId": sid,
                        "toolCall": {"toolCallId": perm_id, "title": c["function"]["name"], "kind": "other", "status": "pending"},
                        "options": [
                            {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                        ],
                    },
                )
                outcome = perm.get("outcome") or {}
                if outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow_once":
                    res = await self.mcp(
                        s,
                        "tools/call",
                        {
                            "name": tool,
                            "arguments": json.loads(c["function"]["arguments"] or "{}"),
                            "_meta": {"agent-tool-call-request-id": c["id"]},
                        },
                    )
                    content = "".join(p.get("text", "") for p in res.get("content") or [])
                else:
                    content = "permission denied"
                s["history"].append({"role": "tool", "tool_call_id": c["id"], "content": content})

    async def model(self, sid: str, s: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": f"{s['server']}__{t['name']}",
                    "description": t.get("description", ""),
                    "parameters": t["inputSchema"],
                },
            }
            for t in s["tools"]
        ]
        if FLAG["ACP_AGENT_ROGUE_TOOL"]:
            tools.append(
                {"type": "function", "function": {"name": "shell", "description": "run a shell command", "parameters": {"type": "object"}}}
            )
        body = {
            "model": "whatever",
            "messages": [{"role": "system", "content": "fake agent system prompt"}, *s["history"]],
            "tools": tools,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        text: list[str] = []
        bufs: dict[int, dict[str, Any]] = {}
        async with self.http.stream(
            "POST", f"{self.base}/chat/completions", json=body, headers={"Authorization": f"Bearer {self.key}"}
        ) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"model returned {resp.status_code}: {(await resp.aread())[:300]!r}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                chunk = json.loads(line[5:])
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("content"):
                        text.append(d["content"])
                        await self.notify_update(
                            sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": d["content"]}}
                        )
                    for tc in d.get("tool_calls") or []:
                        b = bufs.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        b["id"] = b["id"] or tc.get("id") or ""
                        fn = tc.get("function") or {}
                        b["name"] = b["name"] or fn.get("name") or ""
                        b["args"] += fn.get("arguments") or ""
        calls = [
            {"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": b["args"]}} for _, b in sorted(bufs.items())
        ]
        return "".join(text), calls

    async def on_cancel(self, params: dict[str, Any]) -> None:
        s = self.sessions.get(params.get("sessionId", ""))
        if s and s["task"] is not None:
            s["task"].cancel()

    # ---- dispatch ----

    async def handle(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method is None:
            fut = self.pending.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(msg.get("result") or {})
            return
        params = msg.get("params") or {}
        handlers = {"initialize": self.on_initialize, "session/new": self.on_session_new, "session/prompt": self.on_prompt}
        if "id" not in msg:
            if method == "session/cancel":
                await self.on_cancel(params)
            return
        try:
            if method not in handlers:
                raise LookupError(method)
            result = await handlers[method](params)
            await self.write({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except Exception as exc:
            await self.write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}})


async def main() -> None:
    agent = Agent()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    tasks: set[asyncio.Task[None]] = set()
    while True:
        line = await reader.readline()
        if not line:
            break
        if line.strip():
            t = loop.create_task(agent.handle(json.loads(line)))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
    for t in tasks:
        t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
