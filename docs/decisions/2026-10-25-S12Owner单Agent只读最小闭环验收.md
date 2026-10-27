# S12 Owner单Agent只读最小闭环验收

## 结论与授权

用户已明确确认《Owner单Agent摘要与Trace契约草案》的D1～D6推荐方案，授权仅临时库、签名测试身份和Langfuse替身的本机最小实现。本轮完成独立Owner只读API、页面、身份／历史／撤权及在途语义验证，**不含Owner反馈权限，不代表真实身份／来源或生产验收**。

未操作已有服务或原库、真实账号、评分或Trace；没有迁移、新增持久身份服务、下载／安装／升级依赖或部署。只启动自有临时回环Console／Runtime／Vite和测试浏览器，使用临时SQLite及无外部效果替身；未启用自动跟进，也未提交、建分支、推送或合并。北极星仍待定义，D47／Helm／Argo CD／S10.7及外部验收状态不变。

## 1. 已实现闭环

| 契约 | 实现及验证 |
| --- | --- |
| 独立入口 | `GET /owner/agents/{agent_id}/summary`、`GET /owner/agents/{agent_id}/trace-metrics`；OpenAPI标注独立Bearer scheme，只有GET，无反馈或修改接口 |
| 默认拒绝 | `CONSOLE_OWNER_LOCAL_READ`默认关闭；只接受CONSOLE_ENV=dev，且须测试代码注入LocalProfile；未配置profile或生产环境受控503 |
| 可信测试身份 | 固定RSA公钥／kid、RS256、唯一Console测试audience、issuer、有效期、nbf／iat、测试客户端及用户类型校验；不网络获取JWKS、不接受服务／代理身份或X-Console-*覆盖 |
| 显式映射 | 固定`(issuer,tenant,subject)→legacy user`一对一测试绑定，显式realm角色映射到既有Role；缺失拒绝、重复／歧义配置不可用，不读取展示目录或猜测human前缀 |
| Agent准入 | ADMIN及以上仍必须有同租户UserAgent显式分配；租户／平台管理员也不能借新入口跳过分配。created_by与Run发起人不替代Owner |
| 摘要最小化 | 仅单Agent的draft／published／retired版本数及范围／读取信息，不透传配置、创建者、分配人数、节点、审计或租户总量 |
| Trace最小化 | 复用已有collect计算器，显式顶层／嵌套输出白名单；Token／来源USD、完整覆盖数和已知小计；空样本／缺失保留null，不返回原始Trace或个人信息 |
| 历史与边界 | 当前分配者可以读取指定窗口内接手前历史聚合；新scope使用带时区、最多7天的半开窗口，拒绝未来until；原管理员Trace仍含尾边界，不静默修改 |
| 撤权／在途 | T1准入和T2输出前均验签、绑定／角色核对及新Session分配查询。T2观察到撤销／失效／授权存储失败，丢弃数据；T2后撤销允许这次已准入JSON完成，下次读取重新拒绝 |
| ABA | T1与T2之间撤销后重授，按T2当前明确分配允许；没有授权epoch，不宣称发现过中途撤销或提供跨服务原子撤权 |
| 页面 | `/manage/owner-operations`独立页面；测试Bearer仅内存，切令牌／Agent／窗口清空、旧响应代次隔离、卸载不接纳迟到数据；401／403／404清空整个Owner视图，503清空对应结果 |
| 响应与源边界 | no-store／Vary Authorization；未知费用不补零，USD保留字符串，科学计数格式不转浮点。新Trace读取强制MockTransport和合成Basic身份，不能改用真实Langfuse服务或用户Bearer |

新Trace scope为`owner_agent_trace_metrics_v1`，摘要为`owner_agent_summary_v1`。响应明确`snapshot_guaranteed=false`、`source_trust=metadata_consistency_only`、`run_metrics_available=false`；完成API扫描不证明真实采集／留存完整，也不升级历史数据可信度。

## 2. 实现文件

- `services/console-api/src/console_api/owner_auth.py`：本机测试profile、验签与显式身份／角色映射、同租户分配核对。
- `services/console-api/src/console_api/owner_metrics.py`：两次准入、独立只读路由、最小结果和缓存／错误边界。
- `services/console-api/src/console_api/trace_metrics.py`：仅新增默认false的half_open计算选项；旧管理员HTTP接口和默认计算行为保持。
- `apps/console-web/src/views/OwnerOperations.vue`及router／App：独立Owner只读页面，不放宽旧Operations或反馈组件allowed条件。
- `services/console-api/tests/test_s12_owner_metrics.py`：42项隔离用例。
- `scripts/owner_browser_fixture.py`、既有浏览器runner／临时workbench夹具及`owner-operations.test.mjs`：签名测试身份、临时Console分配、源替身和4项新增实际浏览器验证。

LocalProfile是进程内测试注入，不提供持久绑定配置加载、真实JWKS地址、账号管理或正式IdP桥接；单开环境变量、粘贴已有真实管理员令牌或启动普通Console均不会使此profile自动可用。测试profile额外约定签名`identity_type=user`、指定azp、realm角色映射和单一audience，不能假定现有真实IdP已经满足这些字段／可信条件。

## 3. 覆盖及实际结果

证据目录：`docs/evidence/s12/2026-10-25-owner-implementation/`。

