# 身份（Keycloak）

- Realm `ai-platform`：人员经企业 IdP 联邦；Agent / Service 为 Client（Client Credentials，短期令牌）。
- Token Exchange（RFC 8693）：能力网关 Client 开启 `token-exchange` 权限，audience 为各 MCP Server / 业务系统 Client；令牌含 `act` 声明标识执行 Agent。
- `realm-export.json` 为最小 realm 导出，`make compose-up` 时由 Keycloak 自动导入。
- 引擎与 APISIX 以 JWKS 本地验签：`JWKS_URL=https://<idp>/realms/ai-platform/protocol/openid-connect/certs`。
