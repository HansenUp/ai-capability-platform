# stdio Harness 协议（NDJSON）

引擎以子进程方式启动 Harness 命令（`HARNESS_STDIO_CMD`），双方通过 stdin/stdout 交换一行一个 JSON 的帧。

## 引擎 → Harness（请求帧）
```json
{"frame":"open","session":{...SessionSpec}}
{"frame":"turn","session_id":"…","messages":[…],"tools":[…ToolSpec],"skills":[{"name":"…","path":"…"}]}
{"frame":"hook_result","hook":"pre_tool","call_id":"…","result":{"deny":"reason"}|null}
{"frame":"interrupt","run_id":"…"}
{"frame":"resume","session":{...},"events":[…Event]}
{"frame":"close"}
```

## Harness → 引擎（事件帧）
```json
{"frame":"opened","session_id":"…","capabilities":{…HarnessCapabilities}}
{"frame":"event","event":{…Event}}            # 统一事件，最后一条为 done
{"frame":"hook","hook":"pre_tool","call_id":"…","payload":{…}}   # 请求引擎执行钩子，等待 hook_result
{"frame":"resumed","session_id":"…"}
```

约束：Harness 子进程无直连模型权限，模型请求经本机 `llm_shim` 转发至 Model Gateway；工具由引擎执行（Harness 只产生 `tool_use`，由引擎回灌 `tool_result` 到下一次 `turn`）。

## 进程池预热握手（可选，S6 §3）
启用 `WorkerPool`（`handshake=True`，默认）时，引擎在 `open` 之前先发：
```json
{"frame":"hello"}            # 引擎 → Harness：做完与会话无关的初始化（如 ACP initialize）后回 ready
{"frame":"ready"}            # Harness → 引擎：可接受 open；此前不得输出其他帧
```
收到 `ready` 的 worker 计为热 worker；`ready` 之后的流程与上文完全相同（open → turn… → close）。一个进程只服务一个会话：
`close`（或会话被中断）后进程组被整体回收，不会再收到第二个 `open`。不实现 `hello` 的 Harness 可配 `handshake=False`
（已启动即视为就绪）；Harness 应忽略不认识的帧。

池模式的子进程环境（S6 评审）：不继承引擎进程的环境，只带白名单（PATH / HOME / TMPDIR / LANG 等，见 `pool.INHERIT_ENV`）
与 `PoolConfig.env`；`STDIO_WORKER_DIR` 是池为该 worker 分配的根目录（临时文件放在其下，回收时——含强制 killpg——整个删除）；
`PoolConfig.secret_env` 中的密钥写成该目录下的 0600 文件，环境里只有 `<NAME>_FILE=路径`，子进程读完应立即删除。

## harness 持有循环的轮次语义（ACP 桥，S6 §1～§2）
goose / dsh 经 `python -m ai_harness_stdio.acp.bridge --profile goose|dsh` 接入（或进程内 `AcpStdioHarness("goose")`），
帧格式不变，`opened.capabilities` 为 `loop_owner=harness`、`tool_interception=enforcing`、`can_resume_from_events=true`
（以真实二进制探针结果为准），`version` 为 agent 二进制版本（goose：`goose --version`；dsh：`@deepseek-ai/dsh/package.json`；
取不到时为 ACP `agentInfo.version`）——Runtime 开会话时与发布快照的 `harness_version` 核对，不一致即 `HARNESS_VERSION_MISMATCH`。桥内起 127.0.0.1 临时端口（每会话随机 bearer）：`/v1/chat/completions` 是 llm_shim，
`/mcp` 是平台工具代理；agent 以最小环境、临时 HOME / 工作目录启动，拿不到模型网关密钥。

- 预热：桥启动即拉起 agent 并完成 ACP `initialize`，之后才处理第一帧（`hello` → `ready` 即表示 agent 已就绪）；
  ACP `session/new`（注入平台 MCP、开会话后检查）推迟到第一个 `turn` 帧——那时才知道平台工具。
- 一个 `turn` 恰好对应 agent 的一次模型调用：无挂起工具调用时，`turn` 启动一次 ACP `session/prompt`；模型的思维 / 文本
  增量即刻作为 `event` 帧上报，每次模型调用一条 `usage`。
- 模型请求工具：agent 调 MCP `tools/call`（goose 在 approve 模式下先 `session/request_permission`，桥只对本轮模型请求的
  平台工具放行），桥认领 call_id 后上报 `tool_use` → `usage` → `done(tool_use)` 并结束本 `turn`；该 MCP 调用挂起，
  ACP prompt 仍在进行。下一个 `turn` 帧必须带回同 call_id 的 `{"role":"tool"}` 结果（可选 `tool_statuses`，error / denied
  以 MCP `isError` 交给 agent），桥据此应答挂起调用，agent 继续，prompt 结束时上报 `done(end_turn|max_tokens)`。
  同一模型应答里的多个工具调用串行成多个 `turn`（后续调用不经模型，直接作为下一 `turn` 的 `tool_use`）。
- 结构化续跑：shim 对每次模型请求都丢弃 agent 组装的 system / messages，改用平台 `system_prompt` 与最近一个 `turn`
  帧的 `messages`（工具名映射为 agent 的模型侧名字），模型别名固定为 `SessionSpec.model_alias`，`parallel_tool_calls=false`。
  因此续跑（新进程 `resume` 后历史以工具结果结尾）只需启动一个新 prompt，上下文完全由平台事件日志决定。
- `interrupt`：ACP `session/cancel`，shim 立即停止流式应答并拒绝之后的任何模型请求，进行中的 `turn` 以 `done(cancelled)`
  收尾；随后 `close` 回收 agent（与桥同一进程组）与临时目录。
- 失败关闭（`error` 事件 code=`ISOLATION_VIOLATION`，随后 `usage`、`done(error)`，会话作废）：agent 请求里出现平台以外的
  工具、同一 `turn` 内第二次成功或并发的模型调用（harness 自行多轮）、模型调用本 `turn` 平台工具以外的工具（shim 先缓冲
tool_call 增量，校验通过才转发给 agent）、未经模型请求的工具调用、为非平台调用请求权限、
  其他 agent → client 请求（fs / terminal 等）、开会话后检查发现残留扩展。
