# B2 后端与 SDK 并行验收

检查时间：2026-10-27。本报告只记录本轮在当前工作区实际执行的命令和结果。隔离测试通过不代表真实服务接入或生产验收通过。未提交、未推送。

仓库内及上级目录未找到 `AGENTS.md`。依据 `docs/plan/2026-10-27-整体交付重排计划.md` 的 B2 行、`docs/decisions/2026-10-27-平台级交付基线与验收矩阵.md`，以及 Runtime / Gateway / Policy / SDK 源码与测试入口。

## 1. 检查时的 HEAD 和工作区

HEAD 始终为 `f35e2f82178183161c5ead076dafb7af77f44ef7`。该提交的远端 CI run `40471891167` 只覆盖该提交，不覆盖工作区里尚未提交的修改。

开始时无暂存、无未跟踪文件。已有未暂存修改，本轮未改这些文件：

- `apps/console-web/src/platform.css`
- `apps/console-web/tests/browser/a-demo-navigation.test.mjs`
- `apps/console-web/tests/browser/component.test.mjs`
- `apps/console-web/tests/browser/workbench.html`
- `docs/decisions/2026-10-27-B1窄屏键盘与演示清理验收.md`
- `docs/plan/2026-10-27-整体交付重排计划.md`
- `scripts/local_demo.html`

结束时上述文件仍在。期间 `2026-10-27-B1窄屏键盘与演示清理验收.md` 的 diff 统计由开始时的 22 行新增变为 44 行新增，并新出现未暂存的 `docs/decisions/2026-10-27-平台级交付基线与验收矩阵.md`。这两处不是本轮写入的，本轮没有编辑它们。

本轮只改了三个生成类型文件，见第 4 节。

## 2. 已核验能力

| 能力 | 源码 | 测试或检查 |
|---|---|---|
| Runtime Run、事件、审批/输入、工作台写锁与回执 | `services/runtime/src/runtime/`，路由前缀含 `/v1/runs`、`/v1/workbench`、`/v1/workbench/feedback-attempts` | `services/runtime/tests/`，临时 SQLite，`HARNESS=mock`，策略请求走 `runtime_policy_stub` |
| Gateway 目录、授权、签名 act、MCP、Skill | `services/capability-gateway/src/capability_gateway/` | `services/capability-gateway/tests/`，内存目录和 `tmp_path` 包存储 |
| Policy baseline 与 Cedar HTTP 替身 | `services/policy/src/policy_service/` | `services/policy/tests/`；未启动本机 `cedar-engine` 进程 |
| Python SDK 手写客户端 | `packages/sdk-python/src/ai_platform_sdk/__init__.py`、`run_stream.py`、`workbench.py`、`workbench_session.py` | `packages/sdk-python/tests/`，`httpx.MockTransport` |
| Python / Web 生成类型 | `types_runtime.py`、`types_gateway.py`、`packages/sdk-web/src/types/runtime.d.ts` | 与 `docs/api/*-openapi.json` 用现有生成器对照 |
| Web SDK 手写 Run 客户端 | `packages/sdk-web/src/run-client.ts`、`ai-assistant.ts` | `tsc --noEmit`；`examples/readonly-run.test.mjs`、`examples/run-history.test.mjs` |
| OpenAPI 与 FastAPI | `docs/api/runtime-openapi.json`（32 条路径）、`docs/api/gateway-openapi.json`（23 条路径） | `make openapi-check` |

手写客户端里的静态路径都能在 Runtime OpenAPI 中找到。`follow_run` 用 `quote()` 拼出 `/v1/runs/{run_id}/stream`，不是缺失路径。Web `RuntimeRunClient` 的工作台、反馈、Run 列表、审批/拒绝和 input 与 `services/runtime/src/runtime/api/` 的现有路由和请求体一致；`ai-assistant.ts` 调用 `/v1/messages`、`/v1/runs/{run_id}/approve`、`/v1/runs/{run_id}/feedback`。Python `create_run` 接受 Runtime 的 202；Web `create` 显式要求 202。

`make lint` 的 mypy 目标是契约、Runtime 和 harness 适配器，不含 Gateway、Policy 和 SDK 生成类型。Ruff 配置排除 `packages/sdk-python/src/ai_platform_sdk/types_*.py`。

## 3. 实际命令和结果

