"""受管节点的可逆排空与回执后的退出请求，不发送任意进程信号。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse


class NodeLifecycle:
    def __init__(self, background: Any, request_exit: Callable[[], None] | None, *, durable: bool) -> None:
        self.background = background
        self.request_exit = request_exit
        self.durable = durable
        self.accepting = True
        self.active_requests = 0
        self.http_idle = asyncio.Event()
        self.http_idle.set()
        self.prepared = False

    def resume(self) -> None:
        self.accepting = True
        self.prepared = False
        if self.background.worker is not None:
            self.background.worker.paused = False

    async def prepare(self, timeout_s: float, notify: Callable[[], Awaitable[None]]) -> None:
        from runtime.state import db

        database = db.engine.url
        if database.database in (None, "", ":memory:") or database.query.get("mode") == "memory":
            raise ValueError("restart requires a persistent Runtime database")
        if self.request_exit is None or not self.durable:
            raise ValueError("restart requires the managed launcher and a durable Redis queue")
        self.accepting = False
        if self.background.worker is not None:
            self.background.worker.paused = True
        try:
            async with asyncio.timeout(timeout_s):
                await notify()
                await self.http_idle.wait()
                if self.background.worker is not None:
                    await self.background.worker.idle.wait()
            self.prepared = True
        except BaseException:
            self.resume()
            raise

    def commit(self) -> None:
        if not self.prepared or self.request_exit is None:
            raise ValueError("node has not drained")
        self.request_exit()


class AdmissionMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        lifecycle = getattr(getattr(scope.get("app"), "state", None), "node_lifecycle", None)
        controlled = scope["type"] == "http" and scope.get("path") != "/healthz"
        if lifecycle is None or not controlled:
            await self.app(scope, receive, send)
            return
        if not lifecycle.accepting:
            response = JSONResponse({"detail": "node is draining", "code": "NODE_DRAINING"}, status_code=503, headers={"Retry-After": "1"})
            await response(scope, receive, send)
            return
        lifecycle.active_requests += 1
        lifecycle.http_idle.clear()
        try:
            await self.app(scope, receive, send)
        finally:
            lifecycle.active_requests -= 1
            if lifecycle.active_requests == 0:
                lifecycle.http_idle.set()
