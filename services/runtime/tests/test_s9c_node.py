"""S9 WP-C：节点代理——服务令牌、注册 / 心跳重注册、拉取 reload_skills 指令 → 物化 → 回执，吊销随心跳刷新。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from test_s9c_helpers import FakeGateway, Signer, build, tgz, write_jwks

from runtime import gateway_client
from runtime.node import agent as node_agent
from runtime.node import settings as node_settings
from runtime.skills import store

CONSOLE = "http://console.test:8081"
TOKEN_URL = "http://kc.test:8180/realms/ai-platform/protocol/openid-connect/token"  # noqa: S105 —— 令牌端点地址


class FakeConsole:
    def __init__(self) -> None:
        self.commands: list[dict] = []
        self.acks: list[tuple[str, dict]] = []
        self.registered: set[str] = set()
        self.auth: list[str | None] = []
        self.fail_ack = False
        self.token_requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "kc.test":
            self.token_requests.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"access_token": "node-jwt", "expires_in": 300})
        self.auth.append(request.headers.get("Authorization"))
        path = request.url.path
        if path == "/nodes/register":
            self.registered.add(json.loads(request.content)["node_id"])
            return httpx.Response(200, json={"ok": True})
        node = path.split("/")[2]
        if path.endswith("/heartbeat"):
            return httpx.Response(200 if node in self.registered else 404, json={})
        if path.endswith("/commands"):
            return httpx.Response(200, json=[c for c in self.commands if c["id"] not in {a[0] for a in self.acks}])
        if path.endswith("/ack"):
            if self.fail_ack:
                return httpx.Response(503)
            self.acks.append((path.split("/")[4], json.loads(request.content)))
            return httpx.Response(200, json={})
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path, monkeypatch):
    signer = Signer()
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("SKILL_TRUST_KEYS_FILE", str(write_jwks(tmp_path / "trust.json", signer)))
    monkeypatch.setenv("CONSOLE_URL", CONSOLE)
    monkeypatch.delenv("NODE_TOKEN_URL", raising=False)
    monkeypatch.setenv("NODE_CONSOLE_TOKEN", "dev-node-token")
    gw, console = FakeGateway(), FakeConsole()
    gateway_client.set_transport_for_tests(gw.transport())
    node_agent.set_transport_for_tests(httpx.MockTransport(console.handler))
    store.revocations.reset()
    yield SimpleNamespace(signer=signer, gw=gw, console=console, tmp=tmp_path)
    gateway_client.set_transport_for_tests(None)
    node_agent.set_transport_for_tests(None)
    store.revocations.reset()


async def test_tick_materializes_and_acks(env):
    pkg, sha = build(env.signer)
    env.gw.packages[("demo-skill", "1.0.0")] = tgz(pkg)
    env.console.commands = [
        {"id": "c1", "type": "reload_skills", "args": {"skills": [{"name": "demo-skill", "version": "1.0.0", "manifest_sha256": sha}]}},
        {"id": "c2", "type": "graceful_restart", "args": {}},
        {
            "id": "c3",
            "type": "reload_skills",
            "args": {"skills": [{"name": "demo-skill", "version": "1.0.0", "manifest_sha256": "0" * 64}]},
        },  # noqa: E501
    ]
    a = node_agent.NodeAgent(node_id="n-test")
    await a.tick()
    acks = dict(env.console.acks)
    assert acks["c1"]["status"] == "ok" and (env.tmp / "skills" / "demo-skill" / "1.0.0" / "SKILL.md").exists()
    assert acks["c2"]["status"] == "unsupported"
    assert acks["c3"]["status"] == "error" and acks["c3"]["detail"]["skills"][0]["code"] == "SKILL_VERIFY_FAILED"
    assert set(env.console.auth) == {"Bearer dev-node-token"}
    assert "GET /skills/revocations" in env.gw.calls  # 吊销列表随心跳刷新
    assert "n-test" in env.console.registered


async def test_reregister_on_404_and_no_reexecute_after_failed_ack(env, monkeypatch):
    pkg, sha = build(env.signer)
    env.gw.packages[("demo-skill", "1.0.0")] = tgz(pkg)
    env.console.commands = [
        {"id": "c1", "type": "reload_skills", "args": {"skills": [{"name": "demo-skill", "version": "1.0.0", "manifest_sha256": sha}]}}
    ]
    a = node_agent.NodeAgent(node_id="n2")
    a.registered = True  # Console 重启丢失注册：心跳 404 → 重新注册
    env.console.fail_ack = True
    calls = []
    real = a.execute

    async def counting(cmd):
        calls.append(cmd["id"])
        return await real(cmd)

    monkeypatch.setattr(a, "execute", counting)
    await a.tick()
    assert "n2" in env.console.registered and env.console.acks == []
    env.console.fail_ack = False
    await a.poll_commands()  # 回执失败后指令仍在队列：只重发回执，不重复执行
    assert calls == ["c1"] and env.console.acks[0][0] == "c1"


async def test_service_token_client_credentials(env, monkeypatch):
    monkeypatch.setenv("NODE_TOKEN_URL", TOKEN_URL)
    monkeypatch.setenv("NODE_CLIENT_SECRET", "s3cret")
    monkeypatch.delenv("NODE_CONSOLE_TOKEN")
    a = node_agent.NodeAgent(node_id="n3")
    await a.register()
    await a.heartbeat()
    assert env.console.auth == ["Bearer node-jwt", "Bearer node-jwt"]
    assert len(env.console.token_requests) == 1 and env.console.token_requests[0]["client_id"] == "ai-node-svc"


async def test_start_and_prod_problems(monkeypatch):
    monkeypatch.delenv("CONSOLE_URL", raising=False)
    assert await node_agent.start() is None
    ns = node_settings.NodeSettings(console_url=CONSOLE, token_url=None, client_secret=None, static_token="x")  # noqa: S106
    probs = node_settings.problems(ns, "prod")
    assert len(probs) == 2 and node_settings.problems(ns, "dev") == []
    monkeypatch.setenv("CONSOLE_URL", CONSOLE)
    monkeypatch.setenv("NODE_POLL_INTERVAL_S", "3600")
    app = SimpleNamespace(state=SimpleNamespace())
    agent = await node_agent.start(app)
    assert agent is not None and app.state.node_agent is agent
    await agent.stop()
