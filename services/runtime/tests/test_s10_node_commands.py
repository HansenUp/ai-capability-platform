import secrets
from unittest.mock import AsyncMock

import httpx

from runtime.node.agent import NodeAgent
from runtime.node.settings import NodeSettings


async def test_reload_node_config_is_atomic_and_does_not_change_security_scope():
    credential = secrets.token_urlsafe()
    agent = NodeAgent(NodeSettings(console_url="http://console.test", static_token=credential, poll_interval_s=30, timeout_s=10))
    original = agent.ns
    response = await agent.execute({"type": "reload_config", "args": {"poll_interval_s": 5.0, "timeout_s": 2.0}})
    assert response == {"status": "ok", "detail": {"scope": "node_agent", "poll_interval_s": 5.0, "timeout_s": 2.0}}
    assert agent.ns.console_url == original.console_url and agent.ns.static_token == original.static_token
    assert credential not in str(response)
    before = agent.ns
    for arguments in (
        {"console_url": "http://evil.test"},
        {"poll_interval_s": 0},
        {"timeout_s": None},
        {"timeout_s": "5"},
        {"timeout_s": float("nan")},
        {"timeout_s": float("inf")},
        {"poll_interval_s": True},
        {"poll_interval_s": 3601.0},
        {"poll_interval_s": 10.0, "timeout_s": -1.0},
        [],
        None,
    ):
        response = await agent.execute({"type": "reload_config", "args": arguments})
        assert response["status"] == "error" and agent.ns is before


async def test_ack_failure_preserves_order_and_does_not_reapply_config(monkeypatch):
    agent = NodeAgent(NodeSettings())
    commands = [{"id": "first", "type": "reload_config", "args": {"poll_interval_s": 5.0}}, {"id": "second", "type": "unknown", "args": {}}]
    fail_ack = True

    async def call(method, path, json=None):
        request = httpx.Request(method, "http://console.test" + path)
        if method == "GET":
            return httpx.Response(200, json=commands, request=request)
        return httpx.Response(503 if fail_ack else 200, request=request)

    monkeypatch.setattr(agent, "_call", call)
    execute = AsyncMock(wraps=agent.execute)
    monkeypatch.setattr(agent, "execute", execute)
    await agent.poll_commands()
    assert execute.await_count == 1 and list(agent._done) == ["first"]
    first_settings = agent.ns
    fail_ack = False
    responses = await agent.poll_commands()
    assert [response["id"] for response in responses] == ["first", "second"]
    assert execute.await_count == 2 and agent.ns is first_settings and agent._done == {}
