# 企业级 AI 能力平台（ai-capability-platform）

按《企业级 AI 能力平台建设方案 V1.2》《开源项目与最佳实践调研报告》《技术实现方案》搭建的单仓多包（monorepo）工程。仓库同时存放**设计文档**与**代码骨架**，代码中的模块划分、命名与状态机与设计文档一一对应。

- 后端：Python 3.12 · FastAPI · SQLAlchemy 2（async）· Pydantic v2 · Ruff · Pytest
- 前端：Vue 3 · Vite · TypeScript · Lit（Web Component SDK）· pnpm 11 工作区
- 基础设施：Keycloak（OIDC / Token Exchange）· LiteLLM（模型网关）· Cedar（策略即代码）· Langfuse + OpenTelemetry（观测评测）· MySQL / Redis / MinIO / Elasticsearch · Docker Compose / Helm / Argo CD

---

## 1. 设计文档

| 位置 | 内容 |
|---|---|
| `docs/design/企业级AI能力平台建设方案_V1.2.docx` | 产品方案 + 技术方案（需求基线） |
| `docs/design/企业级AI能力平台_开源项目与最佳实践调研报告.docx` | Agent / Harness 开源生态调研与选型依据 |
| `docs/design/企业级AI能力平台_技术实现方案.docx` | 语言、框架、开源组件选型与 Phase 0–4 实施路径 |
| `docs/design/企业级AI能力平台技术架构图_V1.2.html` | 可在浏览器打开的架构图页面 |
| `docs/architecture/01…08-*.png` | 功能架构、技术架构、总体产品架构、系统组件、核心链路、部署、数据、建设路线 |
| `docs/design/企业级AI能力平台_平台定位路线调研报告.docx` | 治理与供给平台对比 Agent 能力平台，执行循环归属的结论依据（ADR-0007、0008 的依据） |
| `docs/plan/实施计划.md` | **完整实施计划**：Phase 0–4 / S1–S22 工作包、任务清单、出口标准、门禁、进度跟踪表（后续开发严格按此执行） |
| `docs/adr/` | 架构决策记录：0001 Cedar 判定服务化、0002 Langfuse 事实源、0003 APISIX 插件、0004 stdio Harness 选型、0005 能力网关数据面 POC、0006 模型别名规范、0007 平台定位、0008 执行循环归属与适配器分级、0009 授权合取与范围审批、0010 数据分级全出口、0011 执行者身份与副作用可回滚 |

---

## 2. 架构分层与代码映射

设计上的五层两纵在代码中的落点：

| 设计分层 | 代码位置 |
|---|---|
| AI 触点层（门户、Console、桌面端、嵌入组件） | `apps/portal-web`、`apps/console-web`、`apps/desktop`、`packages/sdk-web`、`packages/sdk-python` |
| Agent Runtime（Run 执行、上下文工程、工具闸口、Harness 适配） | `services/runtime`、`harness-adapters/*`、`packages/contract` |
| AI Capability Gateway（能力目录、两级发现、授权、Token Exchange、MCP、Skill 商店） | `services/capability-gateway`、`mcp-servers/template`、`skills/` |
| 企业 AI 能力层（模型网关、知识 / 检索、评测） | `services/model-gateway`、`evals/` |
| 基础设施（身份、存储、消息、部署） | `services/identity`、`deploy/` |
| 纵向：身份 · 权限 · 审计 | `services/policy`、`services/console-api`（角色链 / 审计）、`services/runtime/api/auth.py` |
| 纵向：观测 · 运营 · 评测 | `services/observe-adapter`、`evals/`、Console 发布闸门 |

---

## 3. 目录结构与文件说明

