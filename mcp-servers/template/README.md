# MCP Server 模板（ai-mcp-template）

业务系统连接器的模板，同时是连接器公共件（mcp-ticket 与 `mcp-servers/mocks` 复用）。

| 模块 | 作用 |
|---|---|
| `mcp_template.auth` | `identity_dependency(prefix, default_audience)`：OBO 令牌 JWKS 验签 + act 断言校验，产出 `Identity` |
| `mcp_template.act` | `X-AI-Act-Assertion` 校验（S8.4）：ES256、网关 JWKS、iss / aud / sub / obo_hash / exp≤60 s / jti 防重放 |
| `mcp_template.jwks` | JWKS 缓存（TTL + 未知 kid 限频刷新，对端不可达 503） |
| `mcp_template.spec` | 写规范（S8.5）：`WriteControl`、`read_tool` / `write_tool` manifest 条目、`validate_manifest` 自检 |
| `mcp_template.idempotency` | SQLite 幂等执行：（租户, 调用者, 键）隔离，业务写入与幂等记录同事务 |
| `mcp_template.testing` | 测试件：自签 RSA 的假 IdP、自签 ES256 的假网关、假 JWKS transport |

## 写规范（S8.5）

非只读工具三项缺一不可，网关在探活、`POST /capabilities`、`/invoke` 三处校验：

1. 幂等键：执行必须带 `idempotency_key`；同键同请求返回首次结果（`deduplicated: true`），同键不同请求 409。
2. dry-run：`dry_run: true` 只返回 `impact_preview` 与 `card`，不写数据、不需要幂等键。
3. 补偿工具：`compensate_tool` 指向同一 manifest 里的另一个写工具，补偿工具本身也满足本规范。

`input_schema` 只含业务参数，不暴露 `idempotency_key` / `dry_run`（由 Runtime 闸口生成、经网关注入）。
manifest 写工具条目除 `idempotency` / `dry_run` / `compensate_tool` 外，保留 `supports_dry_run` / `requires_idempotency_key` 兼容旧网关。

## 鉴权

环境变量见 `mcp_template.auth` 模块文档（前缀 `<P>`，模板为 `MCP_TEMPLATE`）。要点：

- 生产态（`<P>_DEV_AUTH` 未开）：必须 `Authorization: Bearer <OBO>` + `X-AI-Act-Assertion`；缺断言 401，
  断言无效 401，断言有效但 sub 或 obo_hash 与本请求 OBO 不符 403，网关 JWKS 不可达 503。通过后采信断言里的 act 链。
- 开发态：可用 `X-AI-Dev-Sub` / `X-AI-Dev-Tenant` / `X-AI-Dev-Roles`；Bearer 请求缺断言时，裸 `X-AI-Act` 仅在 OBO azp 可信时采信。
- `<P>_ACT_JWKS_URL` 指向能力网关 `/.well-known/jwks.json`（本机 `http://127.0.0.1:8082/.well-known/jwks.json`）。

## 运行

```bash
.venv/bin/uvicorn mcp_template.main:app --host 127.0.0.1 --port 8099
```
