# ADR-0005 能力网关数据面 POC：agentgateway、ContextForge 与自研

- 状态：已采纳（2026-10-18 S6 POC 结论回填：数据面采用 agentgateway v1.6.0，见文末"结论"）
- 日期：2026-10-16（范围评审）；2026-10-18（结论回填）
- 关联需求：CG-23、CG-04、CG-05、CG-16
- 依据：实施计划 P0-3.2、S6.6、S8；ADR-0007、ADR-0009、ADR-0011

## 背景

能力网关由两部分组成：
- 控制面：能力目录与两级发现、授权代理、Token Exchange、MCP 连接器登记、Skill 商店。这些绑定企业自身的身份、授权与目录，属于治理与供给层，按 ADR-0007 保持自研。
- 数据面：Runtime 工具代理到各 MCP Server 的请求转发，包括 MCP 协议的连接管理、会话、流式透传，以及转发前后的挂钩点。

数据面是通用的反向代理加协议适配，开源社区已有同类项目（agentgateway、ContextForge）。按 ADR-0007 的判断准则，若明年被开源项目免费提供，自建会沉没，应当跟随。但这些项目是否满足本平台的硬约束（身份头与 OBO 令牌透传、转发前的策略挂钩、可观测性）尚未验证，因此先做 POC，再决定采用或自研。本 ADR 约定 POC 的范围与验收口径，S6 期间执行，评审通过后不得在 POC 中途改变指标。

## 决策

做一次有时限、有量化指标的 POC，在 agentgateway、ContextForge 与自研最小数据面三者之间做选择。

### 1. 候选对象

| 候选 | 说明 | 记录要求 |
|---|---|---|
| agentgateway | 开源的面向 Agent 与 MCP 的代理网关 | 记录 POC 所用版本号与许可证 |
| ContextForge | 开源的 MCP 网关与注册中心 | 记录 POC 所用版本号与许可证 |
| 自研最小数据面 | 在现有 `services/capability-gateway` 上增加 MCP 反向代理（streamable-http 透传），仅实现本平台需要的最小功能 | 记录代码量与维护点 |

对照基线：Runtime 直连 MCP Server（无网关），用于计算网关引入的额外开销。

### 2. 范围

范围内：MCP 请求转发与流式透传、身份头与 OBO 令牌透传、转发前策略挂钩、Trace 透传与审计字段输出、部署与运维。

范围外：能力目录与发现、Token Exchange 的签发（S8.4）、Cedar 判定服务本身（ADR-0001）、Skill 商店、写操作规范校验（幂等键、dry-run、补偿声明，S8.5）。这些保持自研，不随数据面选型改变。

### 3. 评测维度与通过阈值

POC 测试条件：同一台机器或同一规格节点，同一份 MCP Server（使用 `mcp-servers/template` 或 `mcp-servers/ticket` 的 mock 模式），请求体约 1 KB、响应体约 10 KB，并发 50，预热 1 分钟后持续压测 5 分钟，基线与候选各跑三轮取中位。报告中必须写明硬件规格与任务画像（对应 P0-5.11 的测量口径要求）。

| # | 维度 | 通过阈值 | 一票否决 |
|---|---|---|---|
| 1 | MCP 透传延迟 P95 | 相对直连基线的额外 P95 ≤ 20 ms；错误率 < 0.1%；流式响应首字节额外开销 P95 ≤ 20 ms | 否 |
| 2 | 身份头与 OBO 透传 | 6 项用例全部通过：OBO 令牌原样透传不被网关自身凭据替换；客户端自带的身份头被剥离或覆盖；`act` 声明保留；过期令牌被下游拒绝；受众不符被下游拒绝；网关不落明文令牌日志 | 是 |
| 3 | 策略挂钩点 | 可在转发前同步调用 `POST /authz/check`；`deny` 时请求不到达上游；策略服务不可达时失败关闭；实现方式不需要 fork 上游源码 | 是 |
| 4 | 可观测性 | W3C `traceparent` 透传率 100%；每次转发产生 Span 并带 `run_id`、`call_id`、工具名、判定结果；经 OTel Collector 在 Langfuse 可查到 | 否 |
| 5 | 运维复杂度 | 按 1～5 分评分，平均分 ≥ 3：部署组件数、配置是否声明式并可 GitOps、升级与回滚成本、许可证与社区活跃度、团队（Python 栈）的维护成本 | 许可证不允许企业内部使用，或必须长期维护私有 fork |

维度 2、3 任一不通过，该候选直接淘汰。维度 1、4 的阈值未达到时，允许附带可行的整改方案并在时限内复测一次。

### 4. 选择规则

1. 满足全部硬性指标（维度 2、3 通过，维度 1、4 达标，维度 5 达标）的开源候选中，选择自研代码量最少、运维评分最高者（按 ADR-0007，能跟随则不自建）。
2. 两个开源候选都未通过时，采用自研最小数据面，并把 POC 中测得的指标作为自研版本的验收线。
3. 开源与自研同时通过时，取运维评分更高者；评分相同取自研代码量更少者。

