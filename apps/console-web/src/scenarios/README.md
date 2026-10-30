# 工单总结只读隔离场景包

`ticket-readonly.json` 是F6.1 v1包，包含草稿配置及两条固定合成样例。仅适用于新临时库、签名测试身份、mock Harness与工单替身；不包含租户、权限、凭据、外部地址、发布状态或评测通过标记。

在Console“Agent草稿、评测与发布”中载入内置包或粘贴文件内容，校验→预览→确认填入草稿配置，再填写独立Agent ID/版本。填入会替换提示词、模型别名、Harness与config，不自动保存、发布或创建Run。管理员权限、服务端发布闸门及工作台登记/ACK流程仍生效。前端包校验是此导入入口的预检，不替代服务端权限或通用Agent配置验证。

包版本固定1，工具仅ticket.get，Harness仅mock，未知字段/写工具/外部配置/更改样例预期拒绝。系统提示词允许非空有界文本；包不是可信签名权限清单。内置合成触发消息为SYNTHETIC_TICKET_SUMMARY、SYNTHETIC_TICKET_FAILURE。

评测使用ticket-scenario.ts的纯函数，只验证合成事件链路；并不调用旧scripts/eval_ticket_summary.py，不访问Langfuse，不生成真实评测引用，也不会自动放行发布。真实质量、身份、数据和模型接入仍待单独授权。

复验：
- 仓库根：`node --experimental-strip-types --test apps/console-web/tests/ticket-scenario.test.mjs`
- 已安装Playwright条件下：`PLAYWRIGHT_MODULE=<已有模块路径> S12_DEMO_EVIDENCE=<自有证据目录> node scripts/start_local_demo.mjs --verify-only --ticket-readonly`
- 后一命令跑实际临时Runtime与Console，验证结束清理。交互启动需显式选择去掉verify-only；本轮报告不表示已有服务常驻。

## 销售只读v1
sales-readonly.json使用独立严格解析器、crm.opportunity.search与synthetic-sales模拟别名。仅本人商机，导入不自动保存/发布。使用 node scripts/start_local_demo.mjs --sales-readonly 隔离启动；不是正式CRM配置。

## 经营分析v1
order-metrics.json固定analytics.orders、mock与synthetic-analytics。用 node scripts/start_local_demo.mjs --analytics-readonly 启动临时环境；指标为创建窗口订单及分币种完成订单金额，非财务收入。三条消息见cases；精确许可、登记/本人ACK及原发起人读取不因包导入而绕过。

## F6统一体验
推荐 node scripts/start_local_demo.mjs --f6-readonly，集中验证后输出统一/demo。三个场景均已启用；使用独立Agent ID导入发布，保持登记/一次执行/本人ACK。单场景模式以欢迎页实际能力标识为准。正式接入和北极星仍待验收。
