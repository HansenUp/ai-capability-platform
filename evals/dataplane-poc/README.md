# 数据面 POC（ADR-0005 / S6.6）

对比三种能力网关数据面：自研最小数据面（`capability_gateway/mcp_proxy.py`）、agentgateway v1.6.0、IBM ContextForge v1.0.11，
对照基线为直连上游 MCP Server。结论与评审见 `docs/adr/0005-capability-gateway-dataplane-poc.md`，数据见 `results/`。

## 组成

| 文件 | 作用 |
|---|---|
| `upstream.py` | 上游 MCP Server（mcp 2.x MCPServer，streamable-http，有状态会话）：`ticket.search`（约 1 KB 请求 / 10 KB 响应）、`ticket.get`（流式，先发 3 条进度通知）、`ticket.purge`（未登记工具）；Bearer JWT 按 JWKS 验签（iss / aud=mcp-ticket / exp），`X-AI-Act` 仅在 azp 可信时采信；探针调用把收到的身份与 traceparent 记入 SQLite |
| `loadgen.py` | 多进程闭环压测器：每个并发单元一个真实 MCP 会话，统计 P50/P95/P99、流式首字节、错误率、RPS |
| `cases.py` | 功能用例：鉴权六项（Keycloak 真令牌）、deny 不到达上游、策略服务 / 判定端点停掉后失败关闭、traceparent 透传率、Langfuse 可查 Span |
| `poc.py` | 编排：构建镜像、起停容器、ContextForge 登记、按轮次交错压测、采样 CPU/内存、汇总 `results/summary.{json,md}` |
| `docker-compose.yml` | upstream、policy、selfbuilt（自研，兼判定端点 `/authz/mcp-check`）、agentgateway、contextforge、otel（POC 的 Span 汇聚点） |
| `agentgateway.yaml` | agentgateway 声明式配置：jwtAuth + ext_authz(HTTP) 回调 `/authz/mcp-check` + 头整形 + OTLP |
| `contextforge/` | ContextForge 原生插件（TOOL_PRE_INVOKE 回调 `/authz/mcp-check` 并整形身份头）与插件配置 |
| `policy_app.py` / `relay.py` | 策略服务入口（判定日志改为有界）/ 容器内到宿主机 Keycloak 的 TCP 转发（保持签发者 Host 一致） |

## 前置

1. `make compose-up`（需 keycloak 与 observe profile：otel-collector、langfuse）。
2. 镜像（用户已批准拉取）：`docker pull ghcr.io/agentgateway/agentgateway:v1.6.0`、`docker pull ghcr.io/ibm/mcp-context-forge:v1.0.11`。
   Python 组件借用 ContextForge 镜像的 Python 3.12 运行时，不另拉基础镜像。
3. `make setup` 后的 `.venv`（宿主机侧运行 `poc.py` / `cases.py`）。

## 复现

```bash
.venv/bin/python evals/dataplane-poc/poc.py up                     # 构建并启动，登记 ContextForge
.venv/bin/python evals/dataplane-poc/poc.py bench --warmup 60 --duration 300 --rounds 3
.venv/bin/python evals/dataplane-poc/poc.py bench --warmup 60 --duration 300 --rounds 1 \
    --targets selfbuilt-4cpu,contextforge-notrace                   # 补充对照（可选）
.venv/bin/python evals/dataplane-poc/cases.py                       # 功能用例（过期令牌用例需令牌已过期，bench 开始时预取）
.venv/bin/python evals/dataplane-poc/poc.py report                  # 汇总 results/summary.json 与 summary.md
.venv/bin/python evals/dataplane-poc/poc.py down                    # 删除 POC 容器、网络与本地构建的镜像
```

快速冒烟：`poc.py bench --warmup 5 --duration 20 --rounds 1`；`cases.py --no-wait`（跳过过期令牌用例）。

## 方法说明与已知约束

- **本机 .py 透明加密**：docker 读到的 .py 是密文，镜像构建上下文由 `poc.py` 用 Python 读出明文后打成 tar 经 stdin 交给 `docker build -`，不挂载源码。
- **压测器在 Docker 网络内运行**：Docker Desktop 的宿主机端口转发在 50 并发长连接下自身成为瓶颈并出现"Server disconnected"，
  会把网关差异淹没；压测器改在 POC 网络内的容器里运行（6 CPU 上限，实际约 1.5 CPU），基线与候选走同一路径。
- **上游是单进程 Python**：基线下上游 CPU 接近 100%，50 并发闭环的延迟主要是上游排队；"额外延迟"是同一负载下加网关后的端到端差值，
  网关自身成为瓶颈时差值会显著放大（见结果中 RPS 与网关 CPU）。
- **Span 去向**：压测时 POC 的 otel 服务只计数丢弃（避免每秒上千条 Span 灌进本机 Langfuse 造成积压）；`cases.py` 运行前切换为转发到
  compose-dev Collector → Langfuse。
- **ContextForge**：本机 Docker VM 内存约 7.6 GiB 且 compose-dev 已占约 7 GiB，ContextForge 以 1 个 gunicorn worker、1 GiB 上限运行
  （2 个 worker 时 ClickHouse 被 OOM 杀掉过一次）；压测需关闭其按账号限流与锁定（否则 429 并锁号 15 分钟）、放开 SSRF 私网地址。
  ContextForge 源码（`services/tool_service.py`）在存在活动 Trace 时对每次工具调用新建上游 MCP 会话（initialize + call），否则按下游会话复用上游会话（其注释说明复用时请求头在会话建立时固定）；`contextforge-notrace` 补充对照即不带 traceparent 的压测。
- **agentgateway**：v1.6.0 下 `extAuthz.protocol.http.metadata` 与后端 CEL `transformations` 同时启用时，上游 SSE 响应会被整段缓冲
  （进度通知不再逐条到达）；最终配置不用 metadata，判定结果按响应码记入 Span。
