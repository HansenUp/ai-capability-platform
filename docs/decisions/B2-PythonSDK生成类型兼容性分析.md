# B2 Python SDK 生成类型兼容性分析

检查时间：2026-10-27（Asia/Shanghai）。这是生成类型的离线诊断和兼容性对照，不是全仓验收，也不是生产验收。未提交、未推送。原《B2后端与SDK并行验收》和《B2-Gateway类型收口并行报告》未改。

仓库内没有 `AGENTS.md`。已核对实施计划、两份 B2 报告和 `scripts/gen_sdk_types.sh`。本轮没有修改 `packages/sdk-python/src`、生成脚本、Runtime、Gateway、Web SDK、OpenAPI、CI、依赖或验收矩阵。

## 1. 基线

HEAD：`f4218eaee1b1a9e65faa27b945acc4c491bb3d2f`（2026-10-27 17:58:18 +0800，`fix(platform): close B1 browser lifecycle and align B2 SDK types`）。

任务说明里该提交已推送且远端 CI 通过。本轮没有复跑远端 CI。标准 CI 不运行 `mypy packages/sdk-python/src`，那次通过不能当作扩展 strict 通过。

工具：Python 3.12.0，pydantic 2.13.5，mypy 2.4.0，datamodel-codegen 0.85.0，ruff 0.16.7。未安装或升级依赖。mypy 使用仓库 `pyproject.toml` 的 `strict = true`。

Python 可见 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `docs/api/runtime-openapi.json` | `08bb0d9c79dca859dfd9d1991fcb854a9e3ff48b8f5840d8b045c9498ff4b438` |
| `docs/api/gateway-openapi.json` | `deba936aa8d84b22ef4c859112cf63cce0e57972497e17e3436974ffe73bcb39` |
| `packages/sdk-python/src/ai_platform_sdk/types_runtime.py` | `018d4d8f1f288f021333ea6d60418d14bd19333423945ab879f66468b19f6ff2` |
| `packages/sdk-python/src/ai_platform_sdk/types_gateway.py` | `0dddf74f08b59421a41770ebf5a26ba06038071c8494a8ba3535090db2e55cb5` |
| `scripts/gen_sdk_types.sh` | `10f6672a16e5640bfa50bfca577eccf1e255b91c325e20af8b6a9462a3892de5` |
| `scripts/tests/sdk_type_compat/compare_generated_types.py` | `a10fcad3e34b23d43d058eac2372147aeec538910603b857c14436c054abeeee` |

用现有脚本的同一组参数在临时目录重生后，两个生成文件与仓库字节相同。基准候选可以复现。

## 2. 诊断

命令：

```text
.venv/bin/mypy packages/sdk-python/src --cache-dir /tmp/b2-sdk-compat-80e234dd/mypy-base --no-error-summary
```

退出码 1。98 个错误，只在两个生成文件：`types_gateway.py` 63，`types_runtime.py` 35。手写模块 0。类别：`valid-type` 73，`assignment` 21，`misc` 4。与 B2 记录的 98 相同，但是按这次输出重新计数的。

根因三类，没有第四类：

| 根因 | 数量 | 机制 |
| --- | --- | --- |
| constrained type | 73 | `from __future__ import annotations` 下，`constr()` / `conint()` / `confloat()` 是函数调用，不能当类型 |
| RootModel | 4 | `RootModel[constr(...)]` 的类型参数同样是函数调用。两条 `misc` 落在 `AllowlistItem` 第 244 行和 `RolloutAllowlistItem` 第 217 行，各报 “Type expected” 和 “Invalid base class” |
| 枚举默认值 | 21 | 字段注解是 `StrEnum`，默认值是普通字符串。mypy 报 `str` 不能赋给枚举 |
| 手写模块 | 0 | `PlatformClient`、`run_stream`、工作台客户端不在这 98 项里 |
| 其他 | 0 | 无 |

constrained type 与 RootModel 的位置：

