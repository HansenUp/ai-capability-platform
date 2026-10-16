# ADR-0004 stdio 传输模式首选 Goose，DeepSeek Harness 为第二实现

- 状态：已采纳
- 日期：2026-10-16
- 关联需求：RT-15、RT-27
- 依据：实施计划 S5.3～S5.4、S6.1～S6.5、第 12 章风险表；ADR-0007、ADR-0008

## 背景

Harness 适配器按传输方式有两种：进程内（claude-sdk）与子进程 stdio。子进程方式让平台可以集成任意外部 Harness，而不被某一家 SDK 绑定。stdio 适配器骨架、NDJSON 协议、回显 Harness 与三项探针已完成，尚未接入任何真实的外部 Harness。

ADR-0007 规定执行能力走集成而不自研；ADR-0008 规定管控点固定在工具代理与能力网关，因此外部 Harness 只负责规划与模型回合，不直接产生副作用。

## 决策

1. stdio 模式首选接入 Goose，DeepSeek Harness（dsh）作为第二实现。两者共用同一套 NDJSON 协议，协议定义以 `harness-adapters/stdio/PROTOCOL.md` 为准：引擎以子进程启动 Harness 命令（环境变量 `HARNESS_STDIO_CMD`，名称由 `HARNESS_STDIO_NAME` 指定），双方经 stdin/stdout 交换一行一个 JSON 的帧；引擎侧请求帧为 `open`、`turn`、`hook_result`、`interrupt`、`resume`、`close`，Harness 侧事件帧为 `opened`、`event`、`hook`、`resumed`。
2. 模型调用转到 LiteLLM：Harness 子进程没有直连模型的权限，其模型请求经本机 `llm_shim` 转发到 Model Gateway（LiteLLM），从而沿用统一的别名、预算、回退和数据分级准入（ADR-0006、ADR-0010）。`llm_shim` 放在各 Harness 目录下（`harness-adapters/stdio/goose/`、`harness-adapters/stdio/dsh/`）。
3. 工具执行权不下放：Harness 只产生 `tool_use`，工具由引擎侧工具代理执行并在下一个 `turn` 回灌 `tool_result`；禁用 Harness 自带的有副作用内置工具。
4. 探针作为上架门禁：任一 stdio Harness 必须通过探针套件 V1 的五项（事件流、工具回灌、中断、Hooks、重放续跑）才允许上架，并进入 CI；按 ADR-0008 的分级，只有拦截不可绕过与恢复后不重复执行也通过实测，才可能被判为 L1 或 L2。
5. 主备关系：Goose 通过探针的前提下优先承载业务 Agent；dsh 作为第二实现用于验证协议的通用性和做切换演练（S6.5）。风险表规定，dsh 探针失败三项以上时不阻塞 Goose，维持"Goose 首选、dsh 第二"。

## 备选方案

只做 claude-sdk 一种适配器：被单一厂商 SDK 绑定，且无法验证协议的通用性，不采纳。

自研 stdio Harness：自研执行能力，与 ADR-0007 冲突，不采纳。

首期就让 dsh 作为首选：其为预览版本，稳定性未知，风险表已列为风险项，仅作为第二实现。

## 后果

- 新增两个适配器目录，以及 `llm_shim` 与 worker 进程池（预热、复用、超时回收、资源限制，S6.4）。
- 同一个 Agent 在 claude-sdk 与 stdio 之间切换只改发布快照中的 `harness` 字段，不改代码；该能力由 S6.5 演练验证。
- Goose 的版本升级纳入季度复评（S22.2）：探针重跑通过才允许升级；快照需锁定 Harness 版本（ADR-0011）。
- 若 Goose 无法通过拦截不可绕过探针，则只能按 L0 承载只读任务，写操作仍由平台持有循环（ADR-0008），不改变本 ADR 的选型顺序。
