import json

import httpx
import pytest
from test_workbench_reader import ATTEMPT, IDENTITY, SCOPE

from ai_platform_sdk.workbench_session import UnknownWorkbenchWrite, WorkbenchSession


def session(*, lost=None, foreign=False):
    state = {"active": None, "receipt": None, "posts": []}

    def handler(request):
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/identity"):
                return httpx.Response(200, json={**IDENTITY, "scope_id": "f" * 64} if foreign else IDENTITY)
            if path.endswith("/attempts"):
                return httpx.Response(200, json={"scope_id": SCOPE, "active": state["active"]})
            return httpx.Response(200, json=state["receipt"])
        state["posts"].append(path)
        body = json.loads(request.content)
        if path.endswith("/create-attempts"):
            row = {
                "scope_id": SCOPE,
                "attempt_id": body["attempt_id"],
                "action": "create",
                "run_id": "reserved",
                "request_seq": 0,
                "fingerprint": "b" * 64,
                "status": "PREPARED",
                "revision": 1,
                "evidence_seq": None,
                "rejection": None,
                "acknowledged": False,
                "value_hmac": "c" * 64,
                "create_agent": body["agent_id"],
                "create_version": "1",
                "create_model": "mock",
                "create_snapshot": "d" * 64,
                "create_reason": "only",
            }
        elif path.endswith("/execute"):
            assert body == {"fingerprint": "b" * 64, "revision": 1, "value": "original"}
            row = {**state["receipt"], "status": "COMMITTED", "evidence_seq": 1, "revision": 2}
        else:
            assert body == {"fingerprint": "b" * 64, "revision": 2}
            row = {**state["receipt"], "acknowledged": True, "revision": 3}
        state["receipt"] = row
        state["active"] = None if row["acknowledged"] else row
        if lost and path.endswith(lost):
            raise httpx.ReadError("synthetic lost response")
        return httpx.Response(200, json=row)

    return WorkbenchSession("http://synthetic", "synthetic", httpx.MockTransport(handler)), state


async def test_explicit_prepare_execute_ack_never_automatically_chain():
    client, state = session()
    try:
        prepared = await client.prepare_create("demo", "original", ATTEMPT)
        assert prepared.status == "PREPARED" and len(state["posts"]) == 1
        with pytest.raises(ValueError):
            await client.acknowledge()
        committed = await client.execute("original")
        assert committed.status == "COMMITTED" and len(state["posts"]) == 2
        with pytest.raises(ValueError):
            await client.prepare_create("demo", "other", ATTEMPT)
        assert (await client.acknowledge()).acknowledged
        assert client.pending is None and len(state["posts"]) == 3
    finally:
        await client.aclose()


@pytest.mark.parametrize("lost", ["create-attempts", "execute", "acknowledge"])
async def test_lost_response_recovery_never_resends_business_write(lost):
    client, state = session(lost=lost)
    try:
        if lost == "create-attempts":
            with pytest.raises(UnknownWorkbenchWrite):
                await client.prepare_create("demo", "original", ATTEMPT)
        else:
            await client.prepare_create("demo", "original", ATTEMPT)
            if lost == "execute":
                with pytest.raises(UnknownWorkbenchWrite):
                    await client.execute("original")
            else:
                await client.execute("original")
                with pytest.raises(UnknownWorkbenchWrite):
                    await client.acknowledge()
        count = len(state["posts"])
        with pytest.raises(ValueError):
            await client.prepare_create("demo", "other", ATTEMPT)
        recovered = await client.recover()
        assert len(state["posts"]) == count
        if lost == "create-attempts":
            assert recovered.status == "PREPARED"
            await client.execute("original")
        elif lost == "execute":
            assert recovered.status == "COMMITTED"
            with pytest.raises(ValueError):
                await client.execute("original")
        else:
            assert recovered.acknowledged
            client.confirm_closed()
            assert client.pending is None
    finally:
        await client.aclose()


async def test_empty_ledger_does_not_unlock_unknown_request():
    client, state = session(lost="execute")
    try:
        await client.prepare_create("demo", "original", ATTEMPT)
        with pytest.raises(UnknownWorkbenchWrite):
            await client.execute("original")
        state["active"] = None
        with pytest.raises(ValueError, match="conflict"):
            await client.recover()
        with pytest.raises(ValueError):
            await client.execute("original")
        assert len(state["posts"]) == 2
    finally:
        await client.aclose()


async def test_prepared_readback_does_not_authorize_resending_unknown_execute():
    client, state = session(lost="execute")
    try:
        prepared = await client.prepare_create("demo", "original", ATTEMPT)
        with pytest.raises(UnknownWorkbenchWrite):
            await client.execute("original")
        state["receipt"] = prepared.model_dump(exclude_none=True) | {"evidence_seq": None, "rejection": None}
        state["active"] = state["receipt"]
        assert (await client.recover()).status == "PREPARED"
        with pytest.raises(ValueError):
            await client.execute("original")
        assert len(state["posts"]) == 2
    finally:
        await client.aclose()


async def test_identity_conflict_never_sends_prepare():
    client, state = session(foreign=True)
    try:
        with pytest.raises(ValueError):
            await client.prepare_create("demo", "original", ATTEMPT)
        assert state["posts"] == []
    finally:
        await client.aclose()


async def test_concurrent_prepare_and_cancellation_preserve_unknown_lock():
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    client, state = session()
    inner = client._client._transport

    class DelayedPost(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.method == "POST":
                entered.set()
                await release.wait()
            return await inner.handle_async_request(request)

        async def aclose(self):
            await inner.aclose()

    client._client._transport = DelayedPost()
    task = asyncio.create_task(client.prepare_create("demo", "original", ATTEMPT))
    try:
        await entered.wait()
        with pytest.raises(ValueError):
            await client.prepare_create("demo", "other", ATTEMPT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ValueError):
            await client.prepare_create("demo", "other", ATTEMPT)
        assert state["posts"] == []
    finally:
        release.set()
        await client.aclose()
