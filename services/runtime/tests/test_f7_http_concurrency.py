"""Pure ASGI substitutes: no sockets, worker operations or external model calls."""

import asyncio
from types import SimpleNamespace

import pytest

from runtime.node.lifecycle import AdmissionMiddleware, NodeLifecycle


@pytest.mark.asyncio
async def test_http_sse_slots_lowering_and_drain_precedence():
    worker = SimpleNamespace(paused=False)
    life = NodeLifecycle(SimpleNamespace(worker=worker), None, durable=False)
    life.set_http_concurrency_limit(2)
    entered = asyncio.Queue()
    releases = {name: asyncio.Event() for name in ("/sse", "/http")}
    statuses = {}

    async def app(scope, receive, send):
        path = scope["path"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if path in releases:
            await entered.put(path)
            await releases[path].wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    middleware = AdmissionMiddleware(app)

    async def request(path):
        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            if message["type"] == "http.response.start":
                statuses[path] = message["status"]

        await middleware({"type": "http", "path": path, "app": SimpleNamespace(state=SimpleNamespace(node_lifecycle=life))}, receive, send)

    tasks = [asyncio.create_task(request(path)) for path in releases]
    try:
        assert {await entered.get(), await entered.get()} == set(releases)
        assert life.active_requests == 2
        life.set_http_concurrency_limit(1)
        assert not worker.paused and life.accepting
        await request("/rejected")
        assert statuses["/rejected"] == 429 and life.active_requests == 2
        await request("/healthz")
        assert statuses["/healthz"] == 200 and life.active_requests == 2
        releases["/http"].set()
        await tasks[1]
        assert life.active_requests == 1 and not life.http_idle.is_set()
        await request("/still-full")
        assert statuses["/still-full"] == 429
        life.close_admission()
        await request("/closed")
        assert statuses["/closed"] == 503
        releases["/sse"].set()
        await tasks[0]
        assert life.active_requests == 0 and life.http_idle.is_set()
        life.resume()
        await request("/available")
        assert statuses["/available"] == 200 and life.http_concurrency_limit == 1
    finally:
        for event in releases.values():
            event.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_cancelled_request_releases_exactly_one_slot():
    life = NodeLifecycle(SimpleNamespace(worker=None), None, durable=False)
    life.set_http_concurrency_limit(1)
    entered = asyncio.Event()

    async def app(scope, receive, send):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        AdmissionMiddleware(app)(
            {"type": "http", "path": "/sse", "app": SimpleNamespace(state=SimpleNamespace(node_lifecycle=life))}, None, None
        )
    )
    await entered.wait()
    assert life.active_requests == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert life.active_requests == 0 and life.http_idle.is_set()


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "1", None])
def test_invalid_limit_does_not_change_admission(value):
    life = NodeLifecycle(SimpleNamespace(worker=None), None, durable=False)
    life.set_http_concurrency_limit(2)
    with pytest.raises(ValueError):
        life.set_http_concurrency_limit(value)
    assert life.http_concurrency_limit == 2 and life.accepting
