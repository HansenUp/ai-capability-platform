# S6 harness 切换演练记录

> 由 `scripts/verify_s6_e2e.py`（`make verify-s6`）生成，机器可读版本见 `evals/reports/s6-switch-drill.json`。重新运行会覆盖本文件。

- 时间：2026-10-18T00:17:06+00:00 → 2026-10-18T00:17:44+00:00（UTC）
- 结论：**全部通过**（83/83 项检查）
- 二进制：goose 1.54.0（钉定 1.54.0）、dsh 0.2.0-rc.2（钉定 0.2.0-rc.2）
- LiteLLM 1.107.0；模型别名 `deepseek-v4-pro-2026-08`

## 演练目的

验证同一个 Agent（ticket-summary）只改 `harness` 字段即可在 claude-sdk、goose、dsh 之间切换并回切：版本快照除 harness 外完全一致；
平台语义（选版、工具只经平台执行且每次调用恰好执行一次、审批挂起与发起人续跑、取消、用量与 Trace）在三种 harness 下一致；
goose / dsh 的进程（ACP 桥 + agent）按进程组回收，池回到 min_idle。

## 步骤

1. 预检钉定版本（goose 1.54.0、dsh 0.2.0-rc.2）与 LiteLLM；起 mcp-ticket ← 能力网关（OBO）← Runtime（强制鉴权、工具经网关、
   goose / dsh 进程池 min_idle=1、max_workers=4）。
2. 发布 v1（claude-sdk）；v2 / v3 = v1 发布请求的深拷贝、只改 harness（goose / dsh），依次全量发布；每次发布后 alice **不指定版本**对话，
   由稳定版选版落到新版本：「请用 ticket.search 查询关键字“固件”的工单（q 参数为“固件”，只调用一次 ticket.search，不需要调用 ticket.get），告诉我工单号和当前状态。」
3. goose 加测（v2）：第二条消息（多轮历史经 shim 结构化重放）、审批续跑（白名单 D79、只有发起人可续跑 D88）、取消后进程回收。
4. 一键回滚到 v1（v2 / v3 retired），再对话一次——切回 claude-sdk。
5. Langfuse：每个 Run 的 Trace（session=run_id、ai.harness、模型 span 用量）；LiteLLM 侧回调只记元数据（`turn_off_message_logging`）。
6. Runtime 退出后 ACP 桥与 agent 进程全部结束。

复现：`make verify-s6`（需 compose 的 keycloak / litellm / observe profile 与 DeepSeek Key，本机装有钉定版本的 goose / dsh）。

## 结果

| 步骤 | 版本 | harness | run_id | 状态 | ticket.search 执行次数 | 用量 输入/输出 | 尝试次数 | 耗时 s |
|---|---|---|---|---|---|---|---|---|
| v1 claude-sdk | v1 | claude-sdk | `run_ceaee0cac5b443949686` | completed | 1 | 2654/138 | 1 | 2.9 |
| v2 goose | v2 | goose | `run_e1f62554be1341a791bb` | completed | 1 | 2039/120 | 1 | 3.7 |
| v3 dsh | v3 | dsh | `run_e4ad234d818e4755a608` | completed | 1 | 2057/117 | 1 | 5.4 |
| 回切 claude-sdk | v1 | claude-sdk | `run_061d556b240a42a2afec` | completed | 1 | 2641/122 | 1 | 2.6 |

版本快照（Runtime 库 t_agent_version；除下列字段外 system_prompt / config / model_alias / skills_digest 等完全一致）：

| 版本 | harness | harness_version（发布时冻结） | 状态 |
|---|---|---|---|
| v1 | claude-sdk | 0.3 | published |
| v2 | goose | 1.54.0 | retired |
| v3 | dsh | 0.2.0-rc.2 | retired |

goose 取消：取消时被租用 worker 进程组内 4 个进程，取消后残留 []；
池中桥进程 1 个（min_idle=1）。LiteLLM 侧回调检查：checked。

## 检查明细

### preflight