工具版本：Python 3.12.0，pytest 9.1.1，mypy 2.4.0，ruff 0.16.7，datamodel-codegen 0.85.0，Node v24.14.1，TypeScript 5.9.3，openapi-typescript 7.13.0，Vite 6.4.3。未安装或升级依赖。

执行前清掉 `VAULT_*`、`OTEL_EXPORTER_OTLP_ENDPOINT`、`DATABASE_URL`、`MODEL_GATEWAY_API_KEY`、`RUNTIME_LIVE_*`、`RUNTIME_TEST_DATABASE_URL`、`GATEWAY_TEST_*`、`GATEWAY_DATABASE_URL`、`FAILOVER_*`、`CEDAR_ENGINE_*`。pytest 使用 `-p no:cacheprovider` 和本轮专用 `--basetemp`。mypy 缓存放在本轮临时目录。

| 命令 | 退出码 | 结果 |
|---|---|---|
| `.venv/bin/mypy packages/contract/src services/runtime/src harness-adapters/claude-sdk/src harness-adapters/probe/src harness-adapters/stdio/src` | 0 | 109 个源文件通过。这是 `make lint` 的 mypy 范围 |
| `.venv/bin/mypy services/policy/src` | 0 | 3 个源文件通过 |
| `.venv/bin/mypy packages/contract/src services/capability-gateway/src services/policy/src` | 1 | 12 个错误，全部在 Gateway 的 5 个文件；Policy 无诊断 |
| `.venv/bin/mypy packages/sdk-python/src`（修复前） | 1 | 87 个错误，只在两个生成文件：`types_gateway.py` 63，`types_runtime.py` 24。`valid-type` 63，`assignment` 20，`misc` 4 |
| 同上（生成类型对齐后） | 1 | 98 个错误，仍只在两个生成文件：gateway 63，runtime 35。`valid-type` 73，`assignment` 21，`misc` 4 |
| 四个手写 SDK 模块单独 mypy | 0 | 4 个源文件通过 |
| `packages/sdk-web` 的 `tsc --noEmit`，对齐前后各一次 | 0 | 均通过 |
| `make openapi-check` | 0 | `docs/api` 与 Runtime、Gateway 代码一致 |
| 范围内 227 个 `.py` 经 `scripts/lint_ruff.py` 的 stdin 检查 | 0 | 通过。范围是 Runtime、Gateway、Policy、Python SDK、contract |
| `pytest packages/sdk-python`，对齐前后各一次 | 0 | 各 52 passed |
| `pytest services/policy -k` 排除 3 个会启动 cedar-engine 的用例 | 0 | 48 passed，3 deselected，1 warning |
| `pytest services/capability-gateway` | 0 | 241 passed，3 skipped，2 warnings |
| `pytest services/runtime -k "not test_real_"` | 0 | 713 passed，6 skipped，4 deselected，1 warning，115.49 秒 |
| `vite build` 后 `node --experimental-strip-types --test` 两个 example | 0 | 构建通过；5 passed，0 failed，0 skipped |

Runtime 的 6 个 skip：`test_mysql_concurrent_migrations_on_empty_db`、`test_mysql_migrate_lock_timeout`（未设 `RUNTIME_TEST_DATABASE_URL`）、`test_external_db_schema_matches_orm`（同上）、`test_process_level_sigkill_takeover_within_60s`（未使用 `-m failover`，且未设故障注入库）、`test_live_simple_chat`、`test_live_tool_call_round_trip`（未设 `RUNTIME_LIVE_LLM`）。4 个 deselected 是 `test_s9c_sandbox.py` 的 `test_real_*`。同文件里的非真实容器用例已执行。

Gateway 的 3 个 skip：`test_s3_store_roundtrip`（未设 `GATEWAY_TEST_S3`）、`test_live_sync_and_hybrid_search`（未设 `GATEWAY_TEST_ES_URL`）、`test_fastembed_bge_small_zh`（未设 `GATEWAY_TEST_FASTEMBED`）。同文件中的文件系统和替身用例已执行。

Policy 未执行：`test_cedar_engine_passes_shared_cases`、`test_engines_agree_on_every_case`、`test_legacy_check_on_cedar`。本机 `services/policy/cedar-engine/target/{debug,release}/cedar-engine` 都存在，收集后会监听随机端口，所以用 `-k` 排除，不是测试自己的 skip。

