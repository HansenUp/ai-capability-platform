# Helm Chart（暂不启用）

2026-10-17 确认：当前只维护 `deploy/compose-dev/` 一种部署方式，本目录与 `deploy/argocd/` 保留为草稿（决策 D47，见
`docs/decisions/2026-10-17-S2架构决策.md`）。

- 草稿不做 `helm lint` / `helm template` 校验，也不随功能更新，内容可能落后于代码（例如新增的配置项、迁移方式）。
- 启用前需要：补 CI 渲染校验；对照 `services/runtime/src/runtime/config.py` 与 `.env.example` 核对环境变量；
  核对 Vault Agent 注入模板（S2.4）与 `RUNTIME_ENV=prod` 安全基线。
- 生产环境判定（S8 评审 #9）：平台统一用 `PLATFORM_ENV=prod`；策略服务同时认 `POLICY_ENV=prod`（任一为 prod 即生产：禁止
  baseline 引擎、`POLICY_SERVICE_TOKEN` 必配）。能力网关（`GATEWAY_ENV=prod`）与 Runtime（`RUNTIME_ENV=prod`）同样要求
  `POLICY_SERVICE_TOKEN`。本草稿的 values 只写了说明与示例键，不做 helm 校验。
