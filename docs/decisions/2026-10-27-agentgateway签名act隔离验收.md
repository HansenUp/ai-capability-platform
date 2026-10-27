# agentgateway 签名 act 隔离验收（2026-10-27）

## 本轮交付与结果
新增网关实际 ES256 签发器→MCP 实际验签器的纯替身互操作测试 7 项。只生成内存测试密钥，JWKS 使用 MockTransport；无真实服务连接。未修改生产签发或验签实现。

- 互操作及原 `/invoke` 回归：22 passed，退出码 0。
- 原 MCP act 专项：26 passed，退出码 0。
- 新测试 Ruff 检查通过。首次 lint 的合成 token 字面量与空行问题已修正；未修改断言或超时。
- 两组各有 1 条既有 Starlette/AnyIO 弃用警告。
- 审计 runner 禁止 socket 连接、监听、DNS、子进程及临时根外 SQLite。两组 blocked_actions 均为空；模板测试共 4 次临时 SQLite 连接，临时目录由上下文退出清理，无后台资源。

命令（仓库根）：
```
.venv/bin/python docs/evidence/baseline/2026-10-27-signed-act/verify_chain.py bridge
.venv/bin/python docs/evidence/baseline/2026-10-27-signed-act/verify_chain.py template
```
证据：`docs/evidence/baseline/2026-10-27-signed-act/` 内日志、JUnit、lint 和源码 SHA256。未运行全仓；本轮是定向协议验证。

## 已验证边界
实际签名经测试 JWKS 可被 MCP 验签，Agent/客户端链及 Run/call 字段保持一致。相同用户换一枚 OBO 或换主体均 403，错误绑定不会消耗有效断言；正确使用后同一 jti 再次使用 401。错误 audience、过期断言 401；缺失 OBO 绑定 403；冷缓存 JWKS 故障 503。

互操作测试中的 OBO 是合成不透明字符串，调用验签函数时主体由测试提供；不宣称该测试证明 OBO JWT 本身的认证。连接器正式路径必须先独立校验 OBO 的签名、iss、aud、有效期及主体，再比较精确令牌哈希。签名证明签发者声明，不能单独证明 Agent/Run/租户的授权来源。签名也不是业务幂等回执。

## 正式接入推荐与待确认（未执行）
| 门禁 | 推荐方案及需确认内容 |
|---|---|
| 签发责任与身份委托 | 复用 Capability Gateway 签发能力，agentgateway 不保管私钥；确认可信服务身份、允许调用者及身份委托协议。不能直接信任客户端 x-ai-agent 或回调 body 的 subject/tenant。 |
| 令牌及上下文绑定 | 对实际转发的 OBO 精确绑定，固定连接器 audience；确认 Agent/version、Run/call、租户与工具授权的权威来源及绑定范围。现有签名不包含工具参数摘要，不能宣称参数防替换。 |
| 密钥和撤销 | 确认 issuer/JWKS 信任根、密钥管理责任、轮换重叠和撤销时限；现有 MCP JWKS 有缓存与受限陈旧回退，不是即时撤销。 |
| 重放与重传 | 单进程 jti 缓存不能证明多副本防重放；确认共享存储与失败关闭运维。传输结果未知不得凭重新签一个 jti 自动重发业务写。 |
| 入口与审批 | 边界剥离客户端提供的委托头，仅注入可信签名。明确授权回调 require_approval 与 Runtime 审批的联动，HTTP 200 不代表可以直接执行。 |
| 迁移和回滚 | 保留现有 `/invoke` 契约，旧 MCP 代理不可静默降级到裸头；盘点旧路径使用方、灰度范围与切换开关。回滚仍强制签名，不允许恢复不可信身份入口。 |

## 兼容验收要求
agentgateway POC 尚未等同正式签名链路；本轮没有运行真实 agentgateway 二进制。正式迁移需在上述门禁确认后，验证真实数据面转发同一 OBO、拒绝伪造头、工具级授权与审批、跨副本重放、故障关闭及灰度回滚。不要把 pure-double 的协议通过标记为生产或真实 agentgateway 验收。

下一步优先确认签发责任和上下文绑定契约，再扩展纯替身的“可信调用者→签发→转发→MCP”集成测试；真实密钥、共享存储、网络及部署授权另行处理。未连接业务、未访问原库、未部署、未启用自动跟进。


## 后续签发责任与上下文补验
见 [签发责任与上下文契约草案](2026-10-27-agentgateway签发责任与上下文契约草案.md)。新增 4 项调用入口测试，本轮 bridge 共 26 项通过；本节为后续证据，不替代前轮计数。C1–C6 尚待确认，未实施正式接入。