### 5. 时限与责任

- POC 时限：S6（第 13～14 周）内完成，对应任务 S6.6；POC 结论在 S6 出口前提交评审。
- 决策责任人：平台架构师。执行：网关组；Runtime 组配合提供压测入口。
- 若 S6 结束仍无结论：沿用现有 `capability-gateway` 的转发实现，不阻塞 S7、S8，并由架构师在 S7 Kickoff 前给出延期理由与新时点。

## 备选方案

不做 POC，直接自研数据面：初期可控，但等于自己长期维护一个通用代理，与 ADR-0007 的判断准则冲突，不采纳。

不做 POC，直接采用某一开源项目：身份透传与策略挂钩是平台硬约束，未验证就绑定，一旦不满足需要返工 S8，不采纳。

## 后果

- S6 需要预留约 1 个 Sprint 的网关组工时用于 POC；压测脚本与原始数据存档，作为结论的证据。
- S8 的能力网关 V1 以本 ADR 的结论为前提，结论回填前，S8.4（Token Exchange）、S8.5（MCP 平台）可以按控制面设计推进，数据面相关任务等待结论。
- 无论选型结果如何，Runtime 工具代理只依赖能力网关对外的分发接口，不感知数据面的具体实现，保证选型可替换。

## 结论（S6 POC，2026-10-18 回填）

**选定方案：agentgateway v1.6.0 作为能力网关数据面；平台在 capability-gateway 中只保留转发前判定端点 `POST /authz/mcp-check`（ext_authz 回调）。**
依据选择规则 1：三个候选中只有 agentgateway 同时满足维度 2、3（一票否决项）与维度 1、4、5 的阈值；按 ADR-0007"能跟随则不自建"，
在开源候选达标时不采用自研。即便按规则 3 把整改后的自研（4 CPU）视为同时达标，二者运维评分相同（4.0），
agentgateway 需要的平台自有代码（判定端点约 40 行）也少于自研数据面（`mcp_proxy.py` 约 270 行，且要自行跟进 MCP 协议演进）。

| 项目 | 内容 |
|---|---|
| 评审日期 | 2026-10-18（POC 数据）；待平台架构师评审签字 |
| 选定方案 | agentgateway v1.6.0（`ghcr.io/agentgateway/agentgateway:v1.6.0`，Apache-2.0，LF Projects） |
| 候选版本号 | agentgateway v1.6.0（Apache-2.0）；IBM ContextForge v1.0.11（`ghcr.io/ibm/mcp-context-forge:v1.0.11`，Apache-2.0）；自研 = `services/capability-gateway/src/capability_gateway/mcp_proxy.py`（约 270 行非空非注释 Python，维护点：MCP streamable-http 透传、OBO 换取与缓存、JSON-RPC 解析与判定、Span） |
| 维度 1 实测（额外 P95、错误率） | 50 并发闭环，预热 60 s + 300 s × 3 轮取中位（基线上游为单进程 Python，CPU 打满）。基线 P50/P95/P99 = 39.2/97.1/134.6 ms、954 RPS。agentgateway：额外 P95 −0.4 ms、流式首字节额外 P95 −3.8 ms、错误率 0、973 RPS（网关 0.63 CPU、50 MiB；判定端点另耗 0.82 CPU）——**达标**。自研（2 CPU）：额外 P95 +75.6 ms、首字节 +85.9 ms、594 RPS，网关 CPU 191%（打满）——**未达标**；整改复测（4 CPU/4 worker，1 轮）额外 P95 −1.3 ms、首字节 −10.2 ms、962 RPS——达标但单请求 CPU 约为 agentgateway+判定端点的 1.5 倍。ContextForge（1 worker）：额外 P95 +1539 ms、34 RPS、错误率 0.06%，网关 CPU 打满；不带 traceparent 的对照同样约 33 RPS——**未达标且无可行整改** |
| 维度 2 用例通过情况 | 三个候选 6/6 全部通过（Keycloak 真令牌）：OBO 原样到达上游（自研为网关代用户换得、sub=终端用户、azp=capability-gateway）；伪造的 X-AI-Act / X-AI-Dev-* 被剥离或覆盖；上游采信的执行者为平台声明的 Agent；过期令牌与错受众令牌被拒绝（agentgateway 在网关 jwtAuth 处拒绝，ContextForge 由上游拒绝，自研在换取处拒绝且上游拒绝错受众令牌）；网关日志中无令牌载荷段与签名段 |
| 维度 3 挂钩实现方式 | agentgateway：声明式 `extAuthz`（HTTP，`condition: has(mcp.tool)`）回调 `/authz/mcp-check`，非 2xx 即拒绝，`failureMode: deny`；ContextForge：原生插件 TOOL_PRE_INVOKE（`fail_on_plugin_error: true`）；自研：转发前同步调用策略服务。三者 deny（未登记工具、跨租户）均不到达上游，策略服务停掉后失败关闭；第三方候选在判定端点不可达时也失败关闭；均无需 fork |
| 维度 4 可观测性验证 | traceparent 透传率：三者 100/100。Langfuse 可查 Span 且带 run_id、call_id、工具名、判定结果：自研、agentgateway 通过（agentgateway 判定结果按响应码记入 Span 属性）；ContextForge 不通过（有工具名，无 run_id/call_id/判定，需额外插件或 baggage 映射）。流式进度通知逐条实时到达：自研、agentgateway 是；ContextForge 否（以 JSON 一次返回） |
| 维度 5 运维评分 | 自研 4.0、agentgateway 4.0、ContextForge 2.8（< 3，不达标）。子项与理由见 `evals/dataplane-poc/results/summary.md` |
| 未选方案的淘汰原因 | ContextForge：维度 1 远超阈值（每次工具调用在其 Python 调用链上约耗 30 ms CPU——含上游会话、RBAC、指标落库等，1 个 worker 打满时仅 34 RPS，50 并发下排队到 P50 约 1.46 s；本机内存所限只能跑 1 个 worker，按线性扩展到 2 worker 仍远超阈值）、维度 4 Span 缺平台字段、维度 5 平均 2.8（依赖数据库、Admin API 登记非声明式、需关闭默认的账号限流与锁号）。自研：功能全部达标，但在同等 2 CPU 下额外 P95 +75.6 ms 未达标，需约 2 倍 CPU 才能追平；且按 ADR-0007 有达标开源方案时不自建 |
| 对 S8 任务的影响 | S8.5（MCP 平台）以 agentgateway 为数据面：MCP Server 登记从 `MCP_SERVERS` 迁到 agentgateway 声明式配置（GitOps 下发），Runtime 工具代理经 agentgateway 访问 MCP Server；`/authz/mcp-check` 作为 ext_authz 端点保留并纳入 capability-gateway 的正式接口（S8 补鉴权：只接受数据面的服务身份）。S8.4（Token Exchange）在 Runtime/控制面完成 OBO 换取，agentgateway 只透传（也可评估其 `backendAuth.oauthTokenExchange`）。`mcp_proxy.py` 的 `/mcp/{server}` 降为开发态与验收对照，不作为生产路径，S8 结束前决定删除 |
| 决策人 | 平台架构师（签字待填） |

