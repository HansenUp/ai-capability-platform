"""ACP 用的 JSON-RPC 2.0 连接（stdio 上一行一个 JSON，即 ACP 的 NDJSON 传输）。

双向：本端既发请求（initialize / session/new / session/prompt …）也接收对端请求（session/request_permission 等）
与通知（session/update）。对端请求由 ``on_request`` 处理（返回 result 或抛 JsonRpcError），通知交给 ``on_notification``。
读循环在后台任务里运行；连接关闭（EOF / close()）时所有未完成的请求以 ConnectionClosed 失败。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]

METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


def as_object(value: Any) -> dict[str, Any]:
    """JSON 值是对象时原样返回，否则返回空对象（解析对端消息时用）。"""
    return value if isinstance(value, dict) else {}


class JsonRpcError(Exception):
    """对端返回的错误响应，或本端处理对端请求时要回给对端的错误。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}" + (f" ({data})" if data is not None else ""))
        self.code = code
        self.message = message
        self.data = data


class ConnectionClosed(ConnectionError):
    """连接已关闭（对端退出或本端 close()），请求不会再有响应。"""


async def _no_request(method: str, params: dict[str, Any]) -> Any:
    raise JsonRpcError(METHOD_NOT_FOUND, f"method not supported by client: {method}")


async def _no_notification(method: str, params: dict[str, Any]) -> None:
    return None


class JsonRpcConnection:
    """基于一对 asyncio 流的 JSON-RPC 2.0 连接。"""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_request: RequestHandler = _no_request,
        on_notification: NotificationHandler = _no_notification,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.on_request = on_request
        self.on_notification = on_notification
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._read_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """启动后台读循环（只调用一次）。"""
        if self._read_task is None:
            self._read_task = asyncio.get_running_loop().create_task(self._read_loop())

    @property
    def closed(self) -> bool:
        return self._closed

    async def _write(self, msg: dict[str, Any]) -> None:
        if self._closed:
            raise ConnectionClosed("json-rpc connection closed")
        try:
            self._writer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
            await self._writer.drain()
        except (OSError, RuntimeError) as exc:
            raise ConnectionClosed(f"json-rpc write failed: {exc}") from exc

    async def request(self, method: str, params: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        """发请求并等待响应；返回 result，错误响应抛 JsonRpcError，超时抛 TimeoutError，连接关闭抛 ConnectionClosed。"""
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if timeout_s is None:
                return await fut
            async with asyncio.timeout(timeout_s):
                return await fut
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """发通知（无响应）。"""
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    return
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("acp: dropping non-JSON line from agent: %r", line[:200])
                    continue
                if not isinstance(msg, dict):
                    continue
                self._dispatch(msg)
        except (OSError, asyncio.IncompleteReadError, ValueError) as exc:
            log.warning("acp: read loop stopped: %s", exc)
        finally:
            self._fail_pending()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if isinstance(method, str):
            params = as_object(msg.get("params"))
            if "id" in msg:
                self._spawn(self._answer(msg["id"], method, params))
            else:
                self._spawn(self.on_notification(method, params))
            return
        rid = msg.get("id")
        fut = self._pending.get(rid) if isinstance(rid, int) else None
        if fut is None or fut.done():
            return
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            fut.set_exception(JsonRpcError(int(err.get("code", INTERNAL_ERROR)), str(err.get("message", "")), err.get("data")))
        else:
            fut.set_result(msg.get("result"))

    def _spawn(self, coro: Awaitable[None]) -> None:
        async def run() -> None:
            try:
                await coro
            except Exception:
                log.exception("acp: handler failed")

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, rid: Any, method: str, params: dict[str, Any]) -> None:
        try:
            result = await self.on_request(method, params)
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "result": result}
        except JsonRpcError as exc:
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": INTERNAL_ERROR, "message": f"{type(exc).__name__}: {exc}"}}
        with contextlib.suppress(ConnectionClosed):
            await self._write(reply)

    def _fail_pending(self) -> None:
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionClosed("agent closed the ACP connection"))

    async def close(self) -> None:
        """关闭连接：停止读循环、取消处理中的对端请求、令未完成请求失败。"""
        self._fail_pending()
        tasks = [t for t in [self._read_task, *self._tasks] if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(OSError, RuntimeError):
            self._writer.close()
