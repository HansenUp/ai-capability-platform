# S12.1 可信反馈 SDK 与 Console 接入验收

日期：2026-10-22。范围：用户批准本轮 SDK／Console 显式登记提交、只读恢复、本人确认收口及真实本机浏览器／全仓回归。使用临时库、签名测试用户、临时服务凭据和 Langfuse 替身；未启用自动跟进、生产部署、真实反馈写或账号变更，未新增迁移。

## 已实现

- Runtime 新增原发起人／单租户只读 `GET /v1/workbench/feedback-attempts/runs/{run_id}/head`，提供权威修订用于 expected_revision；不凭页面缓存猜测版本。反馈私有回执投影补充 score，不返回评论、correction、令牌或原始 outbox 正文。
- SDK RuntimeRunClient 识别 feedback 及 PREPARED／PENDING_OBSERVATION／OBSERVED，严格检查 scope、attempt、Run、完成事件、指纹＝payload_hash、评分修订／分值、状态版本和收口状态。主体账本先验签身份、再读活动锁，再 GET 补齐该 feedback 明细；错范围、缺证据、降版或畸形数据失败关闭。
- SDK 新增反馈修订读取、登记、回执恢复及 execute／reconcile／acknowledge；每个方法单次显式请求，无自动重试／重投。既有 settleAttempt 分派 feedback 到独立协议，不误走数据库内 COMMITTED 的旧动作端点；旧 Web Component 的 legacy feedback API 保持旧契约，不宣称所有历史 SDK 调用已升级信任。
- Console Chat 在已完成 Run、验签原发起人范围内提供读取当前修订、-1／0／+1 选择、登记（尚未发送）、显式发送、来源只读对账及本人 ACK 预览／取消／确认。0 明示中性而非撤回，不提供自由文本。活动 feedback 冻结同主体其他新写，仍允许 Run／账本只读与重新认证。
- 登记丢响应仅恢复原 attempt；发送结果未知不提供再次执行按钮。来源对账仅发下游 GET，保存本地观测证明，不再 POST score；OBSERVED 不自动解锁。换令牌不改变服务端主体锁，换主体清空旧页面与确认；身份代次／abort 校验阻断迟到明细跨身份展示。
- ACK 响应丢失时，空活动列表不能直接解除已知锁：另行 GET 原 attempt，严格匹配已知绑定、读取 acknowledged=true 的 OBSERVED 回执，仍须原操作者点击“确认已收口反馈（仅只读恢复）”；之后重读主体账本，不再发 ACK 或评分写。查询缺失或错误保持锁定。

## 验证方式与覆盖

证据目录：`docs/evidence/s12/2026-10-22-feedback-ui/`。真实浏览器访问实际回环 Console 页面与 Runtime HTTP；用户 JWT 真实验签，数据库为 fixture 临时库。Observe 使用实际应用的 ASGI 认证通道，Langfuse 由注入的 HTTP transport 替身处理评分／Trace；不是实际 Langfuse 服务验收，也不是把 TestClient 冒充浏览器。

- SDK：四项新 helper 覆盖分步请求、所有恢复 GET、不完整／错主体／错 Run／错修订／错状态拒收、abort 后不继续读取、未知不重投。
- 后端：只读修订仅原发起人可读，同租户非发起人 403、跨租户 404；读取不清活动锁、不发下游请求。
- DOM：未决不允许 ACK／发送；伪造来源摘要拒收；OBSERVED 仍锁定；忽略 abort 的旧明细不跨身份显示。
- 实际浏览器：登记成功但响应丢失→重载并以同主体新 JWT 恢复；两个标签同时 execute，一方 200、一方 409，Langfuse 替身按固定 score ID 断言仅一次 POST；两方均模拟响应丢失后只能只读恢复 PENDING；其他用户／租户不能读回执或代 ACK；对账响应丢失后恢复 OBSERVED 仍不解锁；取消收口不写，ACK 响应丢失后需精确已收口证据与本人只读确认。浏览器存储均为空，无 trace／video／令牌录制，fixture 清理临时进程及 token 文件。

不增加超时、业务重试或放宽断言，不过滤新接口。首轮 69 项浏览器通过后，增加 ACK 丢响应的严格恢复场景继续复验；所有中间与最终证据分别保留。

## 最终回归结果

| 检查 | 实际结果 |
|---|---|
| 新增 SDK helper | 4/4 |
| 后端反馈与只读修订 | 21 passed |
| 全 Runtime OpenAPI 属性＋反馈组 | 55 passed，包含新增 head GET |
| 首轮／补充 ACK 丢响应后的浏览器 | 69/69、69/69（60 DOM＋9 E2E） |
| 中间全仓 | 1476 passed／38 skipped／2 warnings；尚未收集更新文档的 head 属性用例 |
| 最终完整 OpenAPI 下全仓 | **1477 passed／38 skipped／2 warnings，342.81 秒** |
| Console API／全部 helper | 181 passed；71/71 |
| SDK／Console／Portal 类型及构建 | 通过 |
| Ruff／mypy | 检查与格式 379 文件；类型 109 文件通过 |
| OpenAPI／差异格式 | 一致性通过；git diff --check 通过 |

跳过项及两项既有警告未新增、未压制，不计外部环境通过。最终源文件版本与日志散列归档，临时浏览器／Runtime／Console 已停止、测试令牌文件删除，自动跟进核实为 PAUSED。所有既有未提交变更保留，未建分支、提交、推送或合并。

## 未执行与下一步

本轮不把 Console 原 strict_readonly_unverified 指标改为可信统计，不回填历史评分；可信聚合版本仍需把来源回执、最新已接受修订和混合历史未证明数据的完整性纳入单独只读契约。不启用后台交付／重投、TTL 解锁、管理员代收口或留存清理。

真实 Langfuse v3 固定版本的 score ID／时间／读回序列化兼容验证、真实身份／服务凭据与生产边界仍待独立授权；本机替身通过不等于真实可信满意度已可用。北极星保持待定义，S12.1 不标整体完成。无必须人工确认却先执行的事项。