```
ai-capability-platform/
├── Makefile                      # setup / test / lint / run-* / compose-* 常用命令
├── pyproject.toml                # 根级 Ruff、mypy、pytest 配置（各包各自有 pyproject）
├── package.json / pnpm-workspace.yaml   # 前端工作区（apps/*、packages/sdk-web）
├── .github/workflows/ci.yml      # CI：Python lint + 测试，前端构建
├── docs/                         # 设计文档、架构图、ADR、实施计划（docs/plan/实施计划.md）
│
├── packages/
│   ├── contract/                 # Runtime Contract（唯一来源，其他包只依赖它）
│   │   └── src/ai_contract/
│   │       ├── events.py         # 统一事件流：text / thinking / tool_use / tool_result / card / usage / error / done
│   │       ├── run.py            # Run 状态机（A2A 命名）、合法迁移表、Run 事件日志类型 RunEvent
│   │       ├── tools.py          # ToolSpec（风险级别、沙箱级别、数据分级、dry-run、幂等键）、SkillDir
│   │       └── harness.py        # HarnessAdapter 协议：capabilities / open_session / run_turn / interrupt / resume
│   ├── sdk-python/               # PlatformClient：chat / create_run / get_run / approve
│   └── sdk-web/                  # Lit 组件 <ai-assistant>：SSE 消费、审批卡片、approve 回调
│
├── services/
│   ├── runtime/                  # Agent Runtime 节点（:8080）
│   │   └── src/runtime/
│   │       ├── main.py           # FastAPI 入口：建库、加载 Harness、挂载路由、/healthz
│   │       ├── config.py         # Settings：环境变量 > 代码默认（数据库层覆盖在启动时应用）
│   │       ├── api/
│   │       │   ├── messages.py   # POST /v1/messages（SSE）；Run API：创建 / 查询 / 事件 / approve / reject / resume / cancel
│   │       │   ├── admin.py      # /admin/agents 版本写入与发布（评测闸门）、/admin/node
│   │       │   └── auth.py       # Principal 解析：JWT（sub + act）或开发态 X-AI-Subject
│   │       ├── loop/runner.py    # Run 驱动器：组装 → 回合循环 → 工具闸口 → 执行 → 落日志；从事件日志重放恢复
│   │       ├── state/
│   │       │   ├── db.py         # 表：t_agent_version / t_run / t_run_event / t_audit
│   │       │   └── eventlog.py   # 追加式事件日志、状态迁移校验、幂等键查重、统一事件投影
│   │       ├── run/invariant.py  # 可重建不变量：模型可见上下文哈希 == 由日志重建的哈希
│   │       ├── context/budget.py # 固定块顺序、Token 预算、稳定前缀哈希（提示缓存）、大结果外置 artifact://
│   │       ├── guard/gate.py     # 工具闸口：hooks → deny → 策略引擎 → 风险级别 → 审批 → 幂等键 → 沙箱级别
│   │       ├── assemble/assembler.py  # 能力组装：核心工具预载 + search_capabilities / read_artifact 元工具、Skill 加载
│   │       └── harness/          # registry（mock | claude-sdk | stdio）、mock.py（本地 / CI 用无模型 Harness）
│   ├── console-api/              # Agent Console 后端（:8081）：四级角色、草稿 / 发布 / 回滚 / 可见性、节点注册 / 心跳 / 指令、路由表、审计
│   ├── capability-gateway/       # 能力网关（:8082）：/capabilities 两级发现、/authz/check 代理、/token/exchange、/mcp/servers、/skills
│   ├── policy/                   # 策略服务（:8083）：cedar/ 策略与 Schema、Python 基线评估器、/authz/check、判定记录
│   ├── observe-adapter/          # 开放上报：/feedback、/observe/usage|logs|status → Langfuse（未配置时缓冲）
│   ├── model-gateway/            # LiteLLM 配置：版本固定别名、路由、回退
│   └── identity/                 # Keycloak realm 导出：客户端、Token Exchange 权限
│
├── harness-adapters/
│   ├── stdio/                    # NDJSON 子进程适配器（PROTOCOL.md）、hook 往返、重放式恢复、echo_harness 参考实现
│   └── claude-sdk/               # 进程内适配器骨架（SDK 可选依赖，回合循环为 S5 工作包）
│
├── mcp-servers/template/         # MCP Server 模板：幂等键、dry_run 影响预演、补偿工具声明、OBO 身份校验
├── skills/                       # Agent Skills 规范目录包（SKILL.md + references + scripts），示例 ticket-summary
├── evals/                        # 快照式事件日志（.jsonl）回放测试
│
├── apps/
│   ├── portal-web/               # 用户门户（Vue 3）：Agent 列表、对话
│   ├── console-web/              # 双工作台 Console（Vue 3）：Agent 管理、节点、对话调试
│   └── desktop/                  # Tauri 桌面端配置骨架
│
└── deploy/
    ├── compose-dev/              # 本地依赖：MySQL、Redis、MinIO、ES、Keycloak、LiteLLM、OTel Collector，Langfuse 可选 profile
    ├── helm/ai-platform/         # Helm Chart：节点组（nodeGroups）、gVisor RuntimeClass
    └── argocd/                   # Argo CD Application
```

