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
