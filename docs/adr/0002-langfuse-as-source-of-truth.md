# ADR-0002 Langfuse 作为 Trace 与评测数据的事实源

- 状态：已采纳
- 日期：2026-10-16
- 关联需求：OB-01、OB-04、OB-07、OB-08、TP-09、RT-13
- 依据：实施计划 S1.5、S3.5、S4.6、S10.5；ADR-0007（自建追踪存储排除在自研范围外）

## 背景

平台需要四类观测与评测数据：每次 Run 的 Trace（模型调用、工具调用、token 用量、时延）、用户反馈（点赞、点踩、修正）、评测数据集、评测运行结果。发布评测闸门、Prompt Cache 命中统计、成本核算、反馈沉淀都依赖这些数据。

如果自建 Trace 表与评测存储，需要自己实现查询界面、数据集管理、评分与对比、回放，这些恰是 ADR-0007 所述"会被开源项目免费提供"的一层，投入半衰期短，不属于治理与供给的自研范围。

同时需要分清两件事：Trace 是观测视图，Run 的状态与恢复依据仍是 Runtime 的追加式事件日志（`t_run_event`），二者通过 `run_id` 与事件 `seq` 关联，互不替代。

## 决策

1. Langfuse 是 Trace、反馈（score）、评测数据集、评测运行四类数据的唯一事实源。平台不自建 Trace 表，也不自建评测数据集表。
2. 接入方式以 OpenTelemetry 为标准：
   - Runtime 每个 Run 生成一条 Trace，采用 GenAI 语义约定，Span 属性包含 `run_id`、agent、模型别名、token 用量，经 OTLP 发往 OTel Collector，再由 Collector 的 `otlphttp/langfuse` 导出器转发到 Langfuse 的 OTel 端点（配置见 `deploy/compose-dev/otel-collector.yaml`）。
   - 模型网关（LiteLLM）通过 `success_callback: ["langfuse"]` 回调，补充模型侧明细。
   - 用户反馈经 `services/observe-adapter` 的 `/feedback` 写成 Langfuse score 并挂到对应 Trace。
   - 评测数据集（首个为 `ticket-summary`）建在 Langfuse，Console 的发布评测闸门读取评测运行结果，阈值可配置。
3. 保留替换路径：应用代码只依赖 OTLP 协议，以及封装在 `observe-adapter` 与 Console 评测闸门适配层内的少量 Langfuse API（score、dataset、evaluation run）。替换后端时只改 Collector 导出器并重写这两处适配层，Runtime 不受影响。
4. 数据分级约束沿用 ADR-0010：机密数据只记录元数据不记录内容，内部数据脱敏后记录。该规则在进入 Collector 之前由平台执行，不依赖 Langfuse 自身的脱敏能力。
5. 自托管最小部署策略：
   - 采用 Langfuse v3 自托管，组件为 web、worker、PostgreSQL、ClickHouse、Redis、对象存储（MinIO）。本机开发使用 `deploy/compose-dev` 的 `observe` profile，复用 compose 内的 Redis 与 MinIO，全部单副本。
   - 预发布与生产先按单副本 web 加单副本 worker 起步，PostgreSQL、ClickHouse 与对象存储优先使用企业已有或托管服务，不为 Langfuse 单独新建运维体系。
   - 触发扩容或改托管的条件为运维工时超出预算，决策点为 S15（见实施计划第 12 章风险表）。

## 备选方案

自建 Trace 表与评测存储：与 ADR-0007 冲突，且需要自行实现数据集管理、评分对比与回放，不采纳。

仅用通用链路追踪系统（如 Tempo 加 Grafana）：能承载 Trace，但没有反馈评分、数据集与评测运行的一体化模型，仍需自建评测部分，不采纳。

闭源 SaaS 类 LLM 观测平台：数据出境，与 ADR-0010 的数据分级规则冲突，且完整提示词与响应会离开企业边界，不采纳。

## 后果

- Trace 与评测数据的查询、对比、回放直接使用 Langfuse，平台不再维护对应表与页面；数据架构图需去掉自建 Trace 表（P0-5.10）。
- 评测闸门与反馈沉淀依赖 Langfuse 可用性：Langfuse 不可用时，Run 执行不受影响（Trace 导出为异步，`observe-adapter` 在未配置或不可达时本地缓冲），但发布评测闸门无法取得新的评测结果，发布按失败关闭处理。缓冲容量与丢失边界在 P0-5.11 的非功能口径中明确。
- 新增运维对象：Langfuse 自托管栈。其中 ClickHouse 是最重的一块，列为 S15 前的重点评估项。
- 开发期需要在本机为 Langfuse 创建项目并取得公钥与私钥（`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`），只放本机 `.env`，不入库。

## 2026-10-22 获批本机反馈协议补充

用户批准 Runtime 权威回执与最小 outbox 的本机最小闭环，迁移只在临时库验证。`t_feedback_outbox` 保存不可变待发送数值、固定 score ID／时间和身份／修订绑定及摘要、观测摘要；无评论、correction 或令牌。它是交付与幂等协议证据，不作为第二套反馈统计事实源；只有匹配 Langfuse score 和 Trace 的读回证据才能记 OBSERVED。

本轮不改变历史评分信任、不改现有 Console 只读统计、不提供新满意度事实库。新协议默认关闭，仅 dev 显式启用；认证仅以临时测试凭据和工具替身验证，未配置真实身份或密钥、未写真实 Langfuse。outbox 不自动发送／重投，无 TTL 解锁或留存清理；结果未知保持主体锁。真实部署、凭据管理、留存／清理和评分来源验收仍需另行批准，不将该局部授权视为原 ADR 的生产变更签署。