warning 来自 Starlette `anyio.abc.BlockingPortal` 弃用，以及 Gateway 一条测试用短 HMAC 密钥触发的 PyJWT `InsecureKeyLengthWarning`。没有失败用例。

只对 Runtime 跑 mypy、不把 `packages/contract/src` 列入命令时，会得到 8 个 `Any` 相关错误；只加 contract、不加入 harness 源码时，`runtime/harness/registry.py` 还有 1 个 `AcpStdioHarness` 返回 `Any` 的错误。把 `make lint` 的完整目标一起检查则是 0。这两次失败是检查路径不完整，不是源码在标准门禁下的失败。

## 4. 缺陷、修复和未改项

### 4.1 已修复：生成类型落后于当前 OpenAPI

`make openapi-check` 只比较 `docs/api` 和 FastAPI，不比较 SDK 生成文件。用 `scripts/gen_sdk_types.sh` 里的同一组 datamodel-codegen 参数，以及 `openapi-typescript docs/api/runtime-openapi.json`，在临时目录重生后：

- Web `runtime.d.ts` 从 18 条路径变为 32 条。新增的 14 条全是 `/v1/workbench/*` 和 `/v1/metrics/feedback-*`。统一 diff 的删除行为 0，已有路径块没有被删改。
- Python `types_runtime.py` 新增 `CreateBody`、`EvidenceBody`、`Prepare`、`PrepareBody`。与旧文件共有的模型，JSON Schema 约束差异为 0。
- Python `types_gateway.py` 模型集合和约束差异为 0，只有 `Field(...)` 换行。

根因是工作台和反馈指标进入 OpenAPI 之后，生成类型没有按现有脚本重生成。手写 `RuntimeRunClient` 和 `WorkbenchSession` 已经调用这些路由，所以定向测试原先也能通过；缺的是生成类型表面。

修复只覆盖上述三个生成文件。修复后 Web `tsc` 退出 0，Python SDK 52 passed。扩展 strict mypy 从 87 变为 98，因为新模型仍使用 `constr()` / `RootModel[constr(...)]` 和枚举字符串默认值；gateway 仍是 63。这不是新的错误种类。

### 4.2 未修复：生成类型的扩展 strict mypy

`from __future__ import annotations` 下，生成文件把 `constr()` / `conint()` 写在注解里，mypy 报 `valid-type`；枚举字段用字符串当默认值，报 `assignment`；`RootModel[constr(...)]` 报 `misc`。手写模块没有这些诊断。标准 `make lint` 不检查这些文件，Ruff 也排除它们，所以远端 CI 成功不能说明这项已通过。

临时预检使用 `--use-annotated --field-constraints`，不写回仓库。结果仍有 21 个 `assignment` 错误，不能清零。与同一 OpenAPI、同一生成器的现行参数相比，annotated 结果还会把 `ApprovalBody.calls` 表现为 `minItems=1` / `maxItems=32`。现行生成结果是：

```python
calls: list[ApprovalCall] | None = Field(None, title="Calls")
```

没有该长度约束。Runtime 服务端模型仍有 `min_length=1, max_length=32`；两边本来就不相等。切换生成参数会改变 SDK 模型的校验行为，因此暂停，不改 `scripts/gen_sdk_types.sh`，不手改生成文件，也不把生成文件加入 mypy ignore。扩展 strict 仍不是已交付门禁。

### 4.3 未改：Gateway 的 12 个 strict 诊断

这些文件不在 `make lint` 中。本轮 Gateway 测试 241 passed。读过对应代码后，没有把它当成运行时阻断：

- `SkillRepo.list` 等方法名叫 `list`，mypy 把注解里的 `list[...]` 当成该方法。类体执行时注解仍解析到内置 `list`。
- `skill_store.py` 的 `resp.read()` 在缺少 MinIO 类型存根时被看成 `Any`。
- `scan/structure.py` 在 `isinstance(front, dict)` 之后，mypy 仍未收窄 YAML 的 `Any`。
- `skills.py` 的状态迁移把路径元素看成 `str`，收不窄到状态字面量；运行时仍查 `TRANSITIONS`。

未改这些注解，避免为了清掉非标准门禁而改仓储接口或扫描逻辑。

## 5. 未运行项

