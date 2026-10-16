# 架构决策记录（ADR）

编号 | 主题 | 状态
---|---|---
0001 | Cedar 判定服务化（sidecar HTTP），Python 不直接绑定 | 已采纳
0002 | Langfuse 自托管作为 Trace 与评测事实源 | 已采纳
0003 | APISIX 自定义插件承担身份注入与节点路由 | 已采纳
0004 | stdio Harness 首选 Goose，DeepSeek Harness 为第二实现 | 已采纳
0005 | 能力网关数据面 POC 范围（agentgateway / ContextForge vs 自研） | 待评审
0006 | 模型别名规范：`<family>-<yyyy-mm>`，Agent 快照锁定别名 | 已采纳
0007 | 平台定位：治理与供给优先，执行能力集成不自研 | 已采纳
0008 | 执行循环归属与适配器能力分级（L0/L1/L2） | 已采纳
0009 | 授权多条件合取与范围审批 | 已采纳
0010 | 数据分级传播覆盖全部出口 | 已采纳
0011 | 执行者身份与副作用可回滚 | 已采纳

模板见 `0000-template.md`。