- [x] goose 版本 '1.54.0' 等于钉定版本 1.54.0
- [x] dsh 版本 '0.2.0-rc.2' 等于钉定版本 0.2.0-rc.2
- [x] goose 1.54.0 的真实探针存档 evals/reports/probe-real/goose-1.54.0.json 存在且通过（make probe-real，D98）
- [x] dsh 0.2.0-rc.2 的真实探针存档 evals/reports/probe-real/dsh-0.2.0-rc.2.json 存在且通过（make probe-real，D98）
- [x] LiteLLM 存活且登记了别名 deepseek-v4-pro-2026-08

### 发布

- [x] 发布 ticket-summary v1（claude-sdk）
- [x] v2 由 v1 深拷贝，只改 harness（diff ['harness', 'version']）
- [x] 发布 ticket-summary v2（goose）
- [x] v3 由 v1 深拷贝，只改 harness（diff ['harness', 'version']）
- [x] 发布 ticket-summary v3（dsh）
- [x] Runtime 库中 v1/v2/v3 快照除 harness（及随之冻结的 harness_version）外一致（比较 ['agent_id', 'config', 'eval_passed', 'eval_run_ref', 'model_alias', 'model_resolved', 'retrieval_config', 'rollout_allowlist', 'skills_digest', 'system_prompt', 'tenant_id', 'tools_schema_version']）
- [x] 版本快照的 harness_version 对 goose / dsh 冻结为二进制版本 {'goose': '1.54.0', 'dsh': '0.2.0-rc.2'}（钉定 {'goose': '1.54.0', 'dsh': '0.2.0-rc.2'}；开会话时据此核对）

### v1 claude-sdk

- [x] v1 claude-sdk：Run completed
- [x] v1 claude-sdk：选版落到 v1（RUN_CREATED.agent_version=1）
- [x] v1 claude-sdk：ticket.search 的 TOOL_EXECUTED 恰好一次（请求 1、执行 1）
- [x] v1 claude-sdk：ticket.search 结果 ok
- [x] v1 claude-sdk：回答含工单号 T-1002（'查询“固件”关键字的工单结果如下：\n\n| 工单号 | 当前状态 |\n|--------|----------|\n| T-'…）
- [x] v1 claude-sdk：用量 > 0（{'input_tokens': 2654, 'output_tokens': 138, 'model_responses': 2}）

### v2 goose

- [x] v2 goose：Run completed
- [x] v2 goose：选版落到 v2（RUN_CREATED.agent_version=2）
- [x] v2 goose：ticket.search 的 TOOL_EXECUTED 恰好一次（请求 1、执行 1）
- [x] v2 goose：ticket.search 结果 ok
- [x] v2 goose：回答含工单号 T-1002（'查询到 1 个匹配关键字“固件”的工单：\n\n- **T-1002**（工业路由器 IR-500 固件升级后频繁掉线）— '…）
- [x] v2 goose：用量 > 0（{'input_tokens': 2039, 'output_tokens': 120, 'model_responses': 2}）

### goose 第二条消息

- [x] 第二条消息：Run completed（completed）
- [x] 第二条消息：由 v2（goose）执行
- [x] 第二条消息：回答含工单号 T-1002
- [x] 第二条消息：回答复述了只在第一条用户消息里的口令 口令EFBD49（历史经 shim 重放到模型）
- [x] 第二条消息：每个工具调用恰好执行一次

### goose 审批续跑

- [x] 未在 HARNESS_LOOP_HIGH_RISK_ALLOWLIST 中的 goose Agent 声明高风险写，发布被拒（HTTP 409）
- [x] 白名单内的 s6-goose-approve（goose + ticket.close_batch）发布成功
- [x] 挂起：影响预览卡片 + done(awaiting)
- [x] Run 状态 auth-required
- [x] carol 审批通过
- [x] 第三人 bob 续跑 403（HTTP 403）
- [x] 审批人 carol 续跑 403（续跑只允许发起人，HTTP 403）
- [x] 发起人篡改初始消息续跑 409（HTTP 409）
- [x] alice 续跑：done(end_turn)
- [x] 续跑后 Run completed
- [x] TOOL_EXECUTED 只出现一次（1），TOOL_EXECUTING 一次（1）
- [x] 续跑后模型没有重新请求 close_batch（不重新决策）
- [x] close_batch 结果 ok
- [x] 续跑经新 goose 会话（resume 重放）发起了后续模型调用