POC 局限与需要在 S8 复核的事项：
- 单机测量：M1 Max（10 核）上的 Docker Desktop（10 CPU / 7.6 GiB），各容器以 CPU 配额隔离；压测器在同一 Docker 网络的容器内运行（宿主机端口转发在 50 并发下自身成为瓶颈，已弃用）。第 1 轮各目标 P95 普遍偏高（与后台 Langfuse 积压消化重叠），按三轮中位数取值。
- 上游为 POC 自带的 MCPServer mock（`evals/dataplane-poc/upstream.py`），身份口径与 mcp-ticket 一致（JWKS 验签、azp 可信才采信 X-AI-Act），未直接使用 mcp-servers/ticket 的 mock 模式（后者不是 MCP 协议端点）。上游单进程在基线即打满，额外延迟是同一闭环负载下的端到端差值。
- agentgateway 的判定回调目前落在 Python 判定端点，约 0.82 CPU / 970 RPS，是该方案的主要成本；S8 可改 gRPC ext_authz 或启用 ext_authz 结果缓存（键含主体、工具、Agent）。
- agentgateway 已知坑：v1.6.0 下 `extAuthz` 的 `metadata` 与后端 CEL `transformations` 同时启用会把上游 SSE 响应整段缓冲（首字节退化），POC 配置已规避；非 tools/call 请求不经判定端点。访问日志为每请求一行（压测期间约 575 MB），生产需调日志级别或采样。
- S6 评审整改（判定口径，不影响性能结论）：判定端点 `/authz/mcp-check` 增必填 `server`（目标 MCP Server 登记名），工具必须属于该 Server 的业务系统（能力 `system` 与 `MCP_SERVERS[server].system` 一致），未登记的 Server 或跨 Server 同名工具一律 deny；agentgateway 以 CEL 请求期变量 `mcp.tool.target`（处理该 tools/call 的 target 名，target 按平台登记名命名）放入 ext_authz 请求体，ContextForge 插件按工具名的网关前缀经 `gateway_servers` 映射。判定端点加服务间共享密钥 `X-Gateway-Authz-Token`（`GATEWAY_AUTHZ_TOKEN`，`GATEWAY_ENV=prod` 缺失拒绝启动），S8 可再换为数据面的服务身份（mTLS / 客户端令牌）。自研代理另加 MCP 方法白名单（initialize、ping、tools/list、tools/call、notifications/* 与客户端应答），其余方法拒绝；agentgateway 的 ext_authz 仍只在 tools/call 时回调，S8 需在其配置里对 resources/* 与 prompts/* 等方法另行收口。
- 自研整改复测与 ContextForge 无 traceparent 对照各只跑 1 轮；ContextForge 受本机内存所限以 1 个 worker 运行。
- 复现命令、原始数据与汇总见 `evals/dataplane-poc/README.md`、`evals/dataplane-poc/results/`。
