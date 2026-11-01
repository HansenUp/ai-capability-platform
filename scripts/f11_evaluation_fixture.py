"""Disposable-only model/tool profile for real Runtime-loop evaluation tests."""
from contextlib import aclosing

from ai_contract import DoneEvent, TextEvent, ToolSpec, ToolUseEvent, UsageEvent
from runtime.api import isolated_datasets as datasets
from runtime.api import isolated_evaluation_rules as rules
from runtime.assemble.assembler import LOCAL_TOOLS
from runtime.harness import registry
from runtime.loop import runner, toolbroker

MESSAGES = {scenario: {case: f"F11|{scenario}|{case}" for case in sorted(cases)} for scenario, cases in rules.CASES.items()}


class Profile:
    messages = MESSAGES
    @property
    def digest(self):
        return datasets.digest({"messages": self.messages, "profile": "f11-synthetic-executor-1"})

    def __init__(self):
        self.active = set()

    async def execute(self, run_id, agent, principal, text):
        self.active.add(run_id)
        try:
            async with aclosing(runner.drive(run_id, agent, principal.subject,
                                             [{"role": "user", "content": text}], credential=principal.token)) as events:
                async for _ in events:
                    pass
        finally:
            self.active.discard(run_id)


def output(scenario, case, subject):
    if case == "failure":
        return {"error": "synthetic upstream unavailable"}
    if scenario == "ticket-summary-readonly":
        return {"id": "SYNTHETIC-1", "title": "设备无法连接", "status": "open"}
    if scenario == "sales-summary-readonly":
        from sales_readonly_fixture import ROWS, search
        rows = [dict(row, owner=subject) if row.get("owner") == "human:alice" else dict(row) for row in ROWS]
        return search(rows, "default", subject, {"q": "NO-MATCH"} if case == "empty" else {})
    rows = [("CNY", 10000), ("CNY", 2000), ("USD", 500)]
    if case == "incomplete":
        rows = [("CNY", None)]
    elif case == "empty":
        rows = []
    currencies = []
    for currency in sorted({row[0] for row in rows}):
        values = [row[1] for row in rows if row[0] == currency]
        currencies.append({"currency": currency, "total_minor": str(sum(values)) if all(v is not None for v in values) else None})
    return {"profile": "synthetic-order-metrics-v1", "complete": True, "order_count": len(rows), "currencies": currencies}


def summary(scenario, value):
    if scenario == "ticket-summary-readonly":
        if set(value) == {"id", "title", "status"}:
            return f"模拟工单摘要：{value['id']} / {value['title']} / {value['status']}"
        return "模拟工单查询不可用，未生成摘要。"
    if scenario == "sales-summary-readonly":
        from sales_readonly_fixture import summary as sales_summary
        return sales_summary(value)
    if value.get("complete") is True:
        return f"模拟经营摘要：当前创建窗口订单数 {value['order_count']}；金额按币种查看工具指标，不代表收入。"
    return "模拟经营查询不可用，未生成成功指标。"


class Harness:
    def __init__(self, previous):
        self.previous = previous

    def __getattr__(self, name):
        return getattr(self.previous, name)

    async def run_turn(self, session, turn, tools, skills, hooks):
        text = next((row.get("content") for row in reversed(turn.messages) if row.get("role") == "user"), "")
        if not isinstance(text, str) or not text.startswith("F11|"):
            async for event in self.previous.run_turn(session, turn, tools, skills, hooks):
                yield event
            return
        _, scenario, case = text.split("|")
        results = [row for row in turn.messages if row.get("role") == "tool" and row.get("call_id") == "f11-read"]
        if not results:
            yield ToolUseEvent(call_id="f11-read", tool_name=rules.TOOLS[scenario], input={"synthetic_case": case})
        else:
            value = results[-1].get("content")
            yield TextEvent(delta=summary(scenario, value if isinstance(value, dict) else {}))
        yield UsageEvent(input_tokens=0, output_tokens=0)  # Model-free synthetic usage, not a provider bill.
        yield DoneEvent(run_id=session.session_id, stop_reason="end_turn" if results else "tool_use")


def install(app):
    from runtime.api import isolated_evaluations as api
    profile = Profile()
    app.state.isolated_evaluations = api.ENABLED
    app.state.isolated_evaluation_profile = profile
    app.state.isolated_evaluation_grants = {("default", "human:dataset_admin", "f11-dataset-demo", "1")}
    previous_harness = registry.get("mock")
    previous_execute = toolbroker.execute_tool
    previous_tools = dict(LOCAL_TOOLS)
    registry.register("mock", Harness(previous_harness))
    for name in rules.TOOLS.values():
        schema = {"type": "object", "properties": {"synthetic_case": {"type": "string"}},
                  "required": ["synthetic_case"], "additionalProperties": False}
        if name in previous_tools:
            LOCAL_TOOLS[name] = previous_tools[name].model_copy(update={"input_schema": {"oneOf": [previous_tools[name].input_schema, schema]}})
            continue
        LOCAL_TOOLS[name] = ToolSpec(name=name, description="F11 synthetic read only", capability_id=f"cap.{name}",
                                   data_class="confidential" if name == "crm.opportunity.search" else "internal",
                                   input_schema={"type": "object", "properties": {"synthetic_case": {"type": "string"}},
                                                 "required": ["synthetic_case"], "additionalProperties": False})

    async def execute(tool, tool_input, dry_run=False, rc=None, call_id=None, idempotency_key=None):
        if rc is None or rc.run_id not in profile.active:
            return await previous_execute(tool, tool_input, dry_run, rc, call_id, idempotency_key)
        scenario = next((key for key, value in rules.TOOLS.items() if value == tool.name), None)
        case = tool_input.get("synthetic_case")
        if dry_run or scenario is None or case not in rules.CASES[scenario] or call_id != "f11-read":
            return {"error": "synthetic execution binding rejected"}
        return output(scenario, case, rc.gate.subject)
    toolbroker.execute_tool = execute

    def restore():
        registry.register("mock", previous_harness)
        toolbroker.execute_tool = previous_execute
        LOCAL_TOOLS.clear()
        LOCAL_TOOLS.update(previous_tools)
    return restore