- runtime：`AgentVersionIn` 18, 20, 21, 25, 28；`ApprovalCall` 36, 37；`CreateBody` 55, 57；`EvidenceBody` 110, 111；`FeedbackBody` 120, 121, 122；`InputBody` 148；`MessagesRequest` 180；`Prepare` 189, 192, 193；`PrepareBody` 209, 210, 213；`PublishIn` 231；`RolloutIn` 256；`RunCreate` 269；`RolloutAllowlistItem` 217, 218；`AllowlistItem` 244, 245
- gateway：`Binding` 50, 51；`Capability` 97, 100, 103, 105, 106, 114, 115, 117；`ConnectorVersion` 162, 167, 170, 171；`ConnectorView` 178, 183, 186, 188；`EvalItem` 201；`EvalResultIn` 212, 213, 214；`InvokeIn` 239, 240, 241, 243, 245, 249, 250, 252；`McpServerIn` 274, 275, 277, 278；`ResolveIn` 405, 410；`RevokeIn` 287；`SearchIn` 308–312；`SkillRef` 375, 376；`VersionIn` 395, 397

枚举默认值：

- runtime：`DoneEvent` 78，`PrepareBody` 207，`ThinkingEvent` 400，`ToolResultEvent` 421
- gateway：`Binding` 54；`Capability` 101, 110, 112, 116；`ConnectorVersion` 160, 163, 168, 169；`ConnectorView` 176, 179, 184, 185；`McpServerIn` 273, 276；`VersionIn` 394, 396

实际引用：包导出和 `run_stream` 使用 `RunEvent`；`test_sdk_smoke.py` 断言 `events[1].type is RunEventType.run_done`。`types_gateway` 在仓库内没有导入方。没有调用方不等于可以改掉这些公共类。`RunEvent` 本身不在 98 项里，但它和出错的类在同一个模块，模块级 mypy 仍然失败。

## 3. 候选

生成候选都把 `scripts/gen_sdk_types.sh` 里的 datamodel-codegen 参数原样带到临时目录，再追加下表的参数。输出不写回仓库。`--use-annotated` 的帮助文本写明会同时打开 `--field-constraints`。

语法候选不是新手写模型。它读取当前生成文件，用 AST 把 `constr` / `conint` / `confloat` 换成等价的 `Annotated`，可选地把枚举字段的字符串默认值换成对应成员，再用仓库里的 ruff 经 stdin 整理。转换器在 `scripts/tests/sdk_type_compat/compare_generated_types.py`。

| 候选 | 追加参数或转换 | mypy 退出码 | 错误数 |
| --- | --- | --- | --- |
| baseline | 无 | 1 | 98，类别与仓库相同 |
| annotated | `--use-annotated` | 1 | 21，全部 `assignment` |
| field-constraints | `--field-constraints` | 1 | 21，全部 `assignment` |
| enum-default | `--deserialize-default-values enum` | 1 | 77：`valid-type` 73，`misc` 4 |
| annotated-enum-default | 上两项同时加 | 0 | 0 |
| annotated-literal-enum | `--use-annotated --enum-field-as-literal all` | 0 | 0 |
| syntax-annotated | 只改约束类型写法 | 1 | 21，全部 `assignment` |
| syntax-annotated-enum-default | 约束写法加枚举成员默认值 | 0 | 0 |

八个候选的生成或转换进程、以及随后的隔离加载对照进程，退出码都是 0。

## 4. 兼容性差异

对照把仓库文件和候选放进不同模块名，在单独子进程里加载。每个模型比较 JSON Schema、字段默认值、别名、配置、枚举成员，再跑合成探针：最小合法输入、`model_construct`、额外字段、别名与 Python 字段名、字符串长度、数值边界、列表长度、枚举、日期时间、类型转换、RootModel，以及 `model_dump(mode="python")` 和 JSON。`ApprovalBody.calls` 另测 `[]`、1、32、33 和 `null`。`skipped_models` 全部为 0。

### syntax-annotated：行为一致

runtime 785 个探针相等，gateway 957 个相等，schema、默认值、别名和枚举成员差异都是 0。

独立复核与仓库模型一致：

