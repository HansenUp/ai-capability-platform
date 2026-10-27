# Console 与 Portal 统一隔离体验

当前入口：http://127.0.0.1:50672/demo ，Portal：http://127.0.0.1:50673/demo.html 。北京时间 2026-10-27 14:50:41 到期。旧地址已停止；新环境为新临时库，两个前端共用 Runtime/Console API。运行进程32018，工具会话39598。到期由启动器关闭两前端和后端；当前仍运行供体验，不宣称已清理。

| 功能 | 实现与本次体验边界 |
|---|---|
| Console Agent 配置、版本与发布 | 已实现；本次发布采用显式开发评测 flag，不代表真实评测通过 |
| Console 工作台与轨迹 | 已实现；创建、审批、输入、回执和同 Run 持久事件可体验，数据仅临时库 |
| Console 运营、节点、目录 | 已有页面与接口；本演示无真实指标、节点或账号接入，不把页面存在当真实运行验收 |
| Portal 发布目录与搜索 | 已实现；按测试用户令牌读取本次 Runtime 发布目录 |
| Portal 同 Run 工作台 | 复用既有工作台、SDK和账本接口；模拟模型工具，非多轮会话 |
| Portal 身份切换 | 独立 demo.html 入口注入内存测试身份；显式切换卸载页面，服务端未决锁不解除。正式 main/router/OIDC 不修改 |
| 模型、工具、策略 | 本次替身；真实模型推理和业务接入未验收 |
| 正式身份、Langfuse、agentgateway | 仍待接入或正式验收；本次不启用 |
| 多轮、附件、自动写重试、生产部署 | 未纳入本次体验；保留原门禁 |

## 使用
统一入口先配置/发布，或直接进入 Portal 选 s12-workbench。Alice 输入 S12_E2E_APPROVAL_THEN_INPUT，按显式登记→执行→本人ACK操作；记下Run ID，切Bob审批，再切Alice补充输入。切换身份后重新定位同Run，未知结果只读对账。统一入口提供完整步骤和Console轨迹链接。

## 本轮验证
Console 原浏览器套件3/3通过，无跳过；Portal vue-tsc --noEmit退出0；启动脚本node --check通过。新增浏览器脚本验证统一入口链接、Portal目录、聊天路由、显式切Bob与返回目录；禁止外部HTTP路由，external=[]，退出0。未在Portal重新跑完整审批链，不冒充全仓验证。证据和截图在 docs/evidence/baseline/2026-10-27-platform-live-demo/，含verify-portal.mjs与portal.png。

首次Portal独立验证因默认Chromium缓存缺失失败，改用已有Chrome后通过，未下载依赖。旧演示Ctrl+C停止时后端清理被KeyboardInterrupt打断，日志定位到本轮旧夹具专属s12-workbench-cle2owl3，已精确删除该目录（含遗留测试令牌），未访问其他库。新服务继续保留供体验。