| 项 | 原因 | 若要运行所需授权 |
|---|---|---|
| 全仓 `pytest` | 本轮不做重型全仓回归 | 集成后由主 agent 集中执行。本机 release/debug `cedar-engine` 都在，全量收集会启动该进程；沙箱镜像若已在本机，`test_real_*` 会启动容器 |
| Policy 三个真实 Cedar 用例 | 会启动 `cedar-engine` 并监听本地端口 | 允许启动本机 Cedar 进程后再跑，不要设成跳过 |
| Runtime 四个真实沙箱用例 | 会 `docker run`。本轮在 PATH 前放置只退出 1 的 `docker` 替身，避免连到 Docker daemon，并用 `-k "not test_real_"` 排除 | 允许启动沙箱容器，并确认镜像已在本机；不要为了通过而去拉镜像 |
| MySQL、真实 Elasticsearch、MinIO、FastEmbed、真实模型、进程级 failover | 对应环境变量未设置，测试自行 skip | 独立测试库、测试身份和明确的非生产端点；不能指向原库或真实模型 |
| Console / Portal 类型、浏览器和演示脚本 | 不在本轮可改范围 | 主 agent 在 B1 改动之上集中执行 |
| CI workflow、锁文件、生产部署 | 明确排除 | 不在本轮 |

本轮没有包一层网络审计器，因此不能声称进程对 `127.0.0.1:1` 的连接次数为零。Runtime 夹具把策略和网关地址指到该端口并注入策略替身；Gateway 契约测试同样把策略地址指到该端口。这些用例已通过，没有在本轮启动对应服务。

## 6. 公共接口和兼容性

没有修改 Runtime、Gateway、Policy 的路由、权限或业务语义，也没有修改手写 SDK 的方法、请求路径和状态校验。

生成类型的变化是对齐当前 OpenAPI：Web 路径类型只增加工作台和反馈指标；Python Runtime 类型只增加四个请求模型；已有模型的约束对照为 0。现有调用方继续使用旧模型和手写客户端时，校验行为不变。`ApprovalBody.calls` 的长度约束仍然只在服务端模型上，生成 SDK 模型维持原样，没有收紧也没有放宽。

未发布包，未改包版本。

## 7. 临时资源

使用 `/tmp/b2-verify-80e234dd` 作为临时根，内含 mypy 缓存、生成预检、pytest `--basetemp` 和 Runtime `tempfile.mkdtemp` 的 SQLite。清理时删除该目录和本轮新建的 `packages/sdk-web/dist`（构建前不存在，且被 `.gitignore` 忽略）。清理后这两个路径都不存在。

未发现本轮留在 `/tmp` 下、深度不超过 2 的 `runtime-test-*` 或 `s7-failover-*` 目录。未操作原库、真实密钥、真实模型、真实 Trace 或账号。未启动 Docker 容器、Cedar 引擎或业务服务。

## 8. 给主 agent 的集成顺序

1. 保留开始时已有的 Console、B1 报告、计划和 `scripts/local_demo.html` 修改，以及本轮期间由其他工作写入的平台验收矩阵和 B1 报告后续增补。不要用本报告覆盖它们。
2. 把下面三个生成文件作为同一组纳入，不要只拿其中一个：
   - `packages/sdk-python/src/ai_platform_sdk/types_runtime.py`
   - `packages/sdk-python/src/ai_platform_sdk/types_gateway.py`
   - `packages/sdk-web/src/types/runtime.d.ts`
3. 不要为了清掉 98 个生成类型诊断而改用 `--use-annotated` / `--field-constraints`，除非单独接受 `ApprovalBody.calls` 等模型的校验变化。当前预检即使加上这两个参数仍有 21 个错误。
4. 集成完成后再集中跑，不要把本轮数字和全仓数字相加：
   - `.venv/bin/python scripts/lint_ruff.py`
   - `.venv/bin/python scripts/lint_ruff.py --format`
   - `.venv/bin/mypy packages/contract/src services/runtime/src harness-adapters/claude-sdk/src harness-adapters/probe/src harness-adapters/stdio/src`
   - `make openapi-check`
   - `.venv/bin/python -m pytest packages/sdk-python services/policy services/capability-gateway services/runtime -q`
   - 全仓 `pytest`、Console/Portal 类型和浏览器放在 B1 改动合并后的同一次里。全仓前先确认是否允许启动本机 Cedar 和 Docker 沙箱。
5. 本轮没有提交或推送。`f35e2f8` 的远端成功不能写成当前工作区的验收结果。
