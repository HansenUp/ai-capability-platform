# Helm Chart（暂不启用）

2026-10-17 确认：当前只维护 `deploy/compose-dev/` 一种部署方式，本目录与 `deploy/argocd/` 保留为草稿（决策 D47，见
`docs/decisions/2026-10-17-S2架构决策.md`）。

- 草稿不做 `helm lint` / `helm template` 校验，也不随功能更新，内容可能落后于代码（例如新增的配置项、迁移方式）。
- 启用前需要：补 CI 渲染校验；对照 `services/runtime/src/runtime/config.py` 与 `.env.example` 核对环境变量；
  核对 Vault Agent 注入模板（S2.4）与 `RUNTIME_ENV=prod` 安全基线。
