# AI Capability Platform（企业级 AI 能力平台）

[English](README.md) | **简体中文**

一个开源的企业级 **AI 能力平台**：在一个地方构建、运行、治理和观测 AI Agent，并把企业能力（业务系统、数据、知识、Skill、模型）以可发现、可授权、可审计的方式开放给 Agent。

设计围绕“四个统一”展开：**统一身份 · 统一能力 · 统一运行 · 统一观测与运维**。

- **后端**：Python 3.11+ · FastAPI · SQLAlchemy 2（async）· Pydantic v2 · Ruff · Pytest
- **前端**：Vue 3 · Vite · TypeScript · Lit（Web Component SDK）· pnpm workspace
- **基础设施**：Keycloak（OIDC / Token Exchange）· Apache APISIX · LiteLLM（模型网关）· Cedar（策略即代码）· Langfuse + OpenTelemetry（观测与评估）· MySQL / Redis / MinIO / Elasticsearch · Docker Compose / Helm / Argo CD

---

## 目录

- [总体产品架构](#总体产品架构)
- [技术架构](#技术架构)
- [仓库结构](#仓库结构)
- [快速开始](#快速开始)
- [文档](#文档)
- [许可证](#许可证)

---

## 总体产品架构

五个核心层（AI 应用与触点 → Agent 运行时 → AI 能力网关 → 企业 AI 能力 → 基础设施），外加两大横向支柱（身份、权限与审计；观测、运维与评估）。

![功能架构](docs/architecture/01-%E5%8A%9F%E8%83%BD%E6%9E%B6%E6%9E%84%28%E7%AE%80%E6%98%8E%29.png)

---

## 技术架构

自上而下：触点 → 统一接入网关 → Agent 运行节点（旁侧为 Agent Console 与身份策略）→ 能力网关 / 模型网关 / 观测 → 基础设施。运行节点无状态、可水平扩展；节点内各层严格向下依赖，安全控制为横切层。

![技术架构](docs/architecture/02-%E6%8A%80%E6%9C%AF%E6%9E%B6%E6%9E%84%28%E7%AE%80%E6%98%8E%29.png)

### 架构 → 代码

| 层 | 代码 |
|---|---|
| AI 触点（门户、Console、桌面端、可嵌入组件） | `apps/portal-web`、`apps/console-web`、`apps/desktop`、`packages/sdk-web`、`packages/sdk-python` |
| Agent 运行时（Run 执行、上下文工程、工具闸门、Harness 适配） | `services/runtime`、`harness-adapters/*`、`packages/contract` |
| AI 能力网关（目录、两级发现、鉴权、Token Exchange、MCP、Skill 商店） | `services/capability-gateway`、`services/data-hub`、`mcp-servers/*`、`skills/` |
| 企业 AI 能力（模型网关、评估） | `services/model-gateway`、`evals/` |
| 基础设施（身份、存储、部署） | `services/identity`、`deploy/` |
| 横向：身份 · 权限 · 审计 | `services/policy`、`services/console-api` |
| 横向：观测 · 运维 · 评估 | `services/observe-adapter`、`evals/` |

---

## 仓库结构

```
ai-capability-platform/
├── packages/
│   ├── contract/          # Runtime Contract：事件、Run 状态机、ToolSpec、HarnessAdapter（唯一来源）
│   ├── sdk-python/        # Python 客户端：chat / create_run / get_run / approve
│   └── sdk-web/           # <ai-assistant> Lit Web Component：SSE、审批卡片
├── services/
│   ├── runtime/           # Agent 运行节点（:8080）
│   ├── console-api/       # Agent Console 后端（:8081）
│   ├── capability-gateway/# 能力网关（:8082）：发现、鉴权代理、令牌交换、MCP、Skill
│   ├── policy/            # 策略服务（:8083）：Cedar 策略与 schema、/authz/check
│   ├── observe-adapter/   # 开放上报（:8084）：反馈 / 用量 / 日志 / 状态 → Langfuse
│   ├── data-hub/          # Agent 数据中枢
│   ├── model-gateway/     # LiteLLM 配置：固定模型别名、路由、回退
│   └── identity/          # Keycloak realm 导出
├── harness-adapters/      # stdio（NDJSON 子进程）、claude-sdk（进程内）、probe（契约探针）
├── mcp-servers/           # MCP Server 模板 + 示例服务
├── skills/                # Agent Skill 包（SKILL.md + references + scripts）
├── evals/                 # 快照回放测试、数据集、安全用例
├── apps/                  # portal-web、console-web（Vue 3）、desktop（Tauri）
├── deploy/                # compose-dev、helm、argocd、apisix
├── docs/                  # 架构图（docs/architecture）· OpenAPI 规范（docs/api）
├── Makefile               # setup / test / lint / run-* / compose-* 命令
└── package.json           # pnpm workspace（apps/*、packages/sdk-web）
```

---

## 快速开始

前置条件：Python 3.11+、Node.js 与 pnpm、Docker（用于本地依赖）。

### 后端

```bash
make setup        # 创建 .venv，以可编辑模式安装全部 Python 包
make test         # 运行全部 Python 测试
make run-runtime  # 在 :8080 启动 Runtime（默认 mock Harness、SQLite、不鉴权）
```

创建并发布一个 Agent，然后与它对话（开发模式下由 `X-AI-Subject` 请求头携带身份）：

```bash
curl -s localhost:8080/admin/agents -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"agent_id":"demo","version":"1","model_alias":"claude-sonnet-2026-06","system_prompt":"你是企业助手","config":{"tools":["echo","ticket.search","ticket.close_batch"],"skills":[{"name":"ticket-summary"}]}}'
```

```bash
curl -s -X POST localhost:8080/admin/agents/demo/versions/1/publish -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"eval_run_ref":"eval-1","eval_passed":true}'
```

```bash
curl -N localhost:8080/v1/messages -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"agent_id":"demo","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

触发高风险工具并走审批（mock Harness 约定：以 `!tool <name> {json}` 开头的用户消息会发起一次工具调用）：

```bash
curl -N -D - localhost:8080/v1/messages -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"agent_id":"demo","messages":[{"role":"user","content":"!tool ticket.close_batch {\"ids\":[\"T-1\",\"T-2\"]}"}],"stream":true}'
```

响应头 `X-Run-Id` 即 Run ID；流中的 `card` 事件携带 `impact_preview` 和 `call_id`。随后调用 `/v1/runs/{run_id}/approve` 与 `/v1/runs/{run_id}/resume` 完成执行。

其他服务：

```bash
make run-console   # Console API   :8081
make run-gateway   # 能力网关      :8082
make run-observe   # 观测适配器    :8084
.venv/bin/uvicorn policy_service.main:app --port 8083 --app-dir services/policy/src   # 策略服务 :8083
```

### 前端

```bash
pnpm install && pnpm -r dev
```

`portal-web` 与 `console-web` 的 Vite 开发代理分别指向 `:8080`（Runtime）和 `:8081`（Console API）。

### 本地依赖

```bash
make compose-up                        # 基础设施 + observe profile（默认）
make compose-up COMPOSE_PROFILES=      # 仅基础设施
make compose-down
```

基础设施：MySQL、Redis、MinIO、Elasticsearch、Keycloak、Vault、LiteLLM。`observe` profile（默认开启）额外启动 Langfuse（web、worker、Postgres、ClickHouse）和 OTel Collector。仓库根目录存在 `.env` 时会被自动加载（参见 `.env.example`）。

### 完整本地环境

依赖启动后，拉起全部应用进程并写入演示数据：

```bash
.venv/bin/python scripts/dev_stack.py start    # MCP Server、网关、Runtime、Console API、观测适配器、门户、Console UI
.venv/bin/python scripts/dev_seed.py           # 演示数据（幂等）
.venv/bin/python scripts/dev_stack.py status
.venv/bin/python scripts/dev_stack.py stop
```

### Runtime 关键环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./runtime.db` | 生产环境使用 MySQL（`mysql+asyncmy://…`） |
| `HARNESS` | `mock` | `mock` / `claude-sdk` / `stdio` |
| `MODEL_GATEWAY_URL` | `http://localhost:4000` | LiteLLM Proxy |
| `CAPABILITY_GATEWAY_URL` | `http://localhost:8082` | 不可达时回退到本地工具表 |
| `POLICY_URL` | `http://localhost:8083` | 不可达时回退到内置基线规则（仅开发） |
| `TOOL_RESULT_OFFLOAD_TOKENS` | `8000` | 超过该大小的工具结果卸载到工作区并以引用返回 |
| `EGRESS_ALLOWLIST` | 空 | 逗号分隔的出网白名单 |
| `AUTH_REQUIRED` / `JWKS_URL` | `false` / 空 | 为 `true` 时强制校验 JWT 签名 |
| `TOPOLOGY` / `NODE_ID` | `central` / `node-local` | 节点拓扑：central / hybrid / standalone |

优先级：数据库配置 > 环境变量 > 代码默认值；密钥只来自 Vault。

### 测试与质量

```bash
make test                        # 全部 Python 测试（含快照回放）
make lint                        # Ruff + mypy
pnpm -r build                    # 构建前端包
make security                    # Bandit、依赖审计、出网审计、安全测试
```

---

## 文档

| 路径 | 内容 |
|---|---|
| [docs/architecture/](docs/architecture/) | 架构图：[功能](docs/architecture/01-功能架构%28简明%29.png) · [技术](docs/architecture/02-技术架构%28简明%29.png) · [产品](docs/architecture/03-总体产品架构%28详图%29.png) · [系统组件](docs/architecture/04-技术架构%28系统组件详图%29.png) · [核心链路](docs/architecture/05-核心链路.png) · [部署](docs/architecture/06-部署架构.png) · [数据](docs/architecture/07-数据架构.png) · [建设路线](docs/architecture/08-建设路线.png) |
| [docs/api/](docs/api/) | OpenAPI 规范：[Runtime](docs/api/runtime-openapi.json) · [能力网关](docs/api/gateway-openapi.json) |
| [harness-adapters/stdio/PROTOCOL.md](harness-adapters/stdio/PROTOCOL.md) | stdio Harness 协议 |
| [skills/README.md](skills/README.md) | Skill 包格式与供应链规则 |
| [deploy/helm/README.md](deploy/helm/README.md) · [deploy/apisix/README.md](deploy/apisix/README.md) | 部署 |

---

## 许可证

[MIT](LICENSE) © HansenUp
