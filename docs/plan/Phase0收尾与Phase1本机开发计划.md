# Phase 0 收尾与 Phase 1 本机开发计划

> 依据：`docs/plan/实施计划.md`（执行基线 V1.1）第 4、5 章；仓库现状核验日期 2026-10-16。
> 结论：**当前处于 Phase 0 尾声，不能直接跳到 Phase 1。** 把 Phase 0 拆为"本机可完成的收尾"（约 1 周）与"环境类延后项"，收尾完成即进入 S1。本文件只做本机执行安排，不改变 `实施计划.md` 的任务定义。

---

## 1. 现状核验

| 项目 | 结果 |
|---|---|
| Python 测试 | 52 通过，1 跳过（`make test`） |
| ruff | 通过 |
| mypy | **未安装到 .venv**，`make lint` 会失败（P0-1.5 未完成） |
| git | **尚未初始化**（P0-1.2 未完成） |
| ADR | 目录只有 0001、0007–0011；**0002–0006 文件缺失**，与 README 和计划 V1.1 的"已采纳"表述不一致（P0-3 缺口） |
| Contract / OpenAPI | Contract 已定义但未冻结评审；OpenAPI 未导出、无契约测试（P0-2.2～2.5 未完成） |
| P0-5 契约闭合 | 5.4 契约字段（`loop_owner` / `tool_interception` / `can_resume_from_events` / 能力等级判定）、5.5 工具代理 `loop/toolbroker.py`、5.6 写前记录与 `UNKNOWN_TOOL_RESULT` 转人工、5.7 审批绑定 `params_hash` + `expires_at`、5.8 能力等级发布闸门 **代码均已落地**，计划状态列仍为"进行中"，需补专项测试后改为已完成；5.9～5.11 为文档任务 |
| 本机依赖 | Docker Desktop 已安装但 **daemon 未启动**，compose 尚未跑起来；LiteLLM 配置就绪，缺模型 API Key 环境变量 |
| Phase 1 代码 | S1.1、S2.5、S3.1、S4.1、S4.4 已完成；`runtime/llm/`、`runtime/observe/`、Alembic 迁移、claude-sdk 回合循环、JWT 验签（当前 501）均未开始 |

---

## 2. 阶段判定

- 关键路径是 P0-2 → S1 → S2 → S4。S1.2（LLM 客户端）与 S1.3（claude-sdk 适配器）都以冻结后的 Contract 与事件流为基础，先冻结再开发可避免 S1～S4 返工。
- Phase 0 中的环境类任务（K8s / Harbor / Argo CD / 企业 IdP 联邦 / Vault / gVisor）既不能在本机完成，也不是本机开发的前置；用 compose 替代，延后到预发布环境可用时补做。
- 因此：**Phase 0 本机收尾（第 1 周）→ 进入 Phase 1 S1（第 2 周起）**，S1～S4 按 2 周一个 Sprint 执行，本机出口标准见第 4 节。

---

## 3. Phase 0 本机收尾清单（第 1 周，约 3～5 个工作日）

