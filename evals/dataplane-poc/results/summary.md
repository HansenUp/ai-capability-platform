# 数据面 POC 结果汇总（ADR-0005）

生成时间：2026-10-18T01:44:10+0800。原始数据：`results/<目标>-r<轮次>.json`、`results/cases.json`；本文件由 `poc.py report` 生成。

## 环境与任务画像

- 硬件：Apple M1 Max，10 核，内存 64 GiB；macOS-27.0.1-arm64-arm-64bit
- Docker：28.3.2 cpus=10 mem=8218034176；镜像：{"agentgateway": "ghcr.io/agentgateway/agentgateway:v1.6.0", "contextforge": "ghcr.io/ibm/mcp-context-forge:v1.0.11", "python_runtime": "ai-platform/dataplane-poc-py:local (FROM ghcr.io/ibm/mcp-context-forge:v1.0.11)"}
- 网络路径：压测器容器 →（候选网关 →）上游容器，同一 Docker 网络；基线直连上游。
- 资源：每个候选 2 CPU（补充对照 selfbuilt-4cpu 为 4 CPU）；上游 MCP Server 为单进程 Python（有状态会话、JWKS 验签）。
- 负载：闭环 50 并发（10 进程 × 5），tools/call 请求体约 1 KB、响应约 10 KB；80% 普通工具 + 20% 流式工具（3 条进度通知间隔 10 ms 后返回结果）；每请求新 traceparent（sampled）。预热与时长见下表 params。

## 维度 1：透传延迟与资源（各轮中位数，单位 ms）

| 目标 | 轮数 | P50 | P95 | P99 | 额外 P95 | 普通工具 P95 | 流式首字节 P95 | 额外首字节 P95 | 错误率 | RPS | 网关 CPU% | 判定端点 CPU% | 网关内存 MiB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 3 | 39.2 | 97.1 | 134.6 | 0.0 | 81.6 | 74.5 | 0.0 | 0.0000 | 954 | — | — | — |
| selfbuilt | 3 | 73.1 | 172.7 | 237.5 | 75.6 | 164.9 | 160.4 | 85.9 | 0.0000 | 594 | 191 | — | 148 |
| agentgateway | 3 | 41.7 | 96.7 | 126.3 | -0.4 | 75.3 | 70.7 | -3.8 | 0.0000 | 973 | 63 | 82 | 50 |
| contextforge | 3 | 1464.1 | 1636.4 | 1739.3 | 1539.3 | 1623.4 | 1676.1 | 1601.6 | 0.0006 | 34 | 101 | 4 | 478 |
| selfbuilt-4cpu | 1 | 43.1 | 95.8 | 120.5 | -1.3 | 70.3 | 64.3 | -10.2 | 0.0000 | 962 | 222 | — | 268 |
| contextforge-notrace | 1 | 1517.7 | 1687.0 | 1822.1 | 1589.9 | 1679.4 | 1703.0 | 1628.5 | 0.0005 | 33 | 102 | 4 | 480 |

各轮 P95（ms）：baseline [175.5, 97.1, 92.9]；selfbuilt [198.3, 158.2, 172.7]；agentgateway [180.4, 96.1, 96.7]；contextforge [2771.8, 1636.4, 1536.8]；selfbuilt-4cpu [95.8]；contextforge-notrace [1687.0]

压测参数：`{"concurrency": 50, "procs": 10, "warmup_s": 60.0, "duration_s": 300.0, "stream_ratio": 0.2, "tool": "ticket.search", "stream_tool": "ticket.get", "traceparent": true}`

## 维度 2～4：功能用例（cases.py）

| 用例 | selfbuilt | agentgateway | contextforge |
|---|---|---|---|
| A1 OBO 令牌透传、不被网关凭据替换 | 通过 | 通过 | 通过 |
| A2 客户端身份头剥离/覆盖 | 通过 | 通过 | 通过 |
| A3 act（执行者）保留 | 通过 | 通过 | 通过 |
| A4 过期令牌被拒绝 | 通过 | 通过 | 通过 |
| A5 受众不符被拒绝 | 通过 | 通过 | 通过 |
| A6 日志无明文令牌 | 通过 | 通过 | 通过 |
| P1 deny 不到达上游 | 通过 | 通过 | 通过 |
| P2 策略服务停掉 → 失败关闭 | 通过 | 通过 | 通过 |
| P3 判定端点不可达 → 失败关闭 | — | 通过 | 通过 |
| P4 无需 fork | 通过 | 通过 | 通过 |
| T1 traceparent 透传率 100% | 通过 | 通过 | 通过 |
| T2 Langfuse 可查 Span（run_id/call_id/工具/判定） | 通过 | 通过 | **不通过** |
| 流式进度通知逐条实时到达 | 是 | 是 | 否 |

## 维度 5：运维复杂度评分（1～5）

| 子项 | selfbuilt | agentgateway | contextforge |
|---|---|---|---|
| 部署组件数 | 5 | 4 | 2 |
| 声明式配置/GitOps | 3 | 5 | 2 |
| 升级与回滚成本 | 5 | 3 | 2 |
| 许可证与社区 | 3 | 5 | 5 |
| Python 栈维护成本 | 4 | 3 | 3 |
| **平均** | 4.0 | 4.0 | 2.8 |

- selfbuilt：不新增组件（随 capability-gateway 发布）；MCP Server 登记走网关 API/种子配置，尚未声明式；升级回滚即网关版本；自有代码无许可证风险但无社区；约 270 行 Python（mcp_proxy.py），MCP 协议演进需自己跟进。
- agentgateway：新增 1 个 Rust 二进制（~45 MiB 内存）+ 网关内判定端点；YAML 声明式且热加载、K8s 有 CRD；1.x 每月发版、配置项仍在演进（本 POC 即踩到 CEL transformations 与 extAuthz.metadata 组合导致 SSE 缓冲）；Apache-2.0、LF 项目、活跃；团队不写 Rust，只能配置不能改源码。
- contextforge：网关 + 数据库（生产需 Postgres）+ 多实例需 Redis；上游与虚拟服务器通过 Admin API 登记、存库，非声明式；每版带数据库迁移、默认值变化大（本 POC 需关闭账号限流/锁定、放开 SSRF 私网）；Apache-2.0、IBM 主导、活跃；Python 栈可写插件，但代码库庞大（工具调用路径上有会话池、RBAC、指标落库等），定位/调优成本高。
