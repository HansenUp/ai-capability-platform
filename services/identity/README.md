# 身份（Keycloak）

- Realm `ai-platform`：人员经企业 IdP 联邦；Agent / Service 为 Client（Client Credentials，短期令牌）。
- Token Exchange（RFC 8693）：能力网关 Client 开启 `token-exchange` 权限，audience 为各 MCP Server / 业务系统 Client；令牌含 `act` 声明标识执行 Agent。
- `realm-export.json` 为最小 realm 导出，`make compose-up` 时由 Keycloak 自动导入。
- 引擎与 APISIX 以 JWKS 本地验签：`JWKS_URL=https://<idp>/realms/ai-platform/protocol/openid-connect/certs`。
- Runtime 验签（S2）：受众 `ai-runtime`（`ai-runtime` 客户端作用域里的 audience mapper）、租户声明 `tenant`、realm 角色 `realm_access.roles`、
  应用标识 `client_id`；Runtime 配置 `JWKS_URL`、`JWT_ISSUER`、`JWT_AUDIENCE=ai-runtime`。
- 本机开发客户端（密钥 `<clientId>-dev-secret`，仅限本机 compose）：`agent-demo`（应用令牌，调 /v1）、`console-api`（服务账号带
  `platform-admin`，调 /admin）。realm 导入只在首次启动时进行，修改本文件后需 `docker compose up -d --force-recreate keycloak`。
- 本 realm 显式定义了 clientScopes，Keycloak 不再自动创建内置作用域（profile、roles、basic 等），令牌所需声明全部由
  `ai-runtime` 作用域与各客户端的 mapper 提供。
