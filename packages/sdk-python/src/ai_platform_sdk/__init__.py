"""平台 Messages / Run API 的轻量客户端。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import TypeAdapter

from ai_contract import Event

_EVENT = TypeAdapter(Event)  # 用于解析 SSE 流中每一行 data: 后的 Event JSON


class PlatformClient:
    """封装对平台 `/v1/messages`、`/v1/runs` 等接口的调用，提供 chat（同步流式对话）与
    create_run/get_run/approve（异步运行 + 人工审批）两类使用方式。"""

    def __init__(
        self, base_url: str, token: str | None = None, subject: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if subject:
            headers["X-AI-Subject"] = subject  # 代表哪个主体（human/agent）发起调用
        self._c = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60, transport=transport)

    async def chat(self, agent_id: str, text: str, session_id: str | None = None) -> AsyncIterator[Event]:
        """以流式（SSE）方式与指定 agent 对话，逐条产出统一 Event。"""
        body = {"agent_id": agent_id, "session_id": session_id, "messages": [{"role": "user", "content": text}], "stream": True}
        async with self._c.stream("POST", "/v1/messages", json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    yield _EVENT.validate_python(json.loads(line[5:].strip()))

    async def create_run(self, agent_id: str, messages: list[dict[str, Any]]) -> str:
        """以异步模式创建一次运行（run），立即返回 run_id，不等待完成。"""
        r = await self._c.post("/v1/runs", json={"agent_id": agent_id, "input": messages, "mode": "async"})
        r.raise_for_status()
        return r.json()["run_id"]

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """查询指定 run 的当前状态与结果。"""
        r = await self._c.get(f"/v1/runs/{run_id}")
        r.raise_for_status()
        return r.json()

    async def approve(self, run_id: str, call_id: str, comment: str = "") -> dict[str, Any]:
        """对某次运行中挂起的工具调用（call_id）给予人工审批放行。"""
        r = await self._c.post(f"/v1/runs/{run_id}/approve", json={"call_id": call_id, "comment": comment})
        r.raise_for_status()
        return r.json()

    async def aclose(self) -> None:
        """关闭底层 httpx 客户端，释放连接资源。"""
        await self._c.aclose()
