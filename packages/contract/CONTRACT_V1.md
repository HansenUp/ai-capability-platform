# Runtime Contract V1（冻结说明）

- 版本：`ai_contract.CONTRACT_VERSION = "1.4.0"`（1.0.0 冻结；1.1.0、1.2.0、1.3.0、1.4.0 为非破坏性变更，见第 3 节）
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

`run.created`、`run.state_changed`、`context.assembled`、`model.request`、`model.response`、`tool.requested`、`tool.decision`、`tool.approved`、`tool.rejected`、`tool.executing`、`tool.executed`、`tool.result`、`context.compacted`、`input.requested`（**V1 新增**）、`input.provided`（**V1 新增**）、`run.error`、`run.done`、`capabilities.discovered`（**V1.3 新增**，见 CR-1.3）。

`RunEvent` 的主键为 `(run_id, seq)`，`seq` 在同一 Run 内严格递增；`idempotency_key` 用于写操作去重。

`tool.requested` 的 payload 可选 `batch: {id, index, size}`（**V1.4 新增**，见 CR-1.4）；`context.compacted` 的 payload 为 `{upto_index, summary, method, tokens, dropped_hmac, data_class}`（**V1.4 定义**，见 CR-1.4）。

### 1.6 ToolSpec（`tools.py`）

- 必填：`name`、`description`。
- 有默认值（冻结默认值本身）：`input_schema={}`、`capability_id=None`、`risk_level=read`（read / write_low / write_high）、`sandbox_level=read-only`（read-only / workspace-write / full-access，失败关闭）、`data_class="internal"`、`supports_dry_run=False`、`requires_idempotency_key=False`。

签名脚本执行约束（S9）：`run_skill_script` 的有效 ToolSpec 按本次签名清单解析，写沙箱强制 `write_high`。模型原始入参仍保留在 `TOOL_REQUESTED.input`；服务端冻结的执行绑定存于 `effective_input.__skill_execution`，覆盖清单／脚本哈希、沙箱／镜像／配置、引擎与执行用户、Run／工作区，审批指纹包含 stdin 与该绑定。预览只声明范围，不运行脚本；写前操作登记和 `TOOL_EXECUTING` 同事务提交后才能启动。未知结果沿现有 reconcile 输入协议处理，仅允许 done／skip，不允许 retry；人工确认前须清理原引擎的操作容器。对账结果保留签名数据等级与执行绑定，不修改已有事件枚举或 API 请求格式。

### 1.7 HarnessAdapter 协议与 HookSet（`harness.py`）

协议方法：

- `capabilities() -> HarnessCapabilities`
- `async open_session(spec: SessionSpec) -> SessionHandle`
- `run_turn(session, turn: TurnInput, tools: list[ToolSpec], skills: list[SkillDir], hooks: HookSet) -> AsyncIterator[Event]`：每个 turn 结束前必须产出 UsageEvent，否则文本与思维链不落日志；`tool_result`、`card`、`input_required` 只能由平台产出，harness 产出的会被丢弃并记 `HARNESS_FORGED_PLATFORM_EVENT` 错误
- `async interrupt(session, run_id) -> None`
- `async resume(spec: SessionSpec, events: list[Event]) -> SessionHandle`（checkpoint 通过重放事件日志实现）

`TurnInput`：`messages`、`attachments=[]`、`tool_statuses={}`（**V1.1 新增**，`call_id -> ok / error / denied / pending_approval`）。Runtime 按事件日志中的 TOOL_RESULT 为本轮历史里的每个工具结果填写状态，适配器可据此把错误 / 拒绝标记给模型（例如 Anthropic `tool_result.is_error`），不需要的适配器忽略即可。