### goose 取消

- [x] 发布 s6-goose-writer（goose）
- [x] 取消前池处于 min_idle（桥 1 个）
- [x] 第三人 bob 取消 403
- [x] 发起人 alice 取消 200
- [x] Run 状态 canceled（canceled）
- [x] 取消后 0.3s 内被租用 worker 的进程组全部回收（取消时组内 4 个进程，残留 []）
- [x] 池回到 min_idle=1（桥 1 个，每个一个 goose agent，无游离 agent）
- [x] goose 加测结束后池处于 min_idle（桥 1 个）

### v3 dsh

- [x] v3 dsh：Run completed
- [x] v3 dsh：选版落到 v3（RUN_CREATED.agent_version=3）
- [x] v3 dsh：ticket.search 的 TOOL_EXECUTED 恰好一次（请求 1、执行 1）
- [x] v3 dsh：ticket.search 结果 ok
- [x] v3 dsh：回答含工单号 T-1002（'查询关键字“固件”的工单结果如下：\n\n- 工单号：T-1002\n- 当前状态：open（未处理）\n\n共找到 1 条匹配工'…）
- [x] v3 dsh：用量 > 0（{'input_tokens': 2057, 'output_tokens': 117, 'model_responses': 2}）
- [x] dsh 步骤结束后池处于 min_idle（桥 1 个）

### 回切

- [x] 一键回滚到 v1，v2 / v3 retired（{"agent_id":"ticket-summary","active_version":"1","retired":["2","3"]}）

### 回切 claude-sdk

- [x] 回切 claude-sdk：Run completed
- [x] 回切 claude-sdk：选版落到 v1（RUN_CREATED.agent_version=1）
- [x] 回切 claude-sdk：ticket.search 的 TOOL_EXECUTED 恰好一次（请求 1、执行 1）
- [x] 回切 claude-sdk：ticket.search 结果 ok
- [x] 回切 claude-sdk：回答含工单号 T-1002（'查询关键字“固件”的工单结果如下：\n\n- 工单号：T-1002\n- 当前状态：open（待处理）\n\n共找到 1 条相关工'…）
- [x] 回切 claude-sdk：用量 > 0（{'input_tokens': 2641, 'output_tokens': 122, 'model_responses': 2}）

### Langfuse

- [x] v1 claude-sdk：Langfuse 有该 Run 的 Trace（1 条，ai.run_id 一致）
- [x] v1 claude-sdk：Trace 的 ai.harness = claude-sdk（{'claude-sdk'}）
- [x] v1 claude-sdk：模型 span 2 个，均带用量
- [x] v2 goose：Langfuse 有该 Run 的 Trace（1 条，ai.run_id 一致）
- [x] v2 goose：Trace 的 ai.harness = goose（{'goose'}）
- [x] v2 goose：模型 span 2 个，均带用量
- [x] v3 dsh：Langfuse 有该 Run 的 Trace（1 条，ai.run_id 一致）
- [x] v3 dsh：Trace 的 ai.harness = dsh（{'dsh'}）
- [x] v3 dsh：模型 span 2 个，均带用量
- [x] 回切 claude-sdk：Langfuse 有该 Run 的 Trace（1 条，ai.run_id 一致）
- [x] 回切 claude-sdk：Trace 的 ai.harness = claude-sdk（{'claude-sdk'}）
- [x] 回切 claude-sdk：模型 span 2 个，均带用量
- [x] goose 加测的 Run 在 Langfuse 中均有 Trace（缺 []）
- [x] v2 goose：LiteLLM 侧 generation 2 条带 run_id（Run 内模型调用 2 次）
- [x] v2 goose：LiteLLM 侧只有元数据（输入 / 输出均为 redacted-by-litellm，无工单内容）
- [x] v3 dsh：LiteLLM 侧 generation 2 条带 run_id（Run 内模型调用 2 次）
- [x] v3 dsh：LiteLLM 侧只有元数据（输入 / 输出均为 redacted-by-litellm，无工单内容）

### 进程

- [x] Runtime 退出后 15s 内演练中出现过的 8 个 ACP 桥 / agent 进程全部结束（残留 []）

## 发现与说明

- 无
