# B2 Python SDK 最终收口报告

检查时间：2026-10-27（Asia/Shanghai）。这是对已集成约束注解转换的独立复核，不是全仓验收，也不是生产验收。未提交、未推送。本轮没有再改 SDK 源码、生成脚本或测试。仓库内没有 `AGENTS.md`。

## 1. 基线

HEAD：`f4218eaee1b1a9e65faa27b945acc4c491bb3d2f`（2026-10-27 17:58:18 +0800）。工作区在此之上已有约束注解集成，本轮只复核，不沿用上一轮数字。

工具：Python 3.12.0，pydantic 2.13.5，pytest 9.1.1，mypy 2.4.0，datamodel-codegen 0.85.0，ruff 0.16.7。未安装或升级依赖。mypy 使用仓库 `strict = true`。

已阅读《B2-PythonSDK生成类型兼容性分析》和《B2-PythonSDK对外契约与兼容性评审》。没有修改这两份报告、验收矩阵、OpenAPI、Console、Portal、Runtime、Gateway 或 B3 报告。

手写客户端 `__init__.py`、`run_stream.py`、工作台模块不在本轮 diff 里。当前可执行用法是 SDK 测试，没有另外的 Python 示例目录。`test_sdk_smoke.py` 仍用 `RunEvent` / `RunEventType.run_done` 成员身份解析合成事件。

## 2. 文件与 SHA-256

Python 可见字节：

| 文件 | SHA-256 |
| --- | --- |
| `scripts/normalize_sdk_types.py` | `cdc3affc46815fe042c18c7e6e38ca56484da7b70a1c817f6118bf9d8c97d6ea` |
| `scripts/gen_sdk_types.sh` | `198cf8436179b7795870035cf0f386c655540eba61b6aab64b044e1639380645` |
| `scripts/tests/sdk_type_compat/test_normalize_sdk_types.py` | `818987ee6db3acdbe4aa793deb7d3467e7577b1d93a9a4d1cce45674481c6d7b` |
| `scripts/tests/sdk_type_compat/compare_generated_types.py` | `a10fcad3e34b23d43d058eac2372147aeec538910603b857c14436c054abeeee` |
| `packages/sdk-python/src/ai_platform_sdk/types_runtime.py` | `366e9c9b58937e367b3b18ce0212f6e236180b7a6ccce704a67355d5284c4846` |
| `packages/sdk-python/src/ai_platform_sdk/types_gateway.py` | `743cab664e70d95f8400e87b676a4b4fc6dab05b6c93033f1197f542473e437b` |
| `packages/sdk-python/src/ai_platform_sdk/__init__.py` | `8a3fea639337327488a82f6a3afb91a9d05152cc78ab3ce75bc6228ab4cf1b1f` |
| `packages/sdk-python/src/ai_platform_sdk/run_stream.py` | `9aab19914710c3932c7331317a217c725ba28faaf31d66afb66835edf9da6e5e` |
| `docs/api/runtime-openapi.json` | `08bb0d9c79dca859dfd9d1991fcb854a9e3ff48b8f5840d8b045c9498ff4b438` |
| `docs/api/gateway-openapi.json` | `deba936aa8d84b22ef4c859112cf63cce0e57972497e17e3436974ffe73bcb39` |

生成文件相对 HEAD 的 diff 只有两处：`constr` / `conint` / `confloat` 换成 `Annotated`，以及在 `__future__` 导入后插入 `Annotated` 和 `StringConstraints`。枚举类、字符串默认值、`ApprovalBody.calls`、别名和 RootModel 类结构没有被改成另一种包装。

## 3. 复核结果

转换器按 AST 替换这三种调用，未知位置参数、未知关键字和 `**kwargs` 会抛出 `ValueError`。原导入名 `constr` / `conint` / `confloat` 仍留在模块里，`from ai_platform_sdk.types_runtime import constr` 这种路径还在。`RunEvent`、`RunEventType` 成员列表保持。

| 检查 | 结果 |
| --- | --- |
| `normalize(HEAD)` 与工作区两个生成文件 | 逐字节相同 |
| 对工作区文件再跑一次 `normalize` | 幂等，字节不变 |
| 用 `gen_sdk_types.sh` 里的 datamodel-codegen 参数写到临时目录 | 原始输出与 HEAD 逐字节相同 |
| 再对这份原始输出执行 `normalize` | 与工作区逐字节相同 |
| HEAD 模型对工作区模型的行为探针 | runtime 785，gateway 957，合计 1742，差异 0，跳过模型 0 |

探针比较了 schema、默认值、别名、接受/拒绝、`model_dump(mode="python")` 和 JSON。另外用同一组合成输入比较了警告文本，两边一致：