图片引用（**V1.2 新增**，`images.py`）：`TurnInput.messages` 中用户消息的 `content` 可以是内容块列表，图片块为 `{"type": "image_ref", "ref": "artifact://<32 位十六进制>", "media_type": "image/png|image/jpeg|image/webp|image/gif", "size": int, "sha256": hex}`，只是引用。图片文件位于本 Run 工作区（`SessionSpec.workspace_dir`）的 `<id>.<png|jpg|webp|gif>`。声明 `supports_vision=True` 的适配器在发送给模型时用 `load_image_ref(workspace_dir, part)` 读取（校验 sha256 与文件头类型，失败抛 `ImageRefError`），转成各家格式（OpenAI 兼容 `image_url` data URL、Anthropic `image` 块），不得把图片内容写进任何统一事件；读取失败时产出 `ErrorEvent(code="ATTACHMENT_UNAVAILABLE")` → Usage → Done(error)。`supports_vision=False` 的适配器不会收到 `image_ref`（Runtime 在受理请求时以 422 `ATTACHMENT_MODALITY_UNSUPPORTED` 拒绝）。`TurnInput.attachments` 语义不变（Runtime 不填写）。

`HookSet`：`pre_model`、`post_model`（**V1.1 新增**）、`pre_tool`、`post_tool`、`on_subagent`，均为可选的异步函数；返回 dict 可覆盖 payload（只认 `HOOK_OVERRIDABLE` 白名单内的键），返回 `{"deny": reason}` 表示阻断。

`HookPoint`（**V1.1 新增**，StrEnum）：`pre_model`、`post_model`、`pre_tool`、`post_tool`，即平台统一执行 hook 的四个拦截点。

`post_model` 语义（**V1.1 新增**）：一次模型调用产出完毕（收到该次调用的 `UsageEvent`）后由 Runtime 执行，payload 为 `text`、`thinking`、`tool_call`、`usage`。返回 `{"deny": reason}` 时，本次调用的输出（文本、思维链、工具调用）不进入模型可见历史，工具调用不执行，Run 以 RUN_ERROR `HOOK_DENIED` 进入 failed。为保证被拒绝的输出不外泄，下发方式取决于版本是否配置了 `post_model`：

- 配置了 `post_model`（含可见性为 summary 时隐式挂载的 `thinking_summary`）：Runtime 缓冲本轮文本增量，`post_model` 通过后再下发给客户端；拒绝时客户端收不到这段文本；
- 未配置 `post_model`：文本照常流式下发，不增加延迟（未配置任何 hook 时事件日志与 V1.0 逐字节一致）。

适配器无需感知上述缓冲：`post_model` 不在 `internal_hook_points` 中时，适配器照常产出事件，缓冲与判定都在 Runtime。

`HOOK_OVERRIDABLE`（**V1.1 新增**）：各拦截点可覆盖键白名单——`pre_model` 无；`post_model` 为 `thinking_summary`；`pre_tool` 为 `input`；`post_tool` 为 `output`、`data_class`（只升不降）。白名单外的键忽略并记日志。

`HarnessCapabilities.parallel_tool_calls: bool = False`（**V1.4 新增**）：适配器能否在一轮里交回多个工具调用；不参与 `capability_tier()` 判定。

`HarnessCapabilities` 中与治理相关的字段 `loop_owner`、`tool_interception`、`can_resume_from_events` 以及 `capability_tier()` 的 L0 / L1 / L2 判定规则同样在冻结范围内。`internal_hook_points: list[HookPoint]`（**V1.1 新增**，默认空）声明平台看不到、须由适配器自己调用 hook 的点；runner 传给 `run_turn` 的 `HookSet` 只含这些点，其余点由平台统一执行。该字段不参与 `capability_tier()` 判定。

## 2. 变更规则

1. 冻结后对上述任何类型、字段、取值或语义的变更，须提交接口变更单或 ADR，评审通过后才能合并，并同步更新 `CONTRACT_VERSION` 与本文。
2. 非破坏性变更（新增可选字段、新增事件类型或枚举值且消费方可忽略）递增次版本号，可在阶段内合并。
3. 破坏性变更（删除或重命名字段/取值、改变必填性、默认值或语义、收紧校验）递增主版本号，只在阶段边界合并，并提前通知所有依赖方（runtime、console、gateway、adapters、SDKs）。
4. 契约测试 `packages/contract/tests` 是冻结范围的可执行描述，变更时必须同步更新。