- `AgentVersionIn` 的空 `agent_id` 两边都是 `string_too_short`，`min_length` 为 1；64 个字符都接受，属性类型仍是 `str`。
- `AllowlistItem` 的 128 个字符都接受，129 都是 `string_too_long`，`root` 仍是 `str`。
- `ApprovalBody.model_validate({"calls": []})` 和 33 个调用都接受。`calls` 仍是 `list[ApprovalCall] | None = Field(None, title="Calls")`，没有 `minItems` / `maxItems`。
- `RunEvent` 对 `type="run.done"` 仍满足 `event.type is RunEventType.run_done`。无时区的 `2026-10-16T13:40:08.136711` 和带偏移的 `2026-10-16T13:40:08.2+00:00` 都解析成功。
- `InputRequiredEvent` 接受别名 `schema`，序列化也用 `schema`。Python 名 `schema_` 两边都是 `extra_forbidden`。这是现有行为，转换没有改它。

这 77 项因此可以视为类型写法变化。它不把诊断清零，还留着 21 个枚举默认值。

### 枚举成员默认值：接受集合不变，Python 默认值类型变了

`DoneEvent.model_validate({"run_id": "r"})`：

- 仓库：`stop_reason` 的运行时类型是 `str`，值是 `"end_turn"`。`model_dump(mode="python")` 得到字符串，并出现 pydantic 警告：期望 enum，实际序列化了 `str`。
- 语法枚举候选：省略时得到 `StopReason.end_turn`。`model_dump(mode="json")` 仍是 `"end_turn"`，`== "end_turn"` 仍成立。
- 显式传入 `"end_turn"` 时，两边都得到枚举成员，并且 `is StopReason.end_turn`。

同一差异出现在 `PrepareBody.action`、`ThinkingEvent.visibility`、`ToolResultEvent.status`，以及 gateway 的 `Binding`、`Capability`、`ConnectorVersion`、`ConnectorView`、`McpServerIn`、`VersionIn`。对照里的差异都是 `model_dump(mode="python")` 和属性类型；接受/拒绝和 JSON 没有分叉。runtime 29 个行为差异、gateway 201 个，是因为这些模型的其他字段探针仍带着被省略的枚举默认值。

这是默认值对象的类型变化，不能算作只改注解。

### `--use-annotated` 和 `--field-constraints`：校验集合变了

两者都把 77 个类型错误消掉，并留下同样的 21 个赋值错误。它们相对仓库的行为形状相同。

`ApprovalBody.calls` 在候选里变成新的 `Calls` RootModel，schema 为 `minItems: 1`、`maxItems: 32`。最小复现：

- `{"calls": []}`：仓库接受并得到 `[]`；候选拒绝，`too_short`，`min_length` 1。
- 33 个 `{"call_id": "c", "params_hash": "h"}`：仓库接受；候选拒绝，`too_long`，`max_length` 32。
- 1 个和 32 个仍接受，但属性类型从 `list` 变成 `Calls`。

Runtime 服务端 `ApprovalBody.calls` 本来就有 `min_length=1, max_length=32`。候选更接近服务端 schema，同时改变了 SDK 今天已经发布的接受集合。这不是兼容修复。服务端另外还有二选一的 `model_validator`，OpenAPI 和当前 SDK 都没有；本轮候选也没有把它补进来。

这两个参数还会把部分字符串收成新的 RootModel。测到的属性类型变化：

- `AgentVersionIn.harness`：`str` 变为 `Harness`。JSON 仍是字符串，例如 `"claude"`。
- 同样的包装发生在 `FeedbackBody.correction`、`InputBody.call_id`、`MessagesRequest.session_id`、`PrepareBody.call_id`、`PrepareBody.params_hash`，以及 gateway 的 `Capability.compensate_tool`、`Capability.connector`、`EvalResultIn.tenant`、`InvokeIn.agent_tenant`、`InvokeIn.idempotency_key`、`InvokeIn.run_id`、`VersionIn.system`。
- `SearchIn.scope` 从 `list` 变成 `Scope`。

这些探针的 JSON 和接受/拒绝与仓库相同，Python 属性类型不同。runtime 还有 7 个、gateway 还有 9 个新类只出现在候选里。

### 两个零诊断候选都改变行为

`annotated-enum-default` 同时带上列表长度变化、RootModel 包装和枚举默认值的 Python 类型变化。mypy 为 0。

`annotated-literal-enum` 也是 0，并删除 runtime 9 个、gateway 12 个枚举类。`RunEventType` 不复存在，`RunEventType.run_done` 这种成员访问不能保留。非法枚举值的错误类型从 `enum` 变成 `literal_error`。它同时保留 `calls` 的新长度限制。

