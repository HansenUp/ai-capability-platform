# 架构决策记录（ADR）

每份 ADR 记录一项影响面较大的技术决策：背景、决策、备选方案、后果与关联需求。新增决策按顺序编号，已采纳的决策不直接改写，变更时新增 ADR 并在旧文中标注"已废弃"及替代者。

编号 | 主题 | 状态 | 文件
---|---|---|---
0001 | Cedar 判定服务化（sidecar HTTP），Python 不直接绑定 | 已采纳 | [0001-cedar-as-service.md](0001-cedar-as-service.md)
0002 | Langfuse 自托管作为 Trace 与评测事实源 | 已采纳 | [0002-langfuse-as-source-of-truth.md](0002-langfuse-as-source-of-truth.md)
0003 | APISIX 自定义插件承担身份注入与节点路由 | 已采纳 | [0003-apisix-routing-plugin.md](0003-apisix-routing-plugin.md)
0004 | stdio Harness 首选 Goose，DeepSeek Harness 为第二实现 | 已采纳 | [0004-stdio-harness-first-choice.md](0004-stdio-harness-first-choice.md)
0005 | 能力网关数据面 POC 范围（agentgateway / ContextForge vs 自研） | 已采纳（范围评审通过，结论待 S6 回填） | [0005-capability-gateway-dataplane-poc.md](0005-capability-gateway-dataplane-poc.md)
0006 | 模型别名规范：`<family>-<yyyy-mm>`，Agent 快照锁定别名 | 已采纳 | [0006-model-alias-convention.md](0006-model-alias-convention.md)
0007 | 平台定位：治理与供给优先，执行能力集成不自研 | 已采纳 | [0007-platform-positioning.md](0007-platform-positioning.md)
0008 | 执行循环归属与适配器能力分级（L0/L1/L2） | 已采纳 | [0008-execution-loop-ownership.md](0008-execution-loop-ownership.md)
0009 | 授权多条件合取与范围审批 | 已采纳 | [0009-authorization-conjunction.md](0009-authorization-conjunction.md)
0010 | 数据分级传播覆盖全部出口 | 已采纳 | [0010-data-class-all-exits.md](0010-data-class-all-exits.md)
0011 | 执行者身份与副作用可回滚 | 已采纳 | [0011-side-effect-recoverability.md](0011-side-effect-recoverability.md)

模板见 [0000-template.md](0000-template.md)。阶段性的选型确认与决策纪要见 [../decisions/](../decisions/)。