- `ApprovalBody` 的 `calls=[]`、1 个和 33 个都接受。
- `EvidenceBody.fingerprint` 的 64 位十六进制接受；非法字符和过短都拒绝。模式字面量从 raw string 被 `ast.unparse` 写成普通字符串，值仍是 `^[a-f0-9]{64}$`。
- `AllowlistItem` 的 128 个字符接受，129 拒绝，`root` 仍是 `str`。
- `DoneEvent` 省略 `stop_reason` 时两边都是 `str` `"end_turn"`，并带原来的 pydantic enum 序列化警告；显式传入 `"end_turn"` 时两边都是 `StopReason`。
- `InputRequiredEvent` 接受别名 `schema`，拒绝 Python 名 `schema_`。
- `RunEvent` 对 `type="run.done"` 仍满足 `event.type is RunEventType.run_done`。无时区时间 `2026-10-16T13:40:08.136711` 解析结果相同。

没有发现需要修改代码才能保住的公开行为。因此本轮没有改转换器、生成脚本、生成文件或测试。

失败关闭的边界也核对过：测试覆盖 `constr(1)`、`conint(unknown=True)`、`confloat(**options)`。裸函数名上的未知参数会中止。`conlist` 或其他属性调用不在当前生成结果里，转换器会留着不改；当前 OpenAPI 没有发出这些写法。

## 4. 命令

| 命令 | 退出码 | 计数 |
| --- | --- | --- |
| `.venv/bin/mypy packages/sdk-python/src --cache-dir /tmp/b2-sdk-close-80e234dd/mypy --no-error-summary` | 1 | 21 个 `assignment`，runtime 4，gateway 17。没有其他错误，手写模块为 0 |
| `.venv/bin/pytest packages/sdk-python scripts/tests/sdk_type_compat/test_normalize_sdk_types.py -q -p no:cacheprovider --basetemp=/tmp/b2-sdk-close-80e234dd/pytest` | 0 | 56 passed，0 failed，0 skipped，0.45s |
| 临时目录中的 datamodel-codegen（两个 OpenAPI）再加 `normalize_sdk_types.py` | 0 | 与工作区字节相同 |
| `scripts/tests/sdk_type_compat/compare_generated_types.py --compare`（HEAD 对工作区） | 0 | 1742 相等，0 差异 |
| 对转换器、两个专项测试经 stdin 做 `ruff check` 和 `ruff format --check` | 0 | 3 个文件 |
| `bash -n scripts/gen_sdk_types.sh` | 0 | 语法通过 |

21 项全部是“表达式类型为 `str`，变量类型为枚举”。runtime：81 `StopReason`，210 `Action1`，403 `Visibility`，424 `Status`。gateway：57、104、113、115、119、163、166、171、172、179、182、187、188、276、279、397、399。没有加 `type: ignore`，没有缩小 mypy 范围。

pytest 没有失败记录。mypy 退出码 1 是这 21 项未决诊断，不是新的回归。

没有跑全仓 pytest，没有跑浏览器，没有跑 `gen_sdk_types.sh` 全文。该脚本在 Python 生成之后还会执行 Web SDK 的 `pnpm` 生成；本轮只重放了其中的 Python 循环，避免改动 Web 产物。

## 5. 仍需人工确认

这些项有明确的可观察差异，本轮保持原样。

1. 枚举默认值。保持字符串默认值，mypy 继续是 21，省略字段时 Python 类型是 `str`，JSON 与显式传入的枚举值相同。若改成枚举成员，这 21 项会消失，但省略字段的 `model_dump(mode="python")` 会从 `str` 变成枚举，现有序列化警告也会消失。
2. `ApprovalBody.calls`。SDK 现在接受空列表、33 个元素，以及单个字段和 `calls` 同时出现。Runtime 源码模型会拒绝这些输入。收紧 SDK 会改变已经生成的接受范围，不能当作类型清零的一部分。
3. `InputRequiredEvent` 只接受别名 `schema`，不接受 `schema_`。生成注释里的“两种名字都可以”与类行为不一致。扩大接受范围是别名契约变化。
4. 不采用会新增 RootModel 包装的 `--field-constraints` / `--use-annotated`，也不采用会删除 `RunEventType` 等公共枚举的字面量枚举。

## 6. 给主 agent 的集成回归

本轮已经完成 SDK 专项、Python 生成一致性和定向 lint / mypy。集成阶段如果要宣布 B2 与其他批次一起通过，还需要主 agent 统一安排：

- 全仓 pytest。本轮不能把 56 passed 加进其他套件的数字。
- 浏览器与 Portal。本轮没有启动。
- 只有在准备连同 Web 类型一起重生时，才运行完整 `scripts/gen_sdk_types.sh`。OpenAPI 未变时，Python 产物应与上表哈希一致；脚本后半段会写 Web SDK。
- 远端 CI 仍不覆盖 `mypy packages/sdk-python/src`。21 项未清零，不能写成扩展 strict 已通过。

## 7. 清理

临时目录 `/tmp/b2-sdk-close-80e234dd` 存放过 HEAD 副本、重生文件、mypy 缓存、pytest basetemp 和对照 JSON。报告落盘后已删除，未残留。未连接业务服务、数据库、模型或密钥。未提交、未推送。