## 3. 接口变更单

### CR-1.1（Contract 1.0.0 → 1.1.0，S5 Hooks 由平台统一执行）

- 关联：S5 实施方案 §1.1 / §2，决策 D71（Contract V1.1）、D72（Hooks 由平台统一执行）。
- 性质：非破坏性（按第 2 节规则 2 递增次版本号，可在阶段内合并）。
- 变更内容：
  1. `HookSet` 新增可选字段 `post_model`（默认 `None`）；拒绝语义与文本缓冲/流式下发规则见 §1.7。
  2. 新增 `HookPoint` 枚举（`pre_model` / `post_model` / `pre_tool` / `post_tool`）与 `HOOK_OVERRIDABLE` 可覆盖键白名单，从包入口导出。
  3. `HarnessCapabilities` 新增 `internal_hook_points: list[HookPoint]`，默认空列表。
  4. `CONTRACT_VERSION` 由 `1.0.0` 改为 `1.1.0`。
  5. `TurnInput` 新增可选字段 `tool_statuses: dict[str, Literal["ok","error","denied","pending_approval"]]`（默认空字典）：Runtime 按事件日志 TOOL_RESULT 填写本轮历史中各工具结果的状态，使实时路径（不只重放续跑）也能把 denied / error 结果以错误形式交给模型；不读取该字段的适配器行为不变。
- 不变：`Event` 判别联合、`EventType`、`RunEventType`、`RunState` 与迁移表、`capability_tier()` 判定规则均不变；hook 结果等新增内容只放在 `RunEvent.payload`。
- 兼容性：所有新增字段均有默认值，现有适配器（mock / litellm / claude-sdk / stdio）与调用方无需修改即可构造；`extra="forbid"` 不变，旧版本消费方不会收到新字段以外的结构变化。
- 契约测试：`packages/contract/tests/test_contract.py` 新增 V1.1 用例（导出、拦截点取值与白名单、`post_model` 可选、`internal_hook_points` 默认值与校验、`TurnInput.tool_statuses` 默认值与取值校验）。

### CR-1.2（Contract 1.1.0 → 1.2.0，S6 图片附件按引用传递）

- 关联：S6 实施方案 §6 WP-D 多模态，决策 D102（图片附件按引用、不支持 vision 返回 422）；延续 D63（附件隔离）、D88 / D89（附件分级继承）。
- 性质：非破坏性（按第 2 节规则 2 递增次版本号，可在阶段内合并）。
- 变更内容：
  1. 新增模块 `images.py`，从包入口导出 `IMAGE_REF_TYPE`（`"image_ref"`）、`IMAGE_MEDIA_TYPES`（允许的类型 → 工作区规范扩展名）、`ImageRefError`、`image_ref_path`、`is_image_ref`、`load_image_ref`、`sniff_media_type`。
  2. 约定 `TurnInput.messages` 中用户消息内容块 `image_ref` 的形状与解析规则（见 §1.7）：平台历史、事件日志、初始消息指纹与重建历史中只出现引用与元数据（类型、字节数、sha256），图片内容只在适配器发送时读取。
  3. `CONTRACT_VERSION` 由 `1.1.0` 改为 `1.2.0`。
- 不变：`TurnInput` / `SessionSpec` / `HarnessCapabilities` 字段不变（`supports_vision` 早已存在，现由 litellm 与 claude-sdk 声明为 true）；`Event` 判别联合、`RunEventType`、`RunState` 与 `capability_tier()` 不变；`TurnInput.attachments` 语义不变。
- 兼容性：不含图片附件的请求，历史形状与 1.1.0 完全一致（只有文本附件时附件说明消息仍为字符串）；`supports_vision=False` 的适配器（mock / stdio）不会收到 `image_ref`。
- 契约测试：`packages/contract/tests/test_contract.py` 新增 `test_image_ref_load_and_validation`（导出、读取与 base64、文件头识别、格式 / 路径穿越 / 缺失 / 指纹 / 类型不符失败关闭）。

### CR-1.3（Contract 1.2.0 → 1.3.0，S8 两级发现事件化与统一错误体）

