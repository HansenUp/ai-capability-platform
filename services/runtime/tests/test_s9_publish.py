"""S9 admin.publish 的 Skill 锁（契约 ②，D143）：TOOL_DISPATCH=gateway 时经能力网关 POST /skills/resolve 校验 Skill、
冻结 skills_lock 到版本快照（迁移 0005），风险等级以网关判定为准；网关拒绝 / 不可达 / 应答不合规一律 409（失败关闭）；
进程内开发执行器（TOOL_DISPATCH=local）不经网关、不冻结锁。"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from ai_contract import HarnessCapabilities, SessionHandle
from runtime.api import admin
from runtime.harness import registry
from runtime.loop import toolbroker
from runtime.state import db

SHA = "a" * 64


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Callable[[dict[str, Any]], httpx.Response]], list[dict[str, Any]]]]:
    """把发布切到 TOOL_DISPATCH=gateway，并以给定函数应答 /skills/resolve（其余网关路径 503：组装器回退本地工具表）。"""
    monkeypatch.setattr(admin, "settings", dataclasses.replace(admin.settings, tool_dispatch="gateway"))
    seen: list[dict[str, Any]] = []

    def install(resolve: Callable[[dict[str, Any]], httpx.Response]) -> list[dict[str, Any]]:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path != "/skills/resolve":
                return httpx.Response(503)
            body = json.loads(req.content)
            seen.append(body)
            return resolve(body)

        toolbroker.set_gateway_transport_for_tests(httpx.MockTransport(handler))
        return seen

    yield install
    toolbroker.set_gateway_transport_for_tests(None)


def _lock(body: dict[str, Any]) -> list[dict[str, str]]:
    return [{"name": s["name"], "version": s["version"], "manifest_sha256": SHA} for s in body["skills"]]


def _create(client: Any, config: dict[str, Any], harness: str | None = None) -> str:
    name = f"s9-pub-{uuid.uuid4().hex[:6]}"
    body: dict[str, Any] = {"agent_id": name, "version": "1", "config": config}
    if harness:
        body["harness"] = harness
    assert client.post("/admin/agents", json=body).status_code == 201
    return name


def _publish(client: Any, name: str) -> Any:
    return client.post(f"/admin/agents/{name}/versions/1/publish", json={"eval_passed": True})


async def _lock_of(name: str) -> Any:
    async with db.SessionLocal() as s:
        av = (await s.execute(select(db.AgentVersion).where(db.AgentVersion.agent_id == name))).scalars().one()
        return av.skills_lock


def test_publish_freezes_skills_lock_from_gateway(client: Any, gateway: Any) -> None:
    seen = gateway(lambda body: httpx.Response(200, json={"lock": _lock(body), "risk_level": "read"}))
    cfg = {"tools": ["echo", "ticket.search"], "skills": [{"name": "ticket-summary", "version": "1.0.0"}]}
    name = _create(client, cfg)
    r = _publish(client, name)
    assert r.status_code == 200, r.text
    assert r.json()["skills_lock"] == [{"name": "ticket-summary", "version": "1.0.0", "manifest_sha256": SHA}]
    assert seen == [
        {
            "agent_id": name,
            "tenant": "default",
            "skills": [{"name": "ticket-summary", "version": "1.0.0"}],
            "tools": ["echo", "ticket.search"],
            "discovery_scope": [],
        }
    ]
    import asyncio

    assert asyncio.run(_lock_of(name)) == r.json()["skills_lock"]


def test_publish_without_skills_still_resolves_risk_and_freezes_empty_lock(client: Any, gateway: Any) -> None:
    gateway(lambda body: httpx.Response(200, json={"lock": [], "risk_level": "write_low"}))
    r = _publish(client, _create(client, {"tools": ["mail.send"]}))
    assert r.status_code == 200 and r.json()["skills_lock"] == []


@pytest.mark.parametrize(
    ("response", "fragment", "code"),
    [
        (
            httpx.Response(409, json={"detail": "skill ticket-summary@9 is not listed", "code": "SKILL_NOT_LISTED"}),
            "not listed",
            "SKILL_NOT_LISTED",
        ),
        (
            httpx.Response(409, json={"detail": "capability crm.write not granted", "code": "SKILL_CAPABILITY_NOT_GRANTED"}),
            "not granted",
            "SKILL_CAPABILITY_NOT_GRANTED",
        ),
        (httpx.Response(500, text="boom"), "HTTP 500", "CONFLICT"),
        (httpx.Response(200, json={"lock": [], "risk_level": "read"}), "malformed", "CONFLICT"),  # 锁与声明的 Skill 不一致
        (httpx.Response(200, json={"lock": [{"name": "ticket-summary", "version": "9"}], "risk_level": "read"}), "malformed", "CONFLICT"),
        (httpx.Response(200, json={"lock": [], "risk_level": "extreme"}), "malformed", "CONFLICT"),
    ],
)
def test_publish_fails_closed_on_gateway_rejection_or_malformed(client: Any, gateway: Any, response: Any, fragment: str, code: str) -> None:
    gateway(lambda body: response)
    name = _create(client, {"tools": ["echo"], "skills": [{"name": "ticket-summary", "version": "9"}]})
    r = _publish(client, name)
    assert r.status_code == 409 and fragment in r.json()["detail"] and r.json()["code"] == code, r.text
    assert (
        client.get("/admin/agents").json()
        and next(a for a in client.get("/admin/agents").json() if a["agent_id"] == name)["status"] == "draft"
    )


def test_publish_fails_closed_when_gateway_unreachable(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin, "settings", dataclasses.replace(admin.settings, tool_dispatch="gateway"))
    r = _publish(client, _create(client, {"tools": ["echo"]}))  # CAPABILITY_GATEWAY_URL 指向不可达地址
    assert r.status_code == 409 and "unreachable" in r.json()["detail"]


class _L0Harness:
    """harness 自持循环、只有 advisory 拦截的适配器（L0）。"""

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(name="s9-l0", version="1", transport="in-process", loop_owner="harness", tool_interception="advisory")

    async def open_session(self, spec: Any) -> SessionHandle:
        return SessionHandle(session_id="x", harness="s9-l0")


def test_risk_level_comes_from_gateway_not_local_tools(client: Any, gateway: Any) -> None:
    registry.register("s9-l0", _L0Harness())  # type: ignore[arg-type]
    gateway(lambda body: httpx.Response(200, json={"lock": _lock(body), "risk_level": "write_high"}))
    # 本地工具表里 echo 只读，但网关判定（含 Skill 声明能力）为高风险写：L0 harness 发布被拒
    r = _publish(client, _create(client, {"tools": ["echo"]}, harness="s9-l0"))
    assert r.status_code == 409 and "write_high" in r.json()["detail"]
    # 反之本地工具表里 ticket.close_batch 为高风险写，但以网关判定（read）为准
    gateway(lambda body: httpx.Response(200, json={"lock": [], "risk_level": "read"}))
    r = _publish(client, _create(client, {"tools": ["ticket.close_batch"]}, harness="s9-l0"))
    assert r.status_code == 200, r.text


def test_local_dispatch_does_not_call_gateway_or_freeze_lock(client: Any) -> None:
    assert admin.settings.tool_dispatch == "local"
    name = _create(client, {"tools": ["echo"], "skills": [{"name": "ticket-summary"}]})
    r = _publish(client, name)
    assert r.status_code == 200 and "skills_lock" not in r.json()
    import asyncio

    assert asyncio.run(_lock_of(name)) is None


def test_skill_verify_failure_fails_run_closed(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """组装时 Skill 验签失败 / 已吊销（WP-C，契约 ⑤）：Run 以对应错误码失败，不调用模型。"""
    from runtime.loop import runner
    from runtime.skills.verify import SkillVerifyError

    async def revoked(*_: Any, **__: Any) -> Any:
        raise SkillVerifyError("SKILL_REVOKED", "ticket-summary@1 is revoked")

    monkeypatch.setattr(runner, "assemble", revoked)
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": [{"role": "user", "content": "hi"}]}) as r:
        run_id = r.headers["x-run-id"]
        evs = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
    assert [e["code"] for e in evs if e["type"] == "error"] == ["SKILL_REVOKED"] and evs[-1]["stop_reason"] == "error"
    log = client.get(f"/v1/runs/{run_id}/events").json()
    assert "model.request" not in [e["type"] for e in log] and client.get(f"/v1/runs/{run_id}").json()["state"] == "failed"