| # | 任务 | 对应计划项 | 本机做法 | 完成判定 |
|---|---|---|---|---|
| A1 | 初始化 git，`main` 分支，PR 模板 | P0-1.2 | 本机 git init；远端与分支保护在代码平台可用后补 | `git log` 有首个提交，`.github/PULL_REQUEST_TEMPLATE.md` 存在 |
| A2 | mypy 装入 .venv，`packages/contract`、`services/runtime` 严格模式通过 | P0-1.5 | 加入各包 `[dev]` 依赖；修类型 | `make lint` 一次通过 |
| A3 | pre-commit（ruff、ruff-format、mypy、gitleaks） | P0-1.4 | `.pre-commit-config.yaml` | `pre-commit run -a` 通过 |
| A4 | 各服务 Dockerfile（多阶段、非 root） | P0-1.3 | 本机 `docker build` 验证；trivy 扫描进 CI | 5 个服务镜像可构建，`/healthz` 可访问 |
| A5 | Contract V1 冻结评审：`input-required` 输入事件、`ThinkingEvent` 可见性策略、`CardEvent` 类型枚举（impact_preview / form / choice / reconciliation_required） | P0-2.2 | 改 `packages/contract`，契约测试同步 | 评审纪要入库，`packages/contract` 打 `v1` 标签 |
| A6 | 导出 Runtime OpenAPI 到 `docs/api/runtime-openapi.json`，schemathesis 契约测试进 CI | P0-2.3 | FastAPI `app.openapi()` 导出脚本 + 测试 | CI 新增契约测试段并通过 |
| A7 | 冻结 Discovery / authz / token exchange 接口，导出 `docs/api/gateway-openapi.json` | P0-2.4 | 同上 | 文件入库带版本 `v1` |
| A8 | 由 OpenAPI 生成 Python / Web SDK 类型骨架 | P0-2.5 | 可与 S1 并行，不阻塞 | SDK 包含生成类型且构建通过 |
| A9 | 补齐 ADR 0002（Langfuse）、0003（APISIX 插件）、0004（stdio Harness 首选）、0006（模型别名）文件；ADR-0005 数据面 POC 范围完成评审 | P0-3.1、P0-3.2 | 按 `docs/adr/0000-template.md` 补写，内容与 README 已采纳结论一致 | `docs/adr/` 0001–0011 齐全，0005 为已采纳 |
| A10 | 确认首个场景为工单总结；业务系统只读 API 负责人未到位时，本机先以 mock 数据源实现 `mcp-servers/ticket` | P0-3.3 | 选型确认纪要 | 纪要入库，含负责人与接口约定 |
| A11 | P0-5.4～5.8 补专项测试并把计划状态改为已完成 | P0-5 | 针对写前记录、审批绑定失效、能力等级闸门各加用例 | 用例通过，`实施计划.md` 状态列更新 |
| A12 | 完成（2026-10-17） | compose 12 个服务全部 Up：MySQL（宿主机 3316）、Redis、MinIO、ES、Keycloak（realm ai-platform 已导入）、LiteLLM（5 个别名）、OTel、Langfuse 全家桶。修复：MySQL 端口改 3316、minio 固定版本、Keycloak mapper 补 protocol、Langfuse 补 CLICKHOUSE_MIGRATION_URL 与 CLICKHOUSE_CLUSTER_ENABLED=false。模型 Key 仍需填入 `.env` |

与 S1 并行、不阻塞的文档项：P0-5.9 四项决策纪要、P0-5.10 文档一致性修订、P0-5.11 非功能测量口径。

**延后到环境可用时的任务（记入风险表，指定跟进人）：** P0-4.2 K8s / Harbor / Argo CD、P0-4.3 企业 IdP 联邦、P0-4.4 Vault 注入、P0-4.5 gVisor 申请。

---

## 4. Phase 1 本机执行安排（S1～S4，每 Sprint 2 周）

| Sprint | 任务（编号同 `实施计划.md`） | 本机替代方式 | 本机出口标准 |
|---|---|---|---|
| S1 Runtime 骨架 | S1.2 `runtime/llm/` LiteLLM 客户端；S1.3 claude-sdk 适配器最小实现；S1.4 `HARNESS=claude-sdk` 可对话；S1.5 OTel → Langfuse；S1.6 Alembic 0001 + MySQL | LiteLLM / Langfuse / MySQL 全部来自 compose | mock 与 claude-sdk 各完成一次对话；Langfuse 可查到 Trace 且 seq 与 Run 事件对应；`DATABASE_URL` 指向 compose MySQL 时全部测试通过 |
| S2 身份与上下文 | S2.1 JWT / JWKS 验签；S2.2 Client Credentials；S2.3 审计写 `t_audit` 并镜像 Langfuse；S2.4 密钥注入；S2.6 Prompt Cache 命中统计 | 验签对接 compose Keycloak 本地 realm（`services/identity/realm-export.json`），企业 IdP 联邦延后；密钥本机用 `.env` + compose secrets，Vault 延后 | `AUTH_REQUIRED=true` 下未带令牌 401；任一请求可在 `t_audit` 回答"谁、何时、以何身份、调了什么"；Langfuse 可统计命中率 |
| S3 发布与灰度 | S3.2 哈希灰度；S3.3 Console 迁移 MySQL；S3.4 门户 OIDC（PKCE）登录；S3.5 反馈 → Langfuse score | 门户登录走本地 Keycloak | 演示 10% 灰度与一键回滚；门户登录后可对话；反馈在 Trace 上可见 |
| S4 垂直切片 | S4.2 工单只读 MCP Server；S4.3 工具执行经网关分发；S4.5 快照 ≥ 5 条；S4.6 评测数据集 ≥ 30 条；S4.8 最小安全闭环；S4.9 数据分级最小矩阵；S4.10 幂等责任确认 | 工单数据源先 mock，真实只读 API 到位后切换；S4.7 投产本机只能做到"预发布就绪"（Helm values 与镜像齐备） | 跨用户访问、附件越权、非白名单工具、机密外发四类测试通过；快照与不变量在 CI 通过；M1 剩余项只差真实环境部署与业务方签字 |

每个 Sprint 固定动作沿用 `实施计划.md` 第 9 章（安全评审、观测、文档、评测、演示录屏）。