- 关联：S8 实施方案 §1 S8.2、§2 S7 #4，决策 D125（发现结果事件化、范围快照冻结）、D135（统一错误体）。
- 性质：非破坏性（按第 2 节规则 2 递增次版本号，可在阶段内合并）。
- 变更内容：
  1. `RunEventType` 新增 `CAPABILITIES_DISCOVERED = "capabilities.discovered"`：Agent 版本配置 `discovery.enabled=true` 时，`search_capabilities` 元工具由工具代理在闸口前拦截，经能力网关 `POST /capabilities/search` 检索，把新加入本 Run 的工具写入该事件。payload：`call_id`、`query`、`scope`（版本快照冻结的检索范围）、`tools`（`ToolSpec` 的 JSON 形式列表）、`resources`（工具名 → `{system, tenant}`，闸口策略请求的资源属性）、`tools_hmac`（`tools` 与 `resources` 规范化 JSON 的 HMAC-SHA256，密钥为 Runtime 的 `HOOK_DIGEST_KEY`；S8 评审修复前写入的事件为裸 sha256 的 `tools_hash`，重放时仍按旧算法校验以兼容）、`degraded`，以及超出 `discovery.max_tools` 被丢弃的工具名 `dropped`（有时才写）。重放 / 接管按该事件恢复已发现工具（指纹不符的事件不恢复，失败关闭），不重新检索；发现的工具调用照常过闸口。
  2. 新增模块 `errors.py`，从包入口导出 `ErrorDetail{detail: str, code: str}`：平台 HTTP 接口业务拒绝的统一响应体（400 请求内容不被接受、409 与当前状态冲突；422 只保留给参数校验 `HTTPValidationError`）。
  3. `CONTRACT_VERSION` 由 `1.2.0` 改为 `1.3.0`。
- 不变：`Event` 判别联合、`EventType`、`RunState` 与迁移表、`ToolSpec`、`HarnessAdapter` 协议与 `capability_tier()` 不变。
- 兼容性：未开启 `discovery` 的版本不会产生新事件、不注入元工具；忽略未知事件类型的消费方不受影响。`ErrorDetail` 在 `detail` 之外只多一个 `code` 字段，只读 `detail` 的客户端不受影响。
- 契约测试：`packages/contract/tests/test_contract.py` 新增 `test_v13_capabilities_discovered_and_error_detail`。

### CR-1.4（Contract 1.3.0 → 1.4.0，S9 并行工具调用、上下文压缩事件化与数据分级矩阵）

