# S10.4 本机 APISIX 插件

`plugins/ai-node-routing.lua` 使用 APISIX 3.11.0 的实际接口；本目录不包含生产部署清单，不修改延期的 Helm／Argo CD。

从仓库根运行 `make verify-s10-routing`。前提：Docker Desktop／macOS、项目虚拟环境，以及预拉取的 `apache/apisix:3.11.0-debian`、`redis:7-alpine`。脚本生成临时 standalone YAML、RSA 服务与用户令牌，挂载插件到独立容器，随机回环端口，结束清理；不依赖现有业务节点或生产凭据。脚本固定 argv、无 shell、不接受外部命令参数；根 Ruff 仅对此验证脚本单独豁免 S603，与节点验证脚本一致。

配置必填：tenant_id、state_pool、routing_url、routing_token、issuer、audience、jwks_uri、nodes、fallback_nodes。nodes 为受信任 ID → `{host: IPv4, port, role: api|all, state_pool}`，所有节点必须同池；fallback_nodes 可为空（冷启动无缓存即 503）。scheme 仅 http，refresh_seconds 默认 10，stale_warning_seconds 默认 60。不得把不共享状态的节点填成同池。

通过 `extra_lua_path` 加载 `/opt/ai/?.lua`，将文件挂载为 `/opt/ai/apisix/plugins/ai-node-routing.lua`，在 Runtime `/v1/*` 路由显式启用 ai-node-routing；具体可执行 YAML 生成见验证脚本，不提供可被误启用的生产模板。插件仅允许既有 Runtime 公开 API，保留 Bearer 给开启 JWT 的 Runtime 复验。

Console 新增 `/internal/routing` 验证专用 JWT；不要用原管理页面 `/routing` 的开发身份头，也不要用 ai-node-svc 令牌。配置和日志不得泄漏 routing_token。独立 YAML 中的 encrypt_fields 不自动提供磁盘加密；本机临时文件不能充当生产 Secret 管理。

完整安全及共享状态前提见 `docs/decisions/2026-10-18-S10路由契约.md`。真实回归包含原节点退出后的挂起模拟写审批恢复／SSE、缓存与白名单故障；模型／策略／业务工具为本机桩。生产 TLS／mTLS、服务客户端与轮换、网络边界、多 worker／容量和独立安全签署尚未验收。
