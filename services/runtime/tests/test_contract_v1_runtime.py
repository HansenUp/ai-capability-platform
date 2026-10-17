"""Contract V1（P0-2.2）在 runtime 侧的落地测试：思维链可见性策略与卡片类型枚举的下发形态。

思维链策略：Agent 版本配置 thinking_visibility（缺省 internal）决定 thinking 事件能否下发给客户端；
无论是否下发，思维链内容都随 MODEL_RESPONSE 事件进入事件日志。
"""

import json

import pytest

from ai_contract import HarnessCapabilities, ThinkingEvent
from runtime.harness import registry
from runtime.harness.mock import MockHarness
from runtime.loop.runner import thinking_visibility
from runtime.state.db import AgentVersion


class _ThinkingHarness(MockHarness):
    """在 mock 的基础上，每轮先产出两段思维链（harness 自己声明 full，用来验证以 Agent 配置为准）。"""

    def capabilities(self) -> HarnessCapabilities:
        return super().capabilities().model_copy(update={"name": "thinker"})

    async def run_turn(self, session, turn, tools, skills, hooks):
        yield ThinkingEvent(delta="先想一想，", visibility="full")
        yield ThinkingEvent(delta="再回答。", visibility="full")
        async for ev in super().run_turn(session, turn, tools, skills, hooks):
            yield ev


def _events(resp):
    """把 SSE 响应体解析为事件字典列表（按 `data:` 前缀行提取 JSON）。"""
    return [json.loads(line[5:].strip()) for line in resp.iter_lines() if line.startswith("data:")]


@pytest.fixture(scope="module")
def thinker_agents(client):
    """注册 thinker harness，并按不同 thinking_visibility 配置发布一组 Agent。"""
    registry.register("thinker", _ThinkingHarness())
    agents = {"default": {}, "summary": {"thinking_visibility": "summary"}, "full": {"thinking_visibility": "full"}}
    agents["invalid"] = {"thinking_visibility": "public"}  # 非法取值按 internal 处理（失败关闭）
    for key, cfg in agents.items():
        agent_id = f"think-{key}"
        r = client.post("/admin/agents", json={"agent_id": agent_id, "version": "1", "harness": "thinker", "config": cfg})
        assert r.status_code == 201, r.text
        assert client.post(f"/admin/agents/{agent_id}/versions/1/publish", json={"eval_passed": True}).status_code == 200
    return agents


def _chat(client, agent_id):
    body = {"agent_id": agent_id, "messages": [{"role": "user", "content": "你好"}]}
    with client.stream("POST", "/v1/messages", json=body) as r:
        assert r.status_code == 200
        return r.headers["x-run-id"], _events(r)


@pytest.mark.parametrize("key", ["default", "invalid"])
def test_internal_thinking_not_streamed_but_logged(client, thinker_agents, key):
    """internal（缺省或非法配置）：SSE 中不出现 thinking 事件，但思维链完整进入 MODEL_RESPONSE 事件日志。"""
    run_id, evs = _chat(client, f"think-{key}")
    assert "thinking" not in [e["type"] for e in evs]
    assert evs[0]["type"] == "text" and evs[-1]["stop_reason"] == "end_turn"
    log = client.get(f"/v1/runs/{run_id}/events").json()
    resp = next(e for e in log if e["type"] == "model.response")
    assert resp["payload"]["thinking"] == "先想一想，再回答。"


@pytest.mark.parametrize("key", ["summary", "full"])
def test_summary_and_full_thinking_streamed(client, thinker_agents, key):
    """full：thinking 事件原样下发；summary（S5 D4）：原始增量不下发，本轮只下发一条 post_model 生成的摘要
    （未声明 thinking_summary 时隐式挂默认 truncate 模式，短思维链的摘要即原文）。visibility 以 Agent 版本配置为准覆写。"""
    run_id, evs = _chat(client, f"think-{key}")
    thinking = [e for e in evs if e["type"] == "thinking"]
    expected = ["先想一想，", "再回答。"] if key == "full" else ["先想一想，再回答。"]
    assert [t["delta"] for t in thinking] == expected
    assert {t["visibility"] for t in thinking} == {key}
    log = client.get(f"/v1/runs/{run_id}/events").json()
    assert next(e for e in log if e["type"] == "model.response")["payload"]["thinking"] == "先想一想，再回答。"


def test_non_stream_response_also_filters_internal_thinking(client, thinker_agents):
    """非流式聚合返回同样遵守可见性策略。"""
    body = {"agent_id": "think-default", "stream": False, "messages": [{"role": "user", "content": "hi"}]}
    data = client.post("/v1/messages", json=body).json()
    assert data["events"] and "thinking" not in [e["type"] for e in data["events"]]
    body["agent_id"] = "think-full"
    data = client.post("/v1/messages", json=body).json()
    assert [e["visibility"] for e in data["events"] if e["type"] == "thinking"] == ["full", "full"]


def test_thinking_visibility_reads_agent_config():
    """thinking_visibility 读取 Agent 版本配置，缺省/空配置/非法值均回落到 internal。"""
    assert thinking_visibility(AgentVersion(agent_id="a", version="1", config=None)) == "internal"
    assert thinking_visibility(AgentVersion(agent_id="a", version="1", config={})) == "internal"
    assert thinking_visibility(AgentVersion(agent_id="a", version="1", config={"thinking_visibility": "summary"})) == "summary"
    assert thinking_visibility(AgentVersion(agent_id="a", version="1", config={"thinking_visibility": 1})) == "internal"


def test_cards_serialize_card_type_as_plain_string(client):
    """运行时产出的卡片使用 CardType 枚举，经 SSE 下发后仍是普通字符串。"""
    msgs = [{"role": "user", "content": '!tool ticket.close_batch {"ids":["T-9"]}'}]
    with client.stream("POST", "/v1/messages", json={"agent_id": "demo", "messages": msgs}) as r:
        evs = _events(r)
    card = next(e for e in evs if e["type"] == "card")
    assert card["card_type"] == "impact_preview" and isinstance(card["card_type"], str)