没有一个候选既把 98 项清零，又在这套探针上与仓库行为一致。

## 5. 结论

A. 可直接修复：`syntax-annotated` 覆盖的 77 项。证据是全部模型 schema 和字段元数据无差异，1742 个探针无差异，以及上面的长度、RootModel、审批列表、RunEvent 和别名复核。公共类名、默认值、接受集合和序列化保持原样。

B. 需人工确认：

- 21 个枚举默认值。换成枚举成员后，省略字段时 `model_dump(mode="python")` 的类型从 `str` 变为 `StrEnum`，仓库路径上的 pydantic 序列化警告会消失。JSON 和显式传值不变。
- 任何打开 `--field-constraints` 的生成参数，包括单独使用和 `--use-annotated`。`ApprovalBody.calls` 会开始拒绝空列表和 33 个元素，若干字段的 Python 属性类型变成新的 RootModel。
- `--enum-field-as-literal all`。枚举类从公共 API 消失。

C. 证据不足：没有把整份结论标成证据不足。边界是探针本身：覆盖每个模型的顶层字段边界、默认值、额外字段、别名、构造和序列化，不是所有嵌套组合的穷举。schema 完全相同的 `syntax-annotated` 不受这个缺口影响。枚举候选已经有观察到的 Python 类型差异，所以归入 B，不需要再用更大的样本去证明它有差异。

## 6. 建议

优先只落地 A：在生成脚本之后增加与本轮相同的 AST 转换，把 `con*` 和 `RootModel[con*]` 写成 `Annotated`，不要手改生成文件，也不要打开 `--use-annotated` 或 `--field-constraints`。转换后应重跑本对照脚本，确认 schema 和探针仍是 0 差异，再跑 SDK 自己的 pytest。mypy 预期从 98 降到 21，不是 0。

21 个枚举默认值继续单列。若要清掉它们，需要单独接受“省略默认值时 Python 对象类型从 `str` 变为枚举成员”。仅因为 JSON 相同，不能把它写成行为不变。

不采用字面量枚举。它能让 mypy 通过，代价是删除 `RunEventType` 等公共枚举，现有 `is RunEventType.run_done` 无法保留。

不因为服务端已有 `min_length` / `max_length`，就把 SDK 的 `ApprovalBody.calls` 收紧。那是 SDK 行为变化，要单独决定。

## 7. 命令与结果

| 命令 | 退出码 | 结果 |
| --- | --- | --- |
| `.venv/bin/mypy packages/sdk-python/src --cache-dir /tmp/b2-sdk-compat-80e234dd/mypy-base --no-error-summary` | 1 | 98 个错误，分布见第 2 节 |
| `.venv/bin/python scripts/tests/sdk_type_compat/compare_generated_types.py --out /tmp/b2-sdk-compat-80e234dd` | 0 | 8 个候选都完成生成或转换、mypy 和隔离对照 |
| 对对照脚本经 stdin 运行 `ruff check` 与 `ruff format --check` | 0 | 只检查这一个新文件 |

没有跑 pytest，没有全仓测试，也没有浏览器。探针没有跳过模型。八个对照子进程都是退出码 0。

## 8. 改动与清理

本轮新增：

- `scripts/tests/sdk_type_compat/compare_generated_types.py`
- `docs/decisions/B2-PythonSDK生成类型兼容性分析.md`

没有修改已有源码、生成脚本、OpenAPI 或验收文档。临时目录 `/tmp/b2-sdk-compat-80e234dd` 保存过候选、mypy 缓存和对照 JSON，报告落盘后已删除，未残留。未连接业务服务、数据库、模型或密钥。

## 9. 给主 agent 的集成检查

1. 若采纳 A，只增加生成后的 AST 转换，并保留当前 datamodel-codegen 参数。不要同时改 `--use-annotated`。
2. 集成后重跑第 7 节的对照脚本。`syntax-annotated` 相对当时的生成文件应仍是 0 个行为差异；mypy 应剩 21 个 `assignment`。
3. 再跑 `pytest packages/sdk-python`。现有冒烟测试依赖 `RunEventType` 成员身份，字面量枚举候选会碰到它。
4. 全仓 pytest、浏览器和远端 CI 仍由集成轮处理。这次离线对照通过不能写成 SDK strict 已交付。
