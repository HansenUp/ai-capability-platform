import json

import httpx
import pytest
from test_workbench_reader import ATTEMPT, IDENTITY, ROW, SCOPE

from ai_platform_sdk.workbench_session import UnknownWorkbenchWrite, WorkbenchSession


@pytest.mark.parametrize("action", ["credential", "approve", "reject", "answer"])
@pytest.mark.parametrize("lost", [False, True])
async def test_existing_actions_bind_prepare_execute_ack_without_retry(action, lost):
    state = {"receipt": None, "active": None, "posts": []}

    def handler(request):
        path = request.url.path
        if request.method == "GET":
            if path.endswith("identity"):
                return httpx.Response(200, json=IDENTITY)
            if path.endswith("attempts"):
                return httpx.Response(200, json={"scope_id": SCOPE, "active": state["active"]})
            return httpx.Response(200, json=state["receipt"])
        body = json.loads(request.content)
        state["posts"].append((path, body))
        if path.endswith("/attempts"):
            assert body["action"] == action and body["run_id"] == "run"
            row = {**ROW, "action": action}
            if action != "credential":
                assert body["call_id"] == "call"
                row["call_id"] = "call"
            if action == "answer":
                assert "value" in body and body["value"] is None
                row["value_hmac"] = "c" * 64
            elif action in {"approve", "reject"}:
                assert body["params_hash"] == "original-hash"
                row["params_hash"] = "original-hash"
            else:
                assert set(body) == {"run_id", "action", "attempt_id"}
        elif path.endswith("/execute"):
            expected = {"fingerprint": "b" * 64, "revision": 1}
            if action == "answer":
                expected["value"] = None
            assert body == expected
            row = {**state["receipt"], "status": "COMMITTED", "revision": 2, "evidence_seq": 2}
        else:
            assert body == {"fingerprint": "b" * 64, "revision": 2}
            row = {**state["receipt"], "acknowledged": True, "revision": 3}
        state["receipt"] = row
        state["active"] = None if row["acknowledged"] else row
        if lost and path.endswith("/execute"):
            raise httpx.ReadError("synthetic execute response loss")
        return httpx.Response(200, json=row)

    sdk = WorkbenchSession("http://synthetic", "synthetic", httpx.MockTransport(handler))
    try:
        if action == "credential":
            await sdk.prepare_credential("run", ATTEMPT)
        elif action == "answer":
            await sdk.prepare_answer("run", "call", None, ATTEMPT)
        else:
            await sdk.prepare_approval("run", "call", "original-hash", action == "approve", ATTEMPT)
        assert len(state["posts"]) == 1
        if lost:
            with pytest.raises(UnknownWorkbenchWrite):
                await sdk.execute(None) if action == "answer" else await sdk.execute()
            assert (await sdk.recover()).status == "COMMITTED"
            with pytest.raises(ValueError):
                await sdk.execute(None) if action == "answer" else await sdk.execute()
        else:
            await sdk.execute(None) if action == "answer" else await sdk.execute()
        assert len(state["posts"]) == 2
        assert (await sdk.acknowledge()).acknowledged
        assert len(state["posts"]) == 3 and sdk.pending is None
    finally:
        await sdk.aclose()


async def test_non_json_answer_rejected_before_any_request():
    def handler(request):
        pytest.fail("Non-JSON answer must not access identity or write")

    sdk = WorkbenchSession("http://synthetic", "synthetic", httpx.MockTransport(handler))
    try:
        with pytest.raises(ValueError):
            await sdk.prepare_answer("run", "call", float("nan"), ATTEMPT)
    finally:
        await sdk.aclose()