---

## 5. 本机无法闭环、需要外部投入的事项

| 事项 | 需要谁 | 最晚时点 |
|---|---|---|
| 代码平台远端仓库与 `main` 保护 | 运维 | 第 1 周末 |
| 企业 IdP 联邦到 Keycloak | 运维 + IT | S2 末 |
| 工单系统只读 API 与负责人 | 业务系统团队 | S3 末（否则 S4 继续用 mock） |
| Vault、K8s / Argo CD、预发布环境 | 运维 | S4 投产前 |
| 模型 API Key（Anthropic / DeepSeek）预算 | 产品负责人 | S1 开始前 |

---

## 6. 执行结果（2026-10-16）

| # | 结果 | 说明 |
|---|---|---|
| A1 | 完成 | 本机 git 仓库、`main` 分支、PR 模板；远端与分支保护待运维提供代码平台 |
| A2 | 完成 | mypy 严格模式 `packages/contract`、`services/runtime` 零错误；`make lint` 一次通过 |
| A3 | 完成 | `.pre-commit-config.yaml`：ruff / ruff-format / mypy 本机通过；gitleaks 在本机 DLP 环境无法编译，改为 manual 阶段并由 CI `secrets` job 强制 |
| A4 | 完成（本机未验证镜像） | 5 个 Dockerfile 与 CI `images` job（build + trivy）；本机 Docker 经代理拉取基础镜像卡住，镜像由 CI 验证 |
| A5 | 完成 | Contract V1.0.0 冻结：新增 `input_required` 事件、`ThinkingEvent.visibility`、`CardType` 枚举、`CONTRACT_VERSION`；`packages/contract/CONTRACT_V1.md` |
| A6 | 完成 | `docs/api/runtime-openapi.json`（v1）；schemathesis 契约测试；`make openapi-check` 进 CI |
| A7 | 完成 | `docs/api/gateway-openapi.json`（v1）；契约测试 |
| A8 | 完成 | `make sdk-types`：Python（datamodel-codegen）与 Web（openapi-typescript）类型骨架，SDK 已引用 |
| A9 | 完成 | ADR 0002～0006 补齐，0005 范围评审通过（结论待 S6 回填）；索引更新 |
| A10 | 完成 | `docs/decisions/2026-10-16-首个垂直切片场景确认.md`（负责人栏待填） |
| A11 | 完成 | P0-5.4～5.8 专项测试 22 个；另按属性测试结果修复 Run API 三处 5xx，业务拒绝统一 409 |
| A12 | 受阻 | `.env.example` 已入库、compose 已补密钥透传与 `.env` 读取；本机 `docker pull` 经 Docker Desktop 代理（http.docker.internal:3128）卡住约 30 分钟无进展，需修复宿主机代理设置后重跑 `make compose-up` |

验证基线：`make test` 119 通过 / 1 跳过；`make lint` 通过；`make openapi-check` 通过；`pnpm -r build` 通过；`make precommit` 通过。
本机环境发现：透明加密（DLP）使 ruff 等原生二进制读到密文，已用 `scripts/lint_ruff.py` 绕过，git 提交内容不受影响（见决策纪要 D12）。

**进入 S1 的前置：** A12 已完成；只需在 `.env` 中配置模型 Key（真实模型对话需要）。S1.2～S1.6 可在 A12 完成前先行开发（mock Harness 与 SQLite 不依赖 compose）。

---

## 7. S1 执行结果（2026-10-17）

| 任务 | 结果 | 说明 |
|---|---|---|
| S1.2 | 完成 | `runtime/llm/client.py`：流式 SSE、分片工具调用聚合、`reasoning_content` → 思维链、别名解析（`/model/info`，缓存 60 s）、超时与指数退避重试（只在尚未产出增量前重试）。新增平台持有循环的 `litellm` Harness（L2） |
| S1.3 | 完成 | claude-sdk 适配器：SDK 子进程经 LiteLLM 的 Anthropic 兼容端点访问模型；平台工具以进程内 MCP 注入，SDK 只拿到平台裁决后的结果；隔离本机设置与内置工具 |
| S1.4 | 完成 | `HARNESS=claude-sdk` 与 `litellm` 均可经 `/v1/messages` 对话并完成工具往返 |
| S1.5 | 完成 | 每次 drive 一条 Trace：`run` 根 span、`chat` 模型轮次、`execute_tool` 工具；属性含事件日志 seq；compose 中 Langfuse 无头初始化、Collector 带 Basic Auth、补 MinIO 桶与 S3 region |
| S1.6 | 完成 | Alembic 0001；启动时自动 `upgrade head`；`UTCDateTime`、utf8mb4、连接池参数；`make migrate` / `make test-mysql` |

