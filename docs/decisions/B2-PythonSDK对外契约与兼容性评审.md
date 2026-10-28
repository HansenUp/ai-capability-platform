# Python SDK 对外契约与兼容性评审

## 基线与分工
2026-10-27，主agent只核对公开契约及当前行为；候选生成及98项诊断复现由另一agent负责。起始HEAD f4218eaee1b1a9e65faa27b945acc4c491bb3d2f，工作区干净。未修改SDK源码、生成脚本、OpenAPI或依赖，未运行真实服务。本报告不是候选方案已获批准的声明。

## 公开边界与调用链
| 层 | 当前契约 | 兼容性关注 |
|---|---|---|
| 包发布结构 | ai-platform-sdk 0.1.0；setuptools发现src包；Python>=3.11；pydantic>=2.6；生成模块随包存在 | 没有__all__并不代表导入路径私有；不能因仓库未发现调用就删除生成类。未证明外部用户使用情况，也未验证所有允许Pydantic版本 |
| 顶层PlatformClient | chat/create_run/get_run/run_events/follow_run/approve/aclose | 方法签名、字典返回、异常传播与请求体保持；legacy写接口不能在类型修复中自动迁移为工作台协议 |
| 生成类型RunEvent | 顶层模块与run_stream直接导入；run_events的TypeAdapter返回list[RunEvent] | 字段类型、枚举对象、时间解析、model_dump与extra变化都会影响实际消费者 |
| chat | 使用ai_contract.Event的TypeAdapter | 不能把生成事件模型替换为同名契约模型或反向替换，二者不保证行为相同 |
| follow_run | 单次GET；手写先核对scope、seq准确整数、event_id/type、payload，再由生成RunEvent解析 | 不得因生成类型接受更宽输入就去掉手写检查；done不代表Run终态或ACK；无自动重连/重发 |
| WorkbenchReader | 独立手写VerifiedIdentity/WorkbenchReceipt；strict/frozen及回执绑定校验 | 不复用较宽的生成类型取代安全验证；身份、attempt、action、revision及未知写锁语义保持 |
| WorkbenchSession | 显式prepare/execute/recover/acknowledge/confirm_closed | 不引入自动执行、自动重试、管理员解锁或跨进程恢复；现有文档明确反馈锁不支持时失败关闭 |
| 生成请求模型 | 可由用户直接导入，但手写create_run/approve自行构造字典，并未先通过生成请求模型 | 生成请求模型变化和客户端线上请求行为要分别验证；服务端保持最终校验/授权 |

已扫描受Git管理Python源码的生成类型导入：SDK顶层、run_stream和冒烟测试引用RunEvent；未发现types_gateway直接导入。这只证明仓库内当前静态导入，不证明动态导入或外部消费者不存在。

## 已执行的离线契约探针
输入均为合成值，无HTTP请求。使用当前SDK生成类；Runtime ApprovalCall/ApprovalBody从源码AST提取并在隔离命名空间编译，保留字段和model_validator。此项验证实际类定义的局部行为，不冒充Runtime HTTP/身份验收。

| ApprovalBody输入 | 当前SDK | Runtime原类定义 |
|---|---|---|
| 空对象 | 接受 | 拒绝 |
| calls=[] | 接受 | 拒绝 |
| calls包含1个合法调用 | 接受 | 接受 |
| calls包含33个合法调用 | 接受 | 拒绝 |
| 单项call_id/params_hash和calls同时存在 | 接受 | 拒绝 |

5个结果已用断言核对，命令退出0。Runtime同时有1～32长度约束与_one_form互斥校验；OpenAPI生成不保证完整表达Python自定义validator。不能把“对齐OpenAPI”写成“与服务端完全等价”。

另两组观察：
- DoneEvent(run_id='r')的stop_reason为str；显式传'end_turn'得到StopReason。两者model_dump_json相同，但省略默认值触发PydanticSerializationUnexpectedValue警告。换成枚举默认值会改变Python属性类型和警告表现，即使JSON相同仍属可观察变化。
- InputRequiredEvent(prompt='p',schema={})接受；schema_={}与input_schema={}均拒绝。生成说明文字提到两种命名，不符合当前生成类行为。不能修改别名接受集合来顺手“修正文档语义”。

观测及输入SHA-256保存在忽略的docs/evidence/baseline/sdk-public-contract/observations.json、input-sha256.json。没有新建数据库或服务；无密钥访问。原代码警告如实记录，不抑制或转为成功证明。

