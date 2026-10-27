"""平台 Messages / Run API 的轻量客户端。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter

from ai_contract import Event

from .run_stream import persisted_events
from .types_runtime import RunEvent

_EVENT: TypeAdapter[Event] = TypeAdapter(Event)  # 用于解析 SSE 流中每一行 data: 后的 Event JSON
_RUN_EVENTS = TypeAdapter(list[RunEvent])  # 用 OpenAPI 生成类型解析 Run 事件日志


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
        return cast(str, r.json()["run_id"])

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """查询指定 run 的当前状态与结果。"""
        r = await self._c.get(f"/v1/runs/{run_id}")
        r.raise_for_status()
        return cast(dict[str, Any], r.json())

    async def run_events(self, run_id: str, since_seq: int = 0) -> list[RunEvent]:
        """按 seq 增量拉取 Run 事件日志（自 since_seq 之后），解析为由 OpenAPI 生成的 RunEvent 类型。"""
        r = await self._c.get(f"/v1/runs/{run_id}/events", params={"since_seq": since_seq})
        r.raise_for_status()
        return _RUN_EVENTS.validate_python(r.json())

    async def follow_run(self, run_id: str, since_seq: int = 0) -> AsyncIterator[RunEvent]:
        """Follow persisted events for one Run with a single GET; never reconnect or execute a write.

        The caller owns the last yielded sequence and may explicitly start a new read after
        disconnection. SSE done ends this iterator; it does not acknowledge any write.
        """
        if not run_id.strip() or type(since_seq) is not int or not 0 <= since_seq <= 2**53:
            raise ValueError("Invalid Run ID or read cursor")
        async with self._c.stream(
            "GET",
            f"/v1/runs/{quote(run_id.strip(), safe='')}/stream",
            params={"since_seq": since_seq},
            headers={"Accept": "text/event-stream", "Cache-Control": "no-store"},
        ) as response:
            async for row in persisted_events(response, run_id.strip(), since_seq):
                yield row

    async def approve(self, run_id: str, call_id: str, params_hash: str, comment: str = "", ttl_seconds: int = 900) -> dict[str, Any]:
        """对某次运行中挂起的工具调用（call_id）给予人工审批放行。

        params_hash 必填：取自审批卡片（CardEvent.payload.params_hash）。审批绑定具体参数，
        指纹与待审批调用不一致时 Runtime 返回 409；ttl_seconds 为审批有效期，过期后需重新审批。"""
        body = {"call_id": call_id, "params_hash": params_hash, "comment": comment, "ttl_seconds": ttl_seconds}
        r = await self._c.post(f"/v1/runs/{run_id}/approve", json=body)
        r.raise_for_status()
        return cast(dict[str, Any], r.json())

    async def aclose(self) -> None:
        """关闭底层 httpx 客户端，释放连接资源。"""
        await self._c.aclose()