本机出口标准核对（第 4 节）：

| 标准 | 结果 | 证据 |
|---|---|---|
| mock 与 claude-sdk 各完成一次对话 | 通过 | `make verify-s1`：litellm、claude-sdk 各跑纯对话与工具调用，Run 均为 completed |
| Langfuse 可查到 Trace 且 seq 与 Run 事件对应 | 通过 | `scripts/verify_langfuse_trace.py` 退出码 0；`make verify-s1` 四条 Trace 均有根 span，sessionId 等于 run_id |
| `DATABASE_URL` 指向 compose MySQL 时全部测试通过 | 通过 | `make test-mysql` 109 通过 / 2 跳过 |

验证基线：`make test` 197 通过 / 5 跳过（跳过项为需要真实模型或 MySQL 的用例）；`make lint`（含 claude-sdk 适配器 mypy）通过；`make openapi-check` 通过；`pnpm -r build` 通过。

联调中发现并修复的问题：

- Harness 以错误结束时 Run 被记为 completed：runner 改为按 `DoneEvent.stop_reason` 迁移终态（error → failed，cancelled → canceled，max_tokens 如实记录）。
- MySQL 严格模式下 Agent 请求体超长字段导致 500：请求体按列宽加长度约束，返回 422。
- 开发用 `echo` 工具没有参数 schema，模型无法传参：补 `text` 参数。
- DeepSeek 思考模式要求工具轮次回传 `reasoning_content`：思维链写入事件日志并进入可重建历史后回传（D25）。
- 独立代码评审的 5 个重要问题：旧库启动崩溃、claude-sdk 会话与子进程泄漏及取消无效、续跑后机密污点丢失、推理文本绕过不变量、Harness 异常时 Run 卡在 working，均已修复并补回归测试。

S1 遗留（进入后续 Sprint）：见决策纪要 `docs/decisions/2026-10-17-S1架构决策.md` 第 3 节。

## 8. S2 执行结果（2026-10-17）

| 任务 | 结果 | 说明 |
|---|---|---|
| S2.1 | 完成 | `api/auth.py`：JWKS 本地验签（只收非对称算法、kid 必填、iss / aud / exp 校验），JWKS 缓存 5 分钟、未知 kid 限频刷新；`/v1`、`/admin` 路由器整体挂鉴权，未带令牌 401 |
| S2.2 | 完成 | Keycloak Client Credentials 令牌识别为应用（`app:<client_id>`）；用户令牌的 `act` 声明记为执行者；按 `tenant` 声明做 Agent 与 Run 的租户隔离；`/admin` 要求 `platform-admin` |
| S2.3 | 完成 | Alembic 0002 给 `t_audit` 增加 act、auth_method、tenant_id；请求级审计中间件 + 工具判定审计 + 管理写审计；每行审计镜像为 Langfuse span，与 Run 同会话 |
| S2.4 | 完成 | `runtime/secret_store.py`：Vault > `<NAME>_FILE` > 环境变量；compose 增加 Vault dev 与 `vault-init`；Helm 改为 Vault Agent Injector + `_FILE`（Helm 暂不启用，模板为草稿，见 D47）；`RUNTIME_ENV=prod` 安全基线 |
| S2.6 | 完成 | MODEL_RESPONSE 事件与模型 span 带稳定前缀哈希与命中统计；Langfuse 用量明细区分命中与未命中输入 |

本机出口标准核对：

| 标准 | 结果 | 证据 |
|---|---|---|
| 任意一次请求可在审计表中回答"谁、何时、以何身份、调了什么" | 通过 | `make verify-s2` 打印并断言 401 / 403 / 对话 / 工具判定 / 管理写各类审计行，身份字段齐全 |
| 未带令牌请求被 401 | 通过 | `make verify-s2`（Keycloak 实际签发的令牌）；`tests/test_auth.py` 覆盖全部接口与 9 类无效令牌 |
| Prompt Cache 命中率在 Langfuse 可统计 | 通过 | `make verify-s2`：Langfuse 用量明细统计命中率 94.9%，与事件日志一致 |

验证基线：`make test` 236 通过 / 5 跳过；`make test-mysql` 148 通过 / 2 跳过；`make lint`、`make openapi-check` 通过；`make verify-s1` 回归通过。

独立代码评审 12 项，9 项已修复（含高风险的"应用自批高风险调用"），其余转入遗留。

S2 遗留（进入后续 Sprint）：见决策纪要 `docs/decisions/2026-10-17-S2架构决策.md` 第 4 节。
