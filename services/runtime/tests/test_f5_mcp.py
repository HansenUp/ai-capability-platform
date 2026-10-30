"""Real temporary transactions through restricted MCP, no external services."""

import pytest
import test_f5_a2a as a2a_tests
from test_f5_a2a import evidence, headers, payload, send_body

from runtime.api import isolated_mcp as mcp


@pytest.fixture(name="idp")
def signed_provider(monkeypatch):
    yield from a2a_tests.signed_provider.__wrapped__(monkeypatch)


@pytest.fixture(name="lab")
def discovery_lab(client, monkeypatch):
    return a2a_tests.discovery_lab.__wrapped__(client, monkeypatch)


@pytest.fixture
def fixture(lab, monkeypatch):
    yield from a2a_tests.fixture.__wrapped__(lab, monkeypatch)


@pytest.fixture
def host(fixture, monkeypatch):
    from runtime.main import app

    if not any(getattr(r, "path", "") == mcp.PREFIX for r in app.routes):
        app.include_router(mcp.router)
    monkeypatch.setattr(app.state, "isolated_mcp", mcp.ENABLED, raising=False)
    return fixture


def h(idp, **claims):
    return headers(idp, **claims) | {
        "X-Platform-MCP-Profile": mcp.PROFILE,
        "MCP-Protocol-Version": mcp.VERSION,
        "Accept": "application/json, text/event-stream",
    }


def rpc(client, auth, method, params):
    return client.post(mcp.PREFIX, headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def call(client, auth, name, arguments):
    r = rpc(client, auth, "tools/call", {"name": name, "arguments": arguments})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_mcp_execute_recover_and_ack(host, idp):
    client, _, agent, queue = host
    auth = h(idp)
    assert rpc(client, auth, "initialize", {"protocolVersion": mcp.VERSION}).json()["result"]["protocolVersion"] == mcp.VERSION
    assert client.post(mcp.PREFIX, headers=auth, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    names = {t["name"] for t in rpc(client, auth, "tools/list", {}).json()["result"]["tools"]}
    assert names == set(mcp.SCHEMAS)
    catalog = call(client, auth, "agents_discover", {})["structuredContent"]
    assert any(item["agent_id"] == agent for item in catalog["items"])
    body = payload(agent)
    p = call(client, auth, "agent_prepare", body)["structuredContent"]
    assert p["taskId"] is None
    executed = call(client, auth, "agent_execute", send_body(body, p))["structuredContent"]
    assert len(queue) == 1
    assert call(client, auth, "agent_execute", send_body(body, p))["isError"]
    recovered = call(client, auth, "attempt_recover", {"attempt_id": body["attempt_id"]})["structuredContent"]
    assert recovered == executed["task"]["metadata"]["urn:ai-platform:isolated:workbench:1"]
    assert call(client, h(idp, sub="bob"), "attempt_recover", {"attempt_id": body["attempt_id"]})["isError"]
    assert call(client, auth, "attempt_withdraw", evidence(recovered))["isError"]
    assert call(client, auth, "attempt_ack", evidence(recovered))["structuredContent"]["receipt"]["acknowledged"]
    assert call(client, auth, "task_read", {"task_id": recovered["taskId"]})["structuredContent"]["id"] == recovered["taskId"]


def test_mcp_withdraw_shared_receipt_and_legacy_replay(host, idp):
    from runtime.api import isolated_a2a as a2a

    client, _, agent, queue = host
    auth, body = h(idp), payload(agent)
    p = call(client, auth, "agent_prepare", body)["structuredContent"]
    # A new HTTP request recovers even when the previous response was discarded.
    assert call(client, auth, "attempt_recover", {"attempt_id": body["attempt_id"]})["structuredContent"] == p
    terminal = call(client, auth, "attempt_withdraw", evidence(p))["structuredContent"]
    assert terminal["receipt"]["rejection"] == "OWNER_WITHDRAWN" and not queue
    assert client.post(a2a.PREFIX + "/message:send", headers=auth, json=send_body(body, p)).status_code == 409
    assert call(client, auth, "agent_prepare", payload(agent))["isError"]
    assert call(client, auth, "attempt_ack", evidence(terminal))["structuredContent"]["receipt"]["acknowledged"]
    assert call(client, auth, "attempt_recover", {"attempt_id": body["attempt_id"]})["structuredContent"]["receipt"]["acknowledged"]


def test_mcp_fail_closed_envelope_identity_and_origin(host, idp):
    client, _, _, _ = host
    auth = h(idp)
    for claims in ({"act": {}}, {"client_id": "service"}):
        assert rpc(client, h(idp, **claims), "tools/list", {}).status_code == 403
    assert rpc(client, auth | {"Origin": "https://foreign.invalid"}, "tools/list", {}).status_code == 403
    assert rpc(client, auth | {"MCP-Protocol-Version": "wrong"}, "tools/list", {}).status_code == 400
    assert rpc(client, auth | {"X-Platform-MCP-Profile": ""}, "tools/list", {}).status_code == 400
    assert rpc(client, auth, "tools/call", {"name": "unknown", "arguments": {}}).json()["error"]["code"] == -32602
    assert rpc(client, auth, "tools/call", {"name": "attempt_ack", "arguments": {}}).json()["error"]["code"] == -32602
    assert rpc(client, auth, "tasks/cancel", {}).json()["error"]["code"] == -32601
    assert rpc(client, auth, "initialize", {"protocolVersion": "wrong"}).json()["error"]["code"] == -32602
    assert client.post(mcp.PREFIX, headers=auth, json=[]).json()["error"]["code"] == -32600
    assert client.get(mcp.PREFIX, headers=auth).status_code == 405


def test_mcp_concurrent_execute_withdraw_share_one_terminal(host, idp):
    from concurrent.futures import ThreadPoolExecutor

    client, _, agent, queue = host
    auth, body = h(idp), payload(agent)
    prepared = call(client, auth, "agent_prepare", body)["structuredContent"]
    with ThreadPoolExecutor(2) as pool:
        execute = pool.submit(call, client, auth, "agent_execute", send_body(body, prepared))
        withdraw = pool.submit(call, client, auth, "attempt_withdraw", evidence(prepared))
        results = [execute.result(), withdraw.result()]
    assert sum(not result["isError"] for result in results) == 1
    terminal = call(client, auth, "attempt_recover", {"attempt_id": body["attempt_id"]})["structuredContent"]
    assert len(queue) == (1 if terminal["receipt"]["status"] == "COMMITTED" else 0)
    assert call(client, auth, "attempt_ack", evidence(terminal))["structuredContent"]["receipt"]["acknowledged"]


def test_mcp_requires_fixture_enablement(host, idp, monkeypatch):
    from runtime.main import app

    client, _, _, _ = host
    monkeypatch.setattr(app.state, "isolated_mcp", None)
    assert rpc(client, h(idp), "tools/list", {}).status_code == 503
