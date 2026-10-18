"""mock 连接器公共骨架：健康检查、manifest、调用审计（供 e2e 断言上游收到的身份与 act 链）。

每个 mock 暴露：
- ``GET /healthz``：存活探针；``GET /tools``：manifest（满足写规范，见 ``mcp_template.spec``）；
- ``GET /_mock/calls``：最近的工具调用审计（工具、sub、租户、OBO 受众、act 链、断言是否验签、run_id / call_id），
  ``POST /_mock/reset`` 清空审计（mail 另清空发件箱）。``/_mock/*`` 只用于本机 e2e 断言，不鉴权——mock 只绑 127.0.0.1。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from mcp_template.auth import Identity, identity_dependency
from mcp_template.idempotency import now_iso

MAX_CALLS = 1000  # 审计环形缓冲上限


class MockConnector:
    """一个 mock 连接器：FastAPI 应用 + 鉴权依赖（环境变量前缀 ``prefix``）+ 调用审计。"""

    def __init__(self, name: str, prefix: str, audience: str, manifest: list[dict[str, Any]]) -> None:
        self.name = name
        self.manifest = manifest
        self.identity = identity_dependency(prefix, default_audience=audience)
        self._calls: deque[dict[str, Any]] = deque(maxlen=MAX_CALLS)
        self._lock = threading.Lock()
        self._reset_hooks: list[Callable[[], None]] = []
        self.app = FastAPI(title=f"MCP Server · {name} (mock)", version="0.1.0")
        self._routes()

    def _routes(self) -> None:
        app = self.app

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            """存活探针。"""
            return {"status": "ok"}

        @app.get("/tools")
        async def tools() -> list[dict[str, Any]]:
            """工具清单（manifest）。"""
            return self.manifest

        @app.get("/_mock/calls")
        async def calls() -> list[dict[str, Any]]:
            """最近的工具调用审计（时间正序）。"""
            with self._lock:
                return list(self._calls)

        @app.post("/_mock/reset")
        async def reset() -> dict[str, bool]:
            """清空审计与 mock 状态。"""
            with self._lock:
                self._calls.clear()
            for hook in self._reset_hooks:
                hook()
            return {"reset": True}

    def on_reset(self, hook: Callable[[], None]) -> None:
        """登记 ``POST /_mock/reset`` 时额外执行的清理。"""
        self._reset_hooks.append(hook)

    def record(self, tool: str, ident: Identity) -> None:
        """记一条调用审计。"""
        entry = {
            "tool": tool,
            "sub": ident.sub,
            "tenant": ident.tenant,
            "roles": sorted(ident.roles),
            "audience": ident.audience,
            "azp": ident.azp,
            "act": ident.act,
            "act_chain": list(ident.act_chain),
            "act_verified": ident.act_verified,
            "run_id": ident.run_id,
            "call_id": ident.call_id,
            "at": now_iso(),
        }
        with self._lock:
            self._calls.append(entry)
