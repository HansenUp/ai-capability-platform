"""S5 WP-A 发布闸门：版本快照 config.hooks 校验（D73，不合规 409）与 harness 持有循环的高风险写白名单（§4-1，D79）。"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from ai_contract import HarnessCapabilities
from runtime.api import admin
from runtime.harness import registry


class _CapsOnly:
    """只用于发布校验的 harness：只声明能力，不会被驱动。"""

    def __init__(self, name: str, **caps: Any) -> None:
        self._caps = HarnessCapabilities(name=name, version="t", transport="in-process", **caps)

    def capabilities(self) -> HarnessCapabilities:
        return self._caps


@pytest.fixture(scope="module", autouse=True)
def harnesses() -> None:
    registry.register(
        "s5g-harness-l2", _CapsOnly("s5g-harness-l2", loop_owner="harness", tool_interception="enforcing", can_resume_from_events=True)
    )
    registry.register("s5g-platform", _CapsOnly("s5g-platform", loop_owner="platform"))


def _publish(client: Any, harness: str, config: dict[str, Any], agent_id: str | None = None) -> Any:
    agent_id = agent_id or f"s5g-{uuid.uuid4().hex[:8]}"
    body = {"agent_id": agent_id, "version": "1", "harness": harness, "config": config}
    assert client.post("/admin/agents", json=body).status_code == 201
    return client.post(f"/admin/agents/{agent_id}/versions/1/publish", json={"eval_passed": True})


def _allow(monkeypatch: pytest.MonkeyPatch, *agent_ids: str) -> None:
    monkeypatch.setattr(admin, "settings", dataclasses.replace(admin.settings, harness_loop_high_risk_allowlist=agent_ids))


@pytest.mark.parametrize(
    "hooks",
    [
        [{"point": "pre_tool", "name": "shell_exec"}],
        [{"point": "pre_model", "name": "token_budget"}],
        [{"point": "pre_tool", "name": "redact_pii"}],
        {"point": "pre_tool"},
    ],
)
def test_invalid_hooks_config_blocks_publish(client, hooks: Any):
    r = _publish(client, "s5g-platform", {"tools": ["echo"], "hooks": hooks})
    assert r.status_code == 409 and "hooks" in r.json()["detail"]


def test_valid_hooks_config_publishes(client):
    hooks = [
        {"point": "pre_model", "name": "token_budget", "params": {"max_total_tokens": 5000}},
        {"point": "post_model", "name": "thinking_summary", "params": {"mode": "llm"}},
        {"point": "pre_tool", "name": "deny_tool_pattern", "params": {"patterns": ["mail.*"]}},
        {"point": "post_tool", "name": "redact_pii", "params": {"kinds": ["phone", "email"]}},
    ]
    assert _publish(client, "s5g-platform", {"tools": ["echo"], "hooks": hooks}).status_code == 200


def test_harness_loop_high_risk_requires_allowlist(client, monkeypatch):
    """loop_owner=harness 的适配器即便达到 L2，声明高风险写的 Agent 不在白名单（默认空）时 409。"""
    _allow(monkeypatch)
    r = _publish(client, "s5g-harness-l2", {"tools": ["ticket.close_batch"]})
    assert r.status_code == 409
    assert "HARNESS_LOOP_HIGH_RISK_ALLOWLIST" in r.json()["detail"] and "ticket.close_batch" in r.json()["detail"]


def test_harness_loop_high_risk_allowed_when_allowlisted(client, monkeypatch):
    agent_id = f"s5g-allow-{uuid.uuid4().hex[:6]}"
    _allow(monkeypatch, agent_id)
    assert _publish(client, "s5g-harness-l2", {"tools": ["ticket.close_batch"]}, agent_id=agent_id).status_code == 200


def test_allowlist_not_needed_for_read_only_or_platform_loop(client, monkeypatch):
    _allow(monkeypatch)
    assert _publish(client, "s5g-harness-l2", {"tools": ["ticket.search"]}).status_code == 200
    assert _publish(client, "s5g-platform", {"tools": ["ticket.close_batch"]}).status_code == 200


def test_allowlist_parsed_from_env(monkeypatch):
    from runtime.config import Settings

    monkeypatch.setenv("HARNESS_LOOP_HIGH_RISK_ALLOWLIST", "agent-a,agent-b")
    assert Settings().harness_loop_high_risk_allowlist == ("agent-a", "agent-b")
    monkeypatch.delenv("HARNESS_LOOP_HIGH_RISK_ALLOWLIST")
    assert Settings().harness_loop_high_risk_allowlist == ()


async def test_assembler_skills_inline_opt_in(client):
    """技能提示词内联（D84）：快照 skills_inline=true 才把 SKILL.md 内联进系统提示词；默认关闭，组装结果不变。"""
    from runtime.assemble.assembler import assemble
    from runtime.state.db import AgentVersion

    base = {"tools": ["echo"], "skills": [{"name": "ticket-summary"}]}
    off = await assemble(AgentVersion(agent_id="a", version="1", system_prompt="SYS", config=base), "human:t")
    on = await assemble(AgentVersion(agent_id="a", version="1", system_prompt="SYS", config=base | {"skills_inline": True}), "human:t")
    assert off.system_prompt == "SYS" and "skills_inline" not in off.report
    assert on.system_prompt.startswith('SYS\n\n<skill name="ticket-summary"') and on.system_prompt.endswith("</skill>")
    assert on.report["skills_inline"] is True and [s.name for s in on.skills] == ["ticket-summary"]