| 验证 | 最终实际结果 | 证据 |
| --- | --- | --- |
| Owner专项 | **42 passed**，1个既有警告，2.89秒；Python审计禁止网络／DNS／子进程，尝试数**0** | `offline_owner.py`、`owner-final.log` |
| Console完整目录 | **241 passed**，1个既有警告，8.85秒；默认临时SQLite，无外部库测试配置 | `console-final.log` |
| 完整隔离浏览器 | **83/83**，88837.86ms；4项新增Owner，其余为原约定组件／E2E回归 | `browser-final.log` |
| Console类型及构建 | vue-tsc noEmit、Vite构建通过 | `console-types-final.log`、`console-build-final.log` |
| 仓库质量门禁 | Ruff检查／格式396文件、仓库配置的mypy109源文件、现有Runtime／Gateway OpenAPI一致性通过 | `quality-final.log` |
| Owner OpenAPI专项 | 两个路径仅GET，均标注OwnerLocalUserToken HTTP Bearer；检查阶段禁止服务／数据库连接 | `openapi-owner-check.json` |

没有运行全仓Python，不把Console241加到旧全仓1510，也不改报34项默认skip。未改SDK／Portal，本轮不声称重跑它们的类型构建；mypy109是仓库既有目标，不暗示所有Console源码都在其范围内。既有Starlette弃用警告保留，未压制。

### 权限与一致性关键用例

- 签名错误、issuer／audience错配、混合audience、缺失／非法声明、未知用户、服务／代理身份、头和query覆盖：拒绝且不访问源。
- 创建者相同但未分配、已撤销、管理员未分配、跨租户同Agent／同legacy用户名：不能继承其他范围；正确绑定及该租户自己的分配后只返回对应版本。
- 源GET完成期间同步执行临时库撤销、绑定变更、令牌到期、T2授权存储失败：无部分指标返回。
- 确定性屏障在最终分配核对后撤销：当次返回200；下一次404且源请求数不增加。撤销再重授按确认的ABA语义200，不靠sleep、重试或改阈值猜测竞争。
- 接手前历史、until边界、101条分页、重复ID、2000上限、未知Token／费用、来源错配、输出字段额外扩展均有拒收或最小输出验证。
- 原管理员与反馈入口对显式Console ADMIN继续403；单独验证原管理员Trace仍计until边界而Owner不计。Owner没有反馈路径。
- 浏览器经真实回环Console验签／分配后展示摘要及替身历史Trace；Bob无分配404并清空；旧身份忽略abort、503、不合法用量与卸载迟到响应不恢复旧数据；localStorage／sessionStorage均为空。

浏览器撤权时序的精确裁决主要由真实ASGI请求＋临时SQLite的后端确定性用例承担；不把UI旧响应测试称作网络最终发送原子性证明。完整旧套件包含原有反馈替身回归，不代表新增Owner反馈能力，也没有写真实评分或Trace。

## 4. 中间失败与处理

1. 首次`pnpm`自身单文件可执行包在启动时触发Node SEA magic断言，尚未运行Vue编译。保留`console-build.log`；未安装／升级，改用已有node直接调用仓库vue-tsc／Vite，最终类型和构建通过。
2. 首次浏览器夹具被沙箱禁止绑定临时127.0.0.1端口。保留`browser-sandbox-failure.log`；经沙箱审批仅放行自有临时回环服务和测试浏览器，没有启动既有服务或放宽业务门禁。
3. 前述端口失败发生在旧夹具try／finally之前，曾留下一份本轮新建的0600测试令牌文件；已精确识别并删除。调整端口可用性检查到生成令牌文件前，不增加重试或超时。
4. 浏览器先81/81、补足未知／USD／卸载测试后83/83，最后对白名单／Bearer文档版本冻结复验83/83；中间成功日志也保留，不用收集前结果代替最终版本。

本轮未发生业务回归失败；格式检查及工具环境失败不冒充产品红测。没有删除失败证据或降低断言／测试阈值。最终临时Console／Runtime／浏览器停止，测试令牌文件删除，runner输出清理确认。

## 5. 安全界限、待办与未执行事项

- **已确认的本机范围完成，正式Owner接入仍关闭。** 真实issuer／Console audience、角色声明、受控绑定的管理／持久化、IdP撤权和来源采集／租户项目边界仍须另行确认与验收；不能拿合成profile连接真实Langfuse或原库补验。
- 旧Console开发头认证模型不在本轮改造。原管理员API的显式ADMIN拒绝回归，不证明整个开发服务不可伪造角色，不能把新OwnerBearer当作旧接口已完成正式身份隔离。新入口独立拒绝开发头覆盖，不为“修复全站认证”擅自拓展授权。
- T2之后在途响应可完成、已交付数据不能回收；JWT角色撤销不是即时同步；没有授权epoch／强撤权保证，不缓存授权也不能消除此边界。
- 不新增任职区间模型、管理员代收口、后台重试／发送、持续会话、共享缓存、反馈、个人明细、原始Trace跳转或北极星。若要这些能力先明确新授权／业务规则。
- 真实备份／原库迁移、真实节点操作、ES／MinIO原集成、32项原测试门禁及外部／生产验收继续待办；本轮没有执行任何这些动作。

本轮没有新增必须确认却提前执行的事项。保留全部既有未提交变更；S12.1只新增Owner本机测试范围通过，S12整体不标完成。
