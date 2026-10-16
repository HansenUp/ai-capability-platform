# Runtime Contract V1（冻结说明）

- 版本：`ai_contract.CONTRACT_VERSION = "1.0.0"`
- 对应计划项：实施计划 P0-2.2 / Phase0 收尾 A5
- 权威来源：`packages/contract/src/ai_contract/`（本文只是摘要，字段以代码为准）

## 1. 冻结范围

### 1.1 统一事件（`events.py`，`Event` 判别联合，判别字段 `type`）

| type | 模型 | 关键字段 | 说明 |
| --- | --- | --- | --- |
| `text` | `TextEvent` | `delta` | 模型文本增量 |
| `thinking` | `ThinkingEvent` | `delta`、`visibility` | 思维链增量，见 1.2 |
| `tool_use` | `ToolUseEvent` | `call_id`、`tool_name`、`input` | 模型请求调用工具 |
| `tool_result` | `ToolResultEvent` | `call_id`、`status`（ok / error / denied / pending_approval）、`output`、`output_ref` | 工具结果 |
| `card` | `CardEvent` | `card_type: CardType`、`payload` | 富交互卡片，见 1.3 |
| `input_required` | `InputRequiredEvent` | `prompt`、`schema`（JSON Schema 或 null）、`call_id`（可空） | **V1 新增**：Run 进入 input-required，等待用户补充输入 |
| `usage` | `UsageEvent` | `input_tokens`、`output_tokens`、`cache_read_tokens`、`cost`、`model` | 用量 |
| `error` | `ErrorEvent` | `code`、`message`、`retryable` | 错误 |
| `done` | `DoneEvent` | `run_id`、`trace_id`、`stop_reason`（end_turn / tool_use / max_tokens / cancelled / error / awaiting） | 轮次或 Run 结束 |

所有事件模型 `extra="forbid"`，未声明字段一律拒绝。`InputRequiredEvent` 的协议字段名是 `schema`，Python 侧属性名为 `input_schema`（避免遮蔽 BaseModel 自带属性），序列化统一输出 `schema`。

### 1.2 思维链可见性（`ThinkingEvent.visibility`）

取值 `internal`（默认）/ `summary` / `full`。运行时以 Agent 版本配置 `thinking_visibility`（缺省 `internal`，非法值按 `internal` 处理）覆写该字段：

- `internal`：不下发给客户端，只随 `model.response` 事件的 `thinking` 字段进入事件日志；
- `summary` / `full`：原样经 SSE 下发，同时也进入事件日志。

### 1.3 卡片类型（`CardType`，序列化为字符串）

`impact_preview`（高风险写影响预览）、`form`（结构化表单）、`choice`（候选选择）、`reconciliation_required`（结果未知，需人工对账）。

### 1.4 Run 状态（`run.py`，命名对齐 A2A task-state，仅命名对齐）

`submitted`、`working`、`auth-required`、`input-required`、`completed`、`failed`、`canceled`；后三个为终态。

| 源状态 | 允许迁移到 |
| --- | --- |
| submitted | working、canceled、failed |
| working | auth-required、input-required、completed、failed、canceled |
| auth-required | working、failed、canceled |
| input-required | working、failed、canceled |
| completed / failed / canceled | （无） |

### 1.5 Run 事件日志类型（`RunEventType`）

`run.created`、`run.state_changed`、`context.assembled`、`model.request`、`model.response`、`tool.requested`、`tool.decision`、`tool.approved`、`tool.rejected`、`tool.executing`、`tool.executed`、`tool.result`、`context.compacted`、`input.requested`（**V1 新增**）、`input.provided`（**V1 新增**）、`run.error`、`run.done`。

`RunEvent` 的主键为 `(run_id, seq)`，`seq` 在同一 Run 内严格递增；`idempotency_key` 用于写操作去重。

### 1.6 ToolSpec（`tools.py`）

- 必填：`name`、`description`。
- 有默认值（冻结默认值本身）：`input_schema={}`、`capability_id=None`、`risk_level=read`（read / write_low / write_high）、`sandbox_level=read-only`（read-only / workspace-write / full-access，失败关闭）、`data_class="internal"`、`supports_dry_run=False`、`requires_idempotency_key=False`。

### 1.7 HarnessAdapter 协议与 HookSet（`harness.py`）

协议方法：

- `capabilities() -> HarnessCapabilities`
- `async open_session(spec: SessionSpec) -> SessionHandle`
- `run_turn(session, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet) -> AsyncIterator[Event]`：每个 turn 结束前必须产出 UsageEvent，否则文本与思维链不落日志；`tool_result`、`card`、`input_required` 只能由平台产出，harness 产出的会被丢弃并记 `HARNESS_FORGED_PLATFORM_EVENT` 错误
- `async interrupt(session, run_id) -> None`
- `async resume(spec: SessionSpec, events: list[Event]) -> SessionHandle`（checkpoint 通过重放事件日志实现）

`HookSet`：`pre_model`、`pre_tool`、`post_tool`、`on_subagent`，均为可选的异步函数；返回 dict 可覆盖 payload，返回 `{"deny": reason}` 表示阻断。

`HarnessCapabilities` 中与治理相关的字段 `loop_owner`、`tool_interception`、`can_resume_from_events` 以及 `capability_tier()` 的 L0 / L1 / L2 判定规则同样在冻结范围内。

## 2. 变更规则

1. 冻结后对上述任何类型、字段、取值或语义的变更，须提交接口变更单或 ADR，评审通过后才能合并，并同步更新 `CONTRACT_VERSION` 与本文。
2. 非破坏性变更（新增可选字段、新增事件类型或枚举值且消费方可忽略）递增次版本号，可在阶段内合并。
3. 破坏性变更（删除或重命名字段/取值、改变必填性、默认值或语义、收紧校验）递增主版本号，只在阶段边界合并，并提前通知所有依赖方（runtime、console、gateway、adapters、SDKs）。
4. 契约测试 `packages/contract/tests` 是冻结范围的可执行描述，变更时必须同步更新。