- 关联：S9 实施方案 §2（并行工具调用、DEF-2、OPT-4）、§9 冻结契约 ③ ④，决策 D149（并行工具调用语义）、D150（上下文压缩事件化）、D152（六通道矩阵与 internal 污点）、D155（claude-sdk 保持串行）。
- 性质：非破坏性（按第 2 节规则 2 递增次版本号，可在阶段内合并）。
- 变更内容：
  1. `HarnessCapabilities` 新增 `parallel_tool_calls: bool = False`：适配器能否在一轮里交回多个 `ToolUseEvent`。litellm 声明 `True`；mock、claude-sdk（D155，CLI 一条消息里的多个 tool_use 仍按调用顺序串行拆成多轮）、stdio 保持默认 `False`。Runtime 只有在适配器声明 `True` **且** Agent 版本快照 `config.parallel_tool_calls` 为 `true` 时才按批处理（默认关；发布时快照开启而适配器不支持即 409）。
  2. `tool.requested` 的 payload 新增可选键 `batch: {id, index, size}`（仅 payload，`RunEvent` 结构不变）：同一轮模型输出的 `size`（≥ 2）个工具调用共享 `id`，`index` 为模型给出的顺序。语义：
     - 可重建历史中同批调用重建为**一条** assistant 消息 `{"role": "assistant", "tool_uses": [{call_id, name, input}, ...], "reasoning"?}`（按 index），其后的工具结果按 index 排序；实时路径与重放同形（`invariant.rebuild_messages` 与 runner 一致，`messages_hash` 相同）。单个调用的形状（`tool_use`）不变。
     - 先按 index 写全部 `tool.requested`，再逐条过闸口（`tool.decision`）；只读调用并发执行（`PARALLEL_TOOL_MAX`，默认 4），写调用按 index 串行，遇结果未知即停（其余未执行的调用续跑时再处理）；同批待审批的调用合成**一张** `impact_preview` 卡片，payload 在原字段（首个待批调用）之外带 `calls: [{call_id, tool, preview, params_hash}]`；`POST /v1/runs/{id}/approve` 接受单个 `call_id` 或批量 `calls[]`，同批全部批准后才回到 working。
     - 接管：只读调用有 `tool.executing` 无结果（孤儿）时直接重执行；写调用走对账，对账卡列出全部结果未知的调用（`calls[]`），全部处置后才回到 working 续跑。
  3. `RunEventType.CONTEXT_COMPACTED`（取值 `context.compacted` 自 1.0.0 起即在枚举中）定义 payload：`upto_index`（压缩前可重建历史中被摘要替换的消息数）、`summary`、`method`（`llm` / `truncate`）、`tokens{before, after}`、`dropped_hmac`（被替换消息规范化 JSON 的 HMAC-SHA256，密钥 `HOOK_DIGEST_KEY`）、`data_class`（摘要继承的数据等级）。Runtime 在 history 超预算时于 `model.request` 之前按边界压缩（不拆工具调用与结果、不拆同批），重建时以 `{"role": "system", "content": "[compacted summary] <summary>"}` 替换 `upto_index` 之前的消息（与 1.0.0 的重建规则一致）。
  4. 新增模块 `data_class.py`（`ai_contract.data_class`）：`Level`（public < internal < confidential，可比较；未知取值按 confidential）、`CHANNELS = ("model", "embedding", "rerank", "observe", "fallback", "memory")`、`Matrix.load(path) / from_dict(obj)`、`Matrix.allows(channel, level, processor) -> bool`（矩阵未覆盖即拒绝，`"*"` 表示任意）、`Matrix.signed`（`signoff.by` 与 `signoff.at` 非空）、`MatrixError`。生产基线要求矩阵已签署；开发态默认 `services/runtime/data_class_matrix.dev.json`（未签署）。
  5. `CONTRACT_VERSION` 由 `1.3.0` 改为 `1.4.0`。
- 不变：`Event` 判别联合、`EventType`、`RunState` 与迁移表、`ToolSpec`、`TurnInput`、`SessionSpec`、`HarnessAdapter` 协议与 `capability_tier()` 不变。
- 兼容性：未开启并行（默认）时每轮至多一个工具调用，事件日志与 1.3.0 逐字节一致（`evals/snapshots` 重录无漂移）；`batch` 只出现在多调用批次中，忽略该键的消费方把同批调用视为相继的单个调用即可；history 未超预算时不产生 `context.compacted`。
- 契约测试：`packages/contract/tests/test_contract.py` 新增 `test_v14_parallel_tool_calls_and_context_compacted`、`test_v14_data_class_levels_and_matrix`。


### 本机诊断增量：上下文压缩响应记录（2026-10-28）
`context.compacted` 可选 `model_usage_record` 为只读诊断：`source=context_compaction`、`requested_alias`、`response_model`、`usage`（现有客户端记录值）、`coverage=response_record_only`、`billing_verified=false`。仅成功取得有效摘要时写入；失败或本地截断不补记录。它不属于主 `model.response` 用量，不改变现有执行预算统计，不声明覆盖失败/重试/回退，也不证明币种或实际扣费。客户端历史解析器可能默认0，消费方不得把它解释为供应商确认的零用量。历史缺失记录仍未知；`tokens.before/after` 仍是上下文大小估计，不是计费Token。

`model.response.hooks[]` 中平台内置 `thinking_summary` 的成功记录可附带同结构 `model_usage_record`，source为`thinking_summary`；仅为响应诊断，不增加HOOK_OVERRIDABLE字段，不改变主执行预算或对原始推理内容的保护。历史未携带该字段仍为未知。