---

## 4. 核心链路（代码已验证）

```
客户端 ──POST /v1/messages──▶ Runtime.api.messages
                               │ create_run → t_run + run.created
                               ▼
                         loop.runner.drive
      ┌────────────────────────┴────────────────────────┐
      │ assemble（系统提示 + 核心工具 + Skills）            │
      │ context.budget（块顺序 / 预算 / 稳定前缀哈希）        │
      │ invariant.check（模型可见上下文 == 日志重建）         │
      │ harness.run_turn → 统一事件流（SSE 透传）            │
      │ tool_use → guard.gate                             │
      │     read       → 直接执行 → 大结果外置 artifact://   │
      │     write_high → dry-run 影响预演 → card(impact_preview)
      │                 → Run 进入 auth-required，流结束      │
      └───────────────────────────────────────────────────┘
审批 ──POST /v1/runs/{id}/approve──▶ tool.approved + working
恢复 ──POST /v1/runs/{id}/resume ──▶ 重放事件日志 → 先完成已审批调用（幂等键去重）→ 继续回合
```

Run 状态：`submitted → working → (auth-required | input-required) → working → completed | failed | canceled`，迁移表在 `packages/contract/src/ai_contract/run.py`，非法迁移直接拒绝。

---

## 5. 快速开始

### 5.1 后端

```bash
make setup        # 创建 .venv 并以可编辑模式安装全部 Python 包
```

```bash
make test         # 全部 Python 测试
```

```bash
make run-runtime  # 启动 Runtime（默认 mock Harness、SQLite、无鉴权）
```

创建并发布一个 Agent，然后对话（开发态用 `X-AI-Subject` 头表示身份）：

```bash
curl -s localhost:8080/admin/agents -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"agent_id":"demo","version":"1","model_alias":"chat-2026-06","system_prompt":"你是企业助手","config":{"tools":["echo","ticket.search","ticket.close_batch"],"skills":[{"name":"ticket-summary"}]}}'
```

```bash
curl -s -X POST localhost:8080/admin/agents/demo/versions/1/publish -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"eval_run_ref":"eval-1","eval_passed":true}'
```

