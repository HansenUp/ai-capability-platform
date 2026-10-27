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

启动 MySQL、Redis、MinIO、Elasticsearch、Keycloak、LiteLLM、OTel Collector；加 `--profile observe` 启动 Langfuse。

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

已覆盖的行为：契约状态迁移；SSE 对话；高风险写审批与跨 Worker 重放恢复；Cedar 授权与 OBO；Skill 扫描、评测、双人审批、签名上架、验签物化与吊销；沙箱隔离、出站白名单、数据分级；并行工具调用和上下文压缩后的日志重建。

S9 验证入口：`make verify-s9`（需本机依赖，默认 MinIO 与真实模型，会产生模型费用）；`S9_MODEL=scripted SKILL_STORE=fs make verify-s9` 使用脚本化模型与临时文件存储。`make security` 运行 Bandit、联网依赖审计、Runtime 出站代码审计与安全单测；全栈安全用例由 `verify-s9` 启动专用测试栈执行。

真实 Langfuse 专项：`.venv/bin/python scripts/verify_s9_e2e.py --subset langfuse`，模型采用脚本化，Trace 经本机 OTel Collector 写入真实 Langfuse；检查评分重算、低分拒绝、Trace 的 Agent / 版本 / 租户绑定和跨条目复用拒绝。`S9_LANGFUSE=true` 可为完整 S9 验收启用真实评测。数据集使用独立随机前缀，保留验收证据；默认连接本机开发项目，可通过 `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 指定项目，Collector 须对应同一项目。

签名写脚本专项：`.venv/bin/python scripts/verify_s9_e2e.py --subset script-writes`，验证真实 Docker 写入、持久审批、MySQL 写前登记、`kill -9` Worker 后接管和人工对账。写脚本全部按高风险写处理，结果未知时禁止重试；对账先清理原操作容器，`skip` 不撤销副作用。故障接管须共享 Docker 引擎及同路径持久工作区；完整约束见 `skills/README.md`。

S10.3 本机节点控制：`make verify-s10-node`，需要 Docker 与已缓存的 `redis:7-alpine` 镜像；使用随机回环端口、临时库及独立 Redis 容器，不调用真实模型。覆盖版本化模型切换、在途 Run 保持原版本、固定版本不受影响、排空后持久回执、实际新 PID／boot_id 注册，以及纯 Worker 重启。安全契约见 `docs/decisions/2026-10-18-S10节点控制安全契约.md`。

S10.4 真实本机网关：预先拉取官方 `apache/apisix:3.11.0-debian` 与 `redis:7-alpine`，执行 `make verify-s10-routing`（Docker Desktop／macOS）。入口仅随机回环端口，使用临时 Console／双 Runtime／共享 SQLite／Redis、RSA JWKS 与模型／策略／工具桩；验证租户服务拉取鉴权、静态节点白名单、权重与失败关闭、原节点退出后的审批恢复及 SSE、缓存与 POST 不重试。结束清理自有容器／进程，不启用 Helm／Argo CD。

插件与状态池约束见 `docs/decisions/2026-10-18-S10路由契约.md`。路由拉取只调用 `/internal/routing`，服务 JWT 须为 `ai-routing-svc`、含 `routing:read` 及单一租户；Runtime 保持 JWT 鉴权。静态节点映射不是 Console 注册自动发现的连接地址；生产服务客户端、令牌轮换、TLS／mTLS、容量及网络隔离仍待验收。不要开启会记录令牌的 OIDC debug 日志，含凭据的临时 YAML 不进入证据或版本库。

S10.5 闸门复核：`make verify-s10-eval` 只读既有本机 Langfuse 中已归档的 S9 运行，不创建评测数据或服务资源；Runtime 同步边界用明确替身。默认阈值仍为 `EVAL_MIN_ITEMS=30`、`EVAL_MIN_PASS_RATE=0.8`，非法／非有限配置、分页不完整及坏评分失败关闭。管理员通过 `GET /agents/{agent_id}/eval-gate` 查看进程级非秘密策略，成功发布／灰度记录策略审计；没有在线调阈值接口，生产标准仍需人工签署。

S10.6 管理员分配：`GET／PUT /agents/{agent_id}/admins` 由当前租户的租户管理员及以上查询／替换归属，空 users 数组撤销；普通 ADMIN 仅管理和列出已分配 Agent。此接口不创建身份账号、不修改角色，不把开发身份头视为生产鉴权。

S10.6 只读运营接口：`GET /audit/search` 支持本租户精确过滤、带时区时间窗及 `before_id` keyset 分页，返回最新优先的 items／next_before_id；旧 `/audit` 列表契约保留。`GET /metrics/operations` 仅提供租户 Console 控制面计数，Runtime 执行率／延迟／Token 统计明确不可用，不是零值。不进行生产部署。

S10.6 本机 Trace 指标：`GET /metrics/traces?agent_id=...&since=...&until=...` 只读既有本机 Langfuse v3，需要租户管理员、当前租户 Agent 和不超过 7 天的带时区时间窗；完整分页后统计匹配的 Trace、GENERATION、Token 与 USD 费用。未知总量为 null，覆盖数和已知 Trace 小计单列；Trace 不等于 Run，不提供 Run 成功率或费用结算。超限／来源故障 503，不截断为“完整结果”。`make verify-s10-traces` 可复核真实只读来源。

现有 Agent 页面已将草稿保存与发布拆开，读取闸门策略、填写真实评测引用，支持灰度白名单与页面内发布／回滚确认。Langfuse 模式不发送伪造的手动评测通过；开发 flag 模式必须显式勾选。`make verify-s10-publishing` 启动隔离临时 Console→Runtime 验证；正向复用 12 条归档样本的阈值只作用该测试进程，项目默认仍为 30 条／80%。完整边界与执行报告见 `docs/decisions/2026-10-20-S10指标与发布页执行报告.md`。

S10.6 目录范围已由用户确认仅 Console 映射元数据：`/manage/directory` 页面提供当前租户展示信息和外部 issuer／subject 用户映射的增改删与分页。接口为 `GET／PUT /directory/tenant`、`GET／POST /directory/users`、`PUT／DELETE /directory/users/{id}`，仅当前租户 TENANT_ADMIN 及以上可访问；身份不可变、重复映射 409、跨租户 404，变更与审计同事务。删除仅移除展示记录，不撤销既有 Agent 分配、角色、令牌或 Run；不会创建／停用真实 IdP 账号。Console 迁移升级至 `0006`，不会从旧分配记录推断身份目录。安全契约见 `docs/decisions/2026-10-19-S10Console目录契约.md`。

本机运行受管节点：先配置持久数据库、Redis、Console 与节点服务鉴权，再执行 `NODE_ID=node-demo .venv/bin/python -m runtime.node --role all --port 8080`（支持 api／worker／all）。它不是生产 supervisor，拒绝生产模式；普通 uvicorn 启动不接受远程重启。模型切换只选择已发布的全量版本，不改已发布快照或在途 Run。D47 和生产部署继续延期。

---

## 9. 当前边界（按工作包推进）

| 项目 | 现状 | 计划 |
|---|---|---|
| 身份与策略 | JWT / JWKS、服务令牌、Cedar sidecar 与 OBO 已实现；开发态保留显式替身 | S2 / S8 本机验证 |
| Harness 与持久执行 | Claude SDK / stdio 适配、Redis Streams、MySQL 租约、跨节点接管已实现 | S5–S7 本机验证 |
| 能力目录与 Skill | MySQL + ES 目录；签名包、扫描、审批、验签与吊销链路已实现 | S8 / S9；验收范围见阶段纪要 |
| 安全与节点 | 应用层出站控制、Docker L1 沙箱、节点服务鉴权与拉取指令已实现 | S9；集群网络隔离尚未验收 |
| 观测与评测 | Langfuse / OTel 接入；未配置 Langfuse 时仅允许开发态本地评测 | 正式环境需单独验收 |
| APISIX 与集中式投产 | 路由插件已完成真实本机验证，生产部署与外部边界未验收；Helm / Argo CD 延期 | S10 / M2 |
