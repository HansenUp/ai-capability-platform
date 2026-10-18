#!/usr/bin/env python3
"""录制工单总结（ticket-summary）场景的快照：evals/snapshots/<name>.jsonl 与同名 <name>.expected.json。

快照不是手写的：本脚本在进程内跑真实的 Runtime 流程（FastAPI 应用 + /v1/messages + runner + 工具代理 + 事件日志，
临时 SQLite 库，与 services/runtime/tests/conftest.py 的方式一致），只把"模型"换成按脚本产出事件的 harness。
工具调用走真实链路：Runtime 工具代理（TOOL_DISPATCH=gateway）→ 能力网关 /invoke（ASGI 进程内调用，开发态身份）
→ mcp-ticket（ASGI 进程内调用，开发身份 + 包内 mock 数据）。Runtime 闸口与网关的授权判定都经策略服务
``/authz/check-batch``（ASGI 进程内调用，``POLICY_ENGINE=baseline``：与 Cedar 策略同一套规则的 Python 镜像，
policy_version 固定，快照里的 policy_decision_id 不随策略文件内容变化）。因此事件日志的形态（context.assembled、
model.request 的 messages_hash、tool.decision / tool.executing / tool.executed、错误结果的 status 与 status_code）
与线上 Runtime 写出的完全一致；全程不调用任何模型、不需要启动任何服务。

用法（仓库根目录）：
    .venv/bin/python evals/record_snapshots.py            # 重录全部场景
    .venv/bin/python evals/record_snapshots.py forbidden  # 只重录指定场景（名称去掉 ticket-summary- 前缀）

何时需要重录（重录后用 git diff 审查快照变化，确认是预期的形态变化再提交）：
- Runtime 写事件日志的形态变化：runner / toolbroker / eventlog 增删事件或字段、run/invariant.py 的重建或哈希口径变化；
- ticket-summary 的系统提示词、工具白名单或 skills/ticket-summary 内容变化（context.assembled 的摘要与 token 估算随之变化）；
- 能力网关 /invoke 的包装方式、mcp-ticket 的工具出参或 mock 数据（mcp_ticket/data/tickets.json）变化；
- 新增或修改下方 SCENARIOS。
evals/test_snapshots.py 校验每次 model.request 记录的 messages_hash 能从日志重建，上述任一变化都会让旧快照失效或失真。

确定性：call_id 由脚本固定；run_id 归一化为 snap-<name>，去掉 ts（墙钟时间）；其余字段原样保留。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = Path(__file__).resolve().parent / "snapshots"
_TMP = Path(tempfile.mkdtemp(prefix="snapshot-record-"))

# 以下环境变量必须在导入 runtime / 网关 / mcp-ticket 之前设置（三者都在导入时读取配置）。
for _name in (
    "KEYCLOAK_TOKEN_URL",  # 网关不换 OBO 令牌，以开发态 X-AI-Dev-Sub 转发主体
    "GATEWAY_ADMIN_TOKEN",
    "AUTH_REQUIRED",
    "RUNTIME_ENV",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "LANGFUSE_HOST",
    "CONFIDENTIAL_MODEL_ALIASES",
    "MCP_TICKET_DATA",
):
    os.environ.pop(_name, None)
os.environ |= {
    "DATABASE_URL": f"sqlite+aiosqlite:///{_TMP}/runtime.db",
    "WORKSPACE_DIR": str(_TMP / "ws"),
    "HARNESS": "mock",
    "POLICY_URL": "http://policy-service",  # 主机名无意义：Runtime 闸口与网关的判定请求经下面注入的 ASGI transport
    "POLICY_ENGINE": "baseline",  # 进程内策略服务用基线引擎（不依赖 Cedar sidecar 二进制）
    "CAPABILITY_GATEWAY_URL": "http://capability-gateway",  # 主机名无意义：请求经下面注入的 ASGI transport
    "TOOL_DISPATCH": "gateway",
    "TOOL_RESULT_OFFLOAD_TOKENS": "8000",  # 生产默认值
    "PUBLISH_CHECK_MODEL_ALIAS": "false",
    "MCP_TICKET_URL": "http://mcp-ticket",
    "MCP_TICKET_DEV_AUTH": "true",
    "MCP_TICKET_DB": str(_TMP / "mcp-ticket.db"),
}
os.chdir(ROOT)  # Skill 目录（skills/）按相对路径加载

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ai_contract import (  # noqa: E402
    DoneEvent,
    ErrorEvent,
    Event,
    HarnessCapabilities,
    HookSet,
    SessionHandle,
    SessionSpec,
    SkillDir,
    TextEvent,
    ThinkingEvent,
    ToolSpec,
    ToolUseEvent,
    TurnInput,
    UsageEvent,
)
from capability_gateway import main as gateway  # noqa: E402
from mcp_ticket import main as mcp_ticket  # noqa: E402
from policy_service import main as policy_service  # noqa: E402
from runtime.guard import gate  # noqa: E402
from runtime.harness import registry  # noqa: E402
from runtime.loop import toolbroker  # noqa: E402

HARNESS = "snapshot-scripted"
AGENT = "ticket-summary"
SUBJECT = "human:00000000-0000-4000-8000-0000000a11ce"  # mock 数据中的 alice：T-1001..T-1005 的提交人、T-1012 的处理人
# 与 scripts/verify_s4_e2e.py 中 ticket-summary@1 的系统提示词保持一致
SYSTEM = (
    "你是售后工单总结助手，只能读取工单，不能执行任何写操作（关闭、重开、通知客户等），用户要求写操作时明确说明无法执行。"
    "回答前先用 ticket.search 定位工单、用 ticket.get 获取详情，只依据工具返回的内容作答，不编造。"
    "工具返回未找到或无权限时，直接告诉用户无法访问该工单，不要猜测内容。"
    "用户要求总结某个工单时，按以下五个小标题输出：问题、根因、处理过程、客户反馈、可复用经验。其他问题简短直接回答，并注明工单号。"
)


class ScriptedHarness:
    """按脚本产出事件的 harness（替代真实模型）：每次 run_turn 弹出一轮脚本。事件顺序与 LiteLLM harness 一致：
    [thinking] [text] [tool_use] usage done。"""

    def __init__(self) -> None:
        self.turns: list[list[Event]] = []

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(name=HARNESS, version="snapshot", transport="in-process", loop_owner="platform")

    async def open_session(self, spec: SessionSpec) -> SessionHandle:
        return SessionHandle(session_id=f"snap-{spec.run_id}", harness=HARNESS)

    async def run_turn(
        self, session: SessionHandle, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet
    ) -> AsyncIterator[Event]:
        for ev in self.turns.pop(0):
            yield ev

    async def interrupt(self, session: SessionHandle, run_id: str) -> None:
        return None

    async def resume(self, spec: SessionSpec, events: list[Event]) -> SessionHandle:
        return await self.open_session(spec)


def tool_turn(call_id: str, tool: str, args: dict[str, Any], thinking: str | None = None) -> list[Event]:
    """一轮请求工具的模型输出。"""
    head: list[Event] = [ThinkingEvent(delta=thinking)] if thinking else []
    return [
        *head,
        ToolUseEvent(call_id=call_id, tool_name=tool, input=args),
        UsageEvent(model=HARNESS),
        DoneEvent(run_id="x", stop_reason="tool_use"),
    ]


def final_turn(text: str, thinking: str | None = None) -> list[Event]:
    """一轮给出最终回答的模型输出。"""
    head: list[Event] = [ThinkingEvent(delta=thinking)] if thinking else []
    return [*head, TextEvent(delta=text), UsageEvent(model=HARNESS), DoneEvent(run_id="x", stop_reason="end_turn")]


@dataclass
class Scenario:
    name: str  # 快照文件名为 ticket-summary-<name>
    prompt: str  # 用户消息
    turns: list[list[Event]]  # 脚本化模型的逐轮输出
    final_state: str  # 预期终态（录制时校验，防止把意外的失败录成快照）

    @property
    def final_text(self) -> str | None:
        """最后一轮的助手文本；以错误收尾的场景没有最终文本。"""
        if self.final_state != "completed":
            return None
        return "".join(ev.delta for ev in self.turns[-1] if isinstance(ev, TextEvent))


SCENARIOS = [
    Scenario(
        "basic",
        "总结所有工单",
        [tool_turn("call_01", "ticket.search", {}), final_turn("共 6 张您有权限查看的工单。")],
        "completed",
    ),
    Scenario(
        "structured",
        "总结一下外壳破裂相关的工单",
        [
            tool_turn("call_01", "ticket.search", {"q": "外壳破裂"}),
            tool_turn("call_02", "ticket.get", {"id": "T-1001"}),
            final_turn(
                "工单 T-1001 总结：\n"
                "- 问题：订单 SO-20260828-017 的 2 台 GW-200 智能网关到货外壳破裂，其中 1 台无法开机。\n"
                "- 根因：出库质检合格，判定为运输途中受压损坏。\n"
                "- 处理过程：核对发货批次 B0823，转仓储部核实后向承运方提交理赔；免费更换整机，新机签收并调试通过，回收件入库后关闭工单。\n"
                "- 客户反馈：评分 5，更换及时、沟通清楚，没有耽误产线调试。\n"
                "- 可复用经验：签收时应当场验货；运输损坏类问题先核对出库质检记录，再同步推进换机与承运方理赔。"
            ),
        ],
        "completed",
    ),
    Scenario(
        "multi",
        "对比总结退换货类的工单",
        [
            tool_turn("call_01", "ticket.search", {"q": "退换货"}),
            tool_turn("call_02", "ticket.get", {"id": "T-1001"}),
            tool_turn("call_03", "ticket.get", {"id": "T-1012"}),
            final_turn(
                "共 2 张退换货类工单：\n"
                "T-1001（已关闭）：\n"
                "- 问题：GW-200 到货外壳破裂，1 台无法开机。\n"
                "- 根因：运输途中受压损坏（出库质检合格）。\n"
                "- 处理过程：转仓储部核实并向承运方理赔，免费更换整机，回收件入库后关闭。\n"
                "- 客户反馈：评分 5，更换及时。\n"
                "- 可复用经验：签收时当场验货，运输损坏同步推进换机与理赔。\n"
                "T-1012（处理中）：\n"
                "- 问题：2 台边缘计算盒试用到期，客户暂不采购，需要回收。\n"
                "- 根因：试用期满且客户决定不采购。\n"
                "- 处理过程：已发送回收单，客户已寄出，快递单号待补充。\n"
                "- 客户反馈：暂无。\n"
                "- 可复用经验：试用回收需及时登记快递单号，便于跟踪入库。"
            ),
        ],
        "completed",
    ),
    Scenario(
        "forbidden",
        "总结工单 T-1007",  # T-1007 存在，但既不是 alice 提交的也不由她处理：mcp-ticket 返回统一的 404
        [
            tool_turn("call_01", "ticket.get", {"id": "T-1007"}),
            final_turn("无法访问工单 T-1007：该工单不存在或您没有查看权限。请确认工单号，或联系工单所属人授权后再试。"),
        ],
        "completed",
    ),
    Scenario(
        "empty",
        "总结关于退款的工单",
        [
            tool_turn("call_01", "ticket.search", {"q": "退款"}),
            final_turn("没有检索到与“退款”相关且您有权限查看的工单，请换个关键词或放宽时间范围再试。"),
        ],
        "completed",
    ),
    Scenario(
        "unlisted-tool",
        "总结工单 T-1002 并把它关掉",  # 模型先请求白名单外的 ticket.close_batch：记为错误结果，不进闸口、不执行
        [
            tool_turn("call_01", "ticket.close_batch", {"ids": ["T-1002"]}),
            tool_turn("call_02", "ticket.get", {"id": "T-1002"}),
            final_turn(
                "本场景只能读取工单，无法关闭 T-1002，关闭请在工单系统中操作。以下为 T-1002 的总结：\n"
                "- 问题：3 台 IR-500 升级到固件 v3.2.1 后每隔 2 到 3 小时断线重连，回退固件前无此现象。\n"
                "- 根因：尚未确认；掉线时设备日志出现 DHCP renew 失败。\n"
                "- 处理过程：客户现场工程师已导出设备日志，工单仍未关闭。\n"
                "- 客户反馈：暂无。\n"
                "- 可复用经验：固件升级后的掉线问题先对比升级前后的 DHCP 续约日志，并评估临时回退固件。"
            ),
        ],
        "completed",
    ),
    Scenario(
        "reasoning",
        "总结 T-1004 的处理情况",
        [
            tool_turn(
                "call_01",
                "ticket.get",
                {"id": "T-1004"},
                thinking="用户给了明确工单号 T-1004，直接用 ticket.get 取详情与处理记录，无需先检索。",
            ),
            final_turn(
                "工单 T-1004 总结：\n"
                "- 问题：维保合同 MC-2026-0412 的发票抬头少了分公司名称，客户财务无法入账，要求作废重开。\n"
                "- 根因：开票时抬头信息不完整。\n"
                "- 处理过程：核对开票信息后因原发票已跨月改走红字发票流程，转财务部开具红字发票并重开，客户确认入账后关闭。\n"
                "- 客户反馈：评分 4，希望开票前能再确认一次抬头。\n"
                "- 可复用经验：开票前与客户确认抬头；跨月发票需走红字发票流程。",
                thinking="记录完整，按五个字段整理。",
            ),
        ],
        "completed",
    ),
    Scenario(
        "model-error",
        "总结固件升级掉线的工单",
        [
            tool_turn("call_01", "ticket.search", {"q": "固件"}),
            # 与 LiteLLM harness 遇到模型网关 503 时的输出一致：error → usage → done(error)
            [
                ErrorEvent(code="LLM_ERROR", message="model gateway returned HTTP 503: upstream unavailable", retryable=True),
                UsageEvent(),
                DoneEvent(run_id="x", stop_reason="error"),
            ],
        ],
        "failed",
    ),
]


class HostRouter(httpx.AsyncBaseTransport):
    """按请求主机名分发到进程内 ASGI 应用（网关发出的请求既有策略判定也有连接器调用）。"""

    def __init__(self, routes: dict[str, httpx.AsyncBaseTransport]) -> None:
        self.routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.routes[request.url.host].handle_async_request(request)


def normalize(name: str, event: dict[str, Any]) -> dict[str, Any]:
    """去掉不确定的字段：run_id 归一化、丢弃 ts；idempotency_key 为空时省略。"""
    out = {"run_id": f"snap-{name}", "seq": event["seq"], "type": event["type"], "payload": event["payload"]}
    if event.get("idempotency_key"):
        out["idempotency_key"] = event["idempotency_key"]
    return out


def record(client: TestClient, harness: ScriptedHarness, sc: Scenario) -> None:
    name = f"{AGENT}-{sc.name}"
    harness.turns = [list(t) for t in sc.turns]
    initial = [{"role": "user", "content": sc.prompt}]
    headers = {"X-AI-Subject": SUBJECT}
    r = client.post("/v1/messages", json={"agent_id": AGENT, "messages": initial, "stream": False}, headers=headers)
    r.raise_for_status()
    run_id = r.json()["run_id"]
    state = client.get(f"/v1/runs/{run_id}", headers=headers).json()["state"]
    if state != sc.final_state or harness.turns:
        raise SystemExit(f"{name}: 终态 {state}（预期 {sc.final_state}），剩余脚本 {len(harness.turns)} 轮")
    log = [normalize(name, e) for e in client.get(f"/v1/runs/{run_id}/events", headers=headers).json()]
    lines = [json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in log]
    (SNAP_DIR / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    expected = {"initial_messages": initial, "final_state": sc.final_state, "final_text": sc.final_text}
    (SNAP_DIR / f"{name}.expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"recorded {name}: {len(log)} events, state={state}")


def main(argv: list[str]) -> int:
    wanted = set(argv)
    unknown = wanted - {sc.name for sc in SCENARIOS}
    if unknown:
        raise SystemExit(f"未知场景：{sorted(unknown)}")
    # 进程内串起真实链路：Runtime 能力发现与工具分发 → 网关（ASGI）→ mcp-ticket（ASGI）；闸口与网关判定 → 策略服务（ASGI）
    policy_transport = httpx.ASGITransport(app=policy_service.app)
    gate.set_policy_transport_for_tests(policy_transport)
    toolbroker.set_gateway_transport_for_tests(httpx.ASGITransport(app=gateway.app))  # 能力发现与 /invoke 共用网关客户端
    gateway.set_invoke_transport_for_tests(
        HostRouter({"policy-service": policy_transport, "mcp-ticket": httpx.ASGITransport(app=mcp_ticket.app)})
    )
    harness = ScriptedHarness()
    registry.register(HARNESS, harness)

    from runtime.main import app

    with TestClient(app) as client:
        draft = {"agent_id": AGENT, "version": "1", "harness": HARNESS, "system_prompt": SYSTEM}
        draft["config"] = {"tools": ["ticket.search", "ticket.get"], "skills": [{"name": "ticket-summary"}]}
        client.post("/admin/agents", json=draft).raise_for_status()
        client.post(f"/admin/agents/{AGENT}/versions/1/publish", json={"eval_passed": True}).raise_for_status()
        for sc in SCENARIOS:
            if not wanted or sc.name in wanted:
                record(client, harness, sc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