## 可直接执行与人工门禁
| 分类 | 项目 | 判定条件/处理 |
|---|---|---|
| 可直接做 | 诊断重现、根因分组、临时生成、Schema及运行时样例比较 | 无正式源码写回；候选报告记录工具版本与可复现命令 |
| 可直接修复候选 | constrained类型改为等价Annotated表达、类型表达消歧 | 仅在约束、默认值、错误类别、转换/序列化、公开导入等均有一致证据且生成可重复时允许；当前尚未批准具体候选 |
| 需确认 | 增加ApprovalBody.calls长度或互斥规则 | 会收紧SDK可接受输入，可能提前阻止原由服务端拒绝的调用；推荐独立兼容性变更，不混入strict清零 |
| 需确认 | 字符串默认值改枚举对象、validate_default/use_enum_values/strict全局设置 | 可能改变Python值类型、校验、警告、dump；不能只比较JSON |
| 需确认 | 必填/null、数值/字符串/列表边界、coercion、extra、RootModel、别名与序列化 | 接受集合、输出或异常发生位置变化均需列最小复现并决策 |
| 需确认 | 导出路径/类名、方法签名、依赖/生成器升级、包发布 | 属于公共接口或交付变更，不由本轮类型整理自动授权 |
| 继续保留 | 工作台安全检查与legacy边界 | 不新增Any/ignore、不缩小mypy目标，不自动写重试/解锁 |

推荐先采用“保持当前SDK可观察行为”的最小生成修复；将服务端约束对齐、默认值规范化、别名一致化分别列成独立变更供确认。若所有候选都改变行为，则保持现状并提出具体选择，不为清零强行改模型。

## 候选报告汇合后的评审清单
1. 比对实际HEAD与本轮输入哈希，确保候选基于相同输入。
2. 对每类诊断复核静态效果，并审阅接受/拒绝、属性类型、dump及警告差异；JSON Schema一致不足以通过。
3. RunEvent响应路径及手写工作台边界单列回归；未使用模型也保留直接导入兼容性。
4. 候选须可重复生成且不手改产物；不升级依赖，不新增ignore。若依赖现有宽版本范围中的特定行为，要注明只验证了当前安装版本，不能泛称全版本兼容。
5. 无行为变化项可按既有本机授权实施；有行为变化项集中列出推荐选项并等待确认，不先执行后补批。

当前未发现约定的《B2-PythonSDK生成类型兼容性分析》报告，统一评审尚未开始。双方原文件保留，本轮没有生成代码修复或重新全仓回归。

## 统一评审与隔离集成结果
已收到并保留《B2-PythonSDK生成类型兼容性分析》及原对照器。对照器SHA与报告一致，修改前两个生成文件SHA与候选报告一致。仅采纳约束注解转换；没有采纳枚举默认值、field-constraints或literal-enum候选。

生产转换器为scripts/normalize_sdk_types.py，接在原生成参数之后。使用AST识别并精确替换源文本中的constr/conint/confloat表达式，保留原注释、导入符号、默认值和其余源码；不使用候选器的枚举默认值转换，也不依赖测试脚本运行生产生成。未知约束参数失败关闭。RootModel的基类约束也由同一转换处理。没有改OpenAPI或手写客户端。

验证结果（本轮实际执行）：
- 原始生成文件与集成文件离线对照：runtime 785、gateway 957，共1742个探针相同，差异0、跳过模型0；包括Schema、默认值、别名、接受/拒绝和Python/JSON输出。原有序列化警告仍存在，不抑制。有限样本不能证明全部嵌套输入或所有允许依赖版本兼容。
- 两份OpenAPI按原参数生成后再转换，与工作区文件逐字节一致；重复转换幂等。未运行Web生成，因为本轮Web/OpenAPI未变。
- `.venv/bin/pytest packages/sdk-python scripts/tests/sdk_type_compat/test_normalize_sdk_types.py -q`：56 passed，退出0。其中SDK原52项，转换器4项。覆盖保留默认值/别名/导入、幂等与不支持语法拒绝。
- 扩展mypy检查两个生成模块：21个assignment，退出1（预期未清零）；原77个约束类型诊断消除。没有缩小既有门禁、增加ignore或放宽校验。
- 新转换器及测试经stdin执行ruff check/format通过；bash -n生成脚本通过。测试初版使用exec触发S102，已改为临时模块加载后复验；不加豁免。
- 原SDK手写模块不变。本轮未运行全仓Python、浏览器、远端CI，不沿用历史结果声称同版本全仓通过。

证据归档docs/evidence/baseline/sdk-public-contract/integration（忽略目录）：comparison.json、mypy.json、hashes.json。对照候选/缓存临时目录已精确清理，没有启动服务或容器。

仍需确认且未执行：21项枚举默认值的对象类型规范化；审批长度/互斥校验收紧；别名接受范围调整；任何新增RootModel包装或公共枚举删除。建议下一批仅讨论枚举默认值兼容决策，不把其他API变更捆绑进去。本轮“合入”仅指工作区集成，未提交、推送或发布。双方报告及改动全部保留。