```bash
curl -N localhost:8080/v1/messages -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"agent_id":"demo","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

触发高风险工具并走审批（mock Harness 约定：用户消息以 `!tool <name> {json}` 开头即发起工具调用）：

```bash
curl -N -D - localhost:8080/v1/messages -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"agent_id":"demo","messages":[{"role":"user","content":"!tool ticket.close_batch {\"ids\":[\"T-1\",\"T-2\"]}"}],"stream":true}'
```

响应头 `X-Run-Id` 为 Run 编号，事件流中的 `card` 事件携带 `impact_preview` 与 `call_id`；随后调用 `/v1/runs/{run_id}/approve` 与 `/v1/runs/{run_id}/resume` 完成执行。

其他服务：`make run-console`（:8081）、`make run-gateway`（:8082）；策略服务与观测适配器可用 `uvicorn policy_service.main:app --port 8083 --app-dir services/policy/src` 等方式启动。

### 5.2 前端

```bash
pnpm install && pnpm -r dev
```

`portal-web` 与 `console-web` 的 Vite 代理已指向 `:8080`（Runtime）与 `:8081`（Console API）。

### 5.3 本地依赖

```bash
make compose-up
```

启动 MySQL、Redis、MinIO、Elasticsearch、Keycloak、LiteLLM、OTel Collector；加 `--profile langfuse` 启动 Langfuse。

---

## 6. 关键环境变量（Runtime）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./runtime.db` | 生产改为 MySQL（`mysql+asyncmy://…`） |
| `HARNESS` | `mock` | `mock` / `claude-sdk` / `stdio` |
| `MODEL_GATEWAY_URL` | `http://localhost:4000` | LiteLLM Proxy |
| `CAPABILITY_GATEWAY_URL` | `http://localhost:8082` | 不可达时回退到本地工具表 |
| `POLICY_URL` | `http://localhost:8083` | 不可达时回退到内置基线规则（仅开发） |
| `TOOL_RESULT_OFFLOAD_TOKENS` | `8000` | 超过则把工具结果外置到工作区并只回传引用 |
| `EGRESS_ALLOWLIST` | 空 | 逗号分隔的出网白名单 |
| `AUTH_REQUIRED` / `JWKS_URL` | `false` / 空 | 为 `true` 时强制 JWT 验签（S2 工作包） |
| `TOPOLOGY` / `NODE_ID` | `central` / `node-local` | 节点拓扑：central / hybrid / standalone |

配置优先级：数据库配置 > 环境变量 > 代码默认；敏感值只来自 Vault。

---

## 7. 开发约定

- 事件、Run 状态、ToolSpec、Harness 协议只在 `packages/contract` 定义，其他包只能依赖它，不得各自复制。
- 事件日志为追加式且是唯一事实源；任何恢复、审计、评测都从日志重建，不允许绕过日志改状态。
- 写操作工具必须声明 `risk_level`；`write_high` 必须支持 `dry_run` 并要求幂等键。
- 新增 Skill 必须满足供应链硬规则：签名、能力声明、禁止运行时远程拉取、通过静态扫描、附带评测集。
- 代码注释与文档使用中文；标识符、API 字段、协议术语保持英文。
- 每个 PR 关联需求编号（如 `RT-24`），提交遵循 Conventional Commits；`ruff check` 与全部测试必须通过。

---

## 8. 测试与质量

```bash
.venv/bin/python -m pytest -q      # 全部 Python 测试（含快照回放）
```

```bash
.venv/bin/ruff check . && pnpm -r build
```

已覆盖的行为：契约状态迁移；SSE 对话；只读工具执行与结果外置；高风险写操作挂起 → 审批 → 重放恢复 → 幂等去重；非法迁移拒绝；发布评测闸门；策略基线判定；Skill 供应链 422 规则；stdio 协议探针；MCP 模板幂等与 dry-run。

---

## 9. 当前边界（按工作包推进）

| 项目 | 现状 | 计划 |
|---|---|---|
| JWT / JWKS 验签 | 开发态头部身份，`AUTH_REQUIRED=true` 返回 501 | S2 |
| Claude Agent SDK 适配器回合循环 | 骨架，SDK 缺失时 ImportError | S5 |
| Run 领取与 Worker（Redis Streams） | API 内后台任务执行 | S7 |
| Console / 网关持久化 | 内存存储 | S8 / S10 迁移 MySQL |
| MCP 分发（OBO Token） | 开发态本地执行器 | S6 |
| Cedar 引擎绑定 | Python 基线评估器 + HTTP 判定服务 | ADR-0001，S3 |
| Langfuse / OTel 转发 | 未配置时本地缓冲 | S4 |
| APISIX 路由插件 | 路由表 API 已就绪 | ADR-0003，S9 |
