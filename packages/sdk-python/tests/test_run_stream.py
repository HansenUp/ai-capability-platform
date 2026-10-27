import json

import httpx
import pytest

from ai_platform_sdk import PlatformClient


def frame(seq=1, run_id="r1", event="run.state_changed", event_id=None):
    value = {"run_id": run_id, "seq": seq, "type": event, "payload": {}, "ts": "2026-10-26T12:00:00Z"}
    return f"id: {seq if event_id is None else event_id}\nevent: {event}\ndata: {json.dumps(value)}\n\n"


DONE = 'event: done\ndata: {"type":"done","run_id":"r1","stop_reason":"awaiting"}\n\n'


async def consume(body, *, status=200, content_type="text/event-stream", since=0):
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/runs/r1/stream"
        assert request.url.params["since_seq"] == str(since)
        return httpx.Response(status, text=body, headers={"Content-Type": content_type})

    client = PlatformClient("http://synthetic", transport=httpx.MockTransport(handler))
    rows = []
    try:
        async for row in client.follow_run("r1", since):
            rows.append(row)
        return rows, seen
    finally:
        assert len(seen) == 1  # No reconnect, even for malformed frames or HTTP failures.
        await client.aclose()


async def test_read_persisted_events_and_awaiting_done():
    rows, seen = await consume(": heartbeat\n\n" + frame(3) + DONE, since=2)
    assert [row.seq for row in rows] == [3]
    assert seen[0].headers["accept"] == "text/event-stream"
    assert seen[0].headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "body",
    [
        frame(run_id="foreign") + DONE,
        frame(event_id="2") + DONE,
        frame() + frame() + DONE,
        frame() + DONE.replace('"r1"', '"foreign"'),
        frame() + DONE.replace('"awaiting"', '"unknown"'),
        frame(),
        "event: run.state_changed\ndata: invalid-json\n\n",
        "data: " + "x" * (1024 * 1024) + "\n\n",
    ],
)
async def test_invalid_or_disconnected_stream_fails_without_retry(body):
    with pytest.raises(ValueError):
        await consume(body)


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_permission_and_server_errors_are_not_retried(status):
    with pytest.raises(httpx.HTTPStatusError):
        await consume("private-body", status=status)


async def test_non_sse_response_rejected():
    with pytest.raises(ValueError, match="not SSE"):
        await consume("{}", content_type="application/json")


@pytest.mark.parametrize("cursor", [-1, True, 2**53 + 1])
async def test_invalid_cursor_does_not_request(cursor):
    def handler(request):
        pytest.fail("Invalid cursor must not send a request")

    client = PlatformClient("http://synthetic", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError):
            async for _ in client.follow_run("r1", cursor):
                pass
    finally:
        await client.aclose()


class FragmentedStream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.body), 3):
            yield self.body[offset : offset + 3]

    async def aclose(self):
        self.closed = True


async def test_fragmented_crlf_utf8_and_done_close_response():
    body = (frame().replace('"payload": {}', '"payload": {"text":"中文"}') + DONE).replace("\n", "\r\n").encode()
    stream = FragmentedStream(body)
    client = PlatformClient(
        "http://synthetic",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)),
    )
    try:
        rows = [row async for row in client.follow_run("r1")]
        assert rows[0].payload == {"text": "中文"}
        assert stream.closed
    finally:
        await client.aclose()


async def test_explicit_iterator_close_releases_response_without_next_request():
    stream = FragmentedStream((frame() + frame(2) + DONE).encode())
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    client = PlatformClient("http://synthetic", transport=httpx.MockTransport(handler))
    iterator = client.follow_run("r1")
    try:
        assert (await anext(iterator)).seq == 1
        await iterator.aclose()
        assert stream.closed
        assert len(requests) == 1
    finally:
        await iterator.aclose()
        await client.aclose()


async def test_cancelled_stream_releases_response():
    import asyncio

    entered = asyncio.Event()

    class WaitingStream(FragmentedStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

    stream = WaitingStream(b"")
    client = PlatformClient(
        "http://synthetic",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)),
    )
    iterator = client.follow_run("r1")
    task = asyncio.create_task(anext(iterator))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
    finally:
        await iterator.aclose()
        await client.aclose()
