# B2 Gateway 类型收口并行报告

检查时间：2026-10-27。这是 Gateway 扩展 strict mypy 的定向结果，不是全仓或生产验收。未提交、未推送。原《B2后端与SDK并行验收》未改。

仓库内仍没有 `AGENTS.md`。依据该 B2 报告第 4.3 节、交付重排计划的 B2 行，以及平台验收矩阵文末「B2报告接收与B1/B2集成复核」：Gateway 扩展 strict 当时记为 12 项未关闭，并要求单独做最小类型修复，不能把它说成现行 `make lint` 失败。

## 1. HEAD、修改文件与 SHA-256

HEAD 为 `f35e2f82178183161c5ead076dafb7af77f44ef7`。Gateway 源码在修改前没有未提交差异。SDK 生成类型、Console、B1 报告、实施计划、验收矩阵、`scripts/lint_ruff.py`、`scripts/local_demo.html` 以及未跟踪的《B2后端与SDK并行验收》《2026-10-27-B1B2集中验收执行》都保持原样，本轮没有编辑。

本轮只改了下面 5 个文件。SHA-256 是 Python 读到的文件字节，对应最终内容：

| 文件 | SHA-256 |
|---|---|
| `services/capability-gateway/src/capability_gateway/skill_repo.py` | `0057c6d174b3404833478f909b957cc444c958d3d902900f4aeecf71165c81a4` |
| `services/capability-gateway/src/capability_gateway/db.py` | `2c54070b18f2c3fbb2ef23ad3f67c1aeb8355a3a4cd5eeb80a8d0dc8e5241b14` |
| `services/capability-gateway/src/capability_gateway/skill_store.py` | `a07cb026ff2f8c05b502f2c50d1051dad9093929edd90ae35f1ec384d45a3f8e` |
| `services/capability-gateway/src/capability_gateway/scan/structure.py` | `e857e7322e041452653e572c10b131d49b0aec29efefa39fd9497c72e1a286ea` |
| `services/capability-gateway/src/capability_gateway/skills.py` | `eb559aabb2ae6f0c9bda5118c87b7a89276066143b3ac5704228d00df7c23293` |

## 2. 复现与逐项处理

修复前按 B2 的完整路径执行：

`.venv/bin/mypy packages/contract/src services/capability-gateway/src services/policy/src`

退出码 1。40 个源文件中 12 个错误，全部在 Gateway，数量与 B2 记录相同。Policy 无诊断。修复后同一命令退出码 0，40 个源文件、0 个错误。mypy 2.4.0，缓存放在本轮临时目录。

| 修复前位置 | 原因 | 是否运行风险 | 处理 |
|---|---|---|---|
| `skill_repo.py:93` `SkillRepo.events` | 类里已有名为 `list` 的方法，后面的注解 `list[SkillEvent]` 被 mypy 当成该方法。文件已有 `from __future__ import annotations`，运行时不计算这个注解 | 否。方法名和运行时查找不变 | 返回类型写成 `builtins.list[SkillEvent]` |
| `skill_repo.py:133` `MemorySkillRepo.events` | 同上 | 否 | 同上 |
| `db.py:510` `SqlSkillRepo.events` | 同上 | 否 | 同上 |
| `skill_store.py:87` `S3Store._get` | `self.client` 声明为 `Any`，`resp.read()` 因此是 `Any`。已安装的 urllib3 `HTTPResponse.read()` 在不传长度时返回 `bytes` | 类型缺口，不是新的运行缺陷。客户端仍是可选的 minio 对象 | 把 `read()` 的结果标为 `bytes` 再返回。没有新增 `isinstance` 拒绝，读失败仍走原来的异常和 `finally` 关闭 |
| `structure.py:154` 及 157、165 | `meta = front.get("metadata") if isinstance(front.get("metadata"), dict) else {}` 的条件和第二处取值不是同一个表达式，mypy 把结果留成 `Any \| dict \| None` | 否。非 dict 本来就会换成 `{}`，随后才读取 | 先取出一次 `raw_meta`，再用同一个对象做 `isinstance`。dict 仍用原对象，否则仍是 `{}` |
| `structure.py:169`–`172` | 上一项使 `meta` 不能传给 `_choice` / `_declared_rate` 要求的 `dict[str, Any]` | 否，随上一项消失 | 收窄后不再报错，没有改这两个函数的参数或返回值 |
| `skills.py:219` `_move` | `cur` 是 `Status` 字面量，循环变量 `s` 是 `str`。运行时已经用 `s not in TRANSITIONS[cur]` 拒绝非法迁移 | 否。非法状态本来就是 409 `invalid_state` | 用 `_known_status` 把 9 个既有状态收窄为 `Status`。未知或当前状态不允许时，仍抛原来的 `GatewayError(409, "invalid_state", ...)`，消息不变 |

`_STATUSES` 与 `TRANSITIONS` 的键，以及所有迁移目标，在导入时用两条 `assert` 对齐。当前表一致，所以导入不会失败。没有新增对外异常类型。

没有使用 `Any`、`cast`、`type: ignore`，也没有改 mypy 或 Ruff 配置。`list` 这个仓储方法没有改名。

## 3. 行为与公共接口

路由、权限、状态机允许的迁移、错误码和错误文本没有改。`events` 只改了不被运行时计算的注解。`list` 的方法名、参数和调用方不变。

`_get` 仍返回 `read()` 的结果；注解不在运行时拦截非 bytes。`parse_package` 对 metadata 仍是「dict 用原对象，否则空 dict」，只是不再调用两次 `get`。`_move` 对当前 9 个状态的接受和拒绝与原来的 `TRANSITIONS` 成员判断一致；空路径仍会在 `path[-1]` 处抛出原来的 `IndexError`。

Gateway 模块测试在最终代码上为 241 passed、3 skipped、2 warnings，退出码 0。3 个 skip 仍是未设置 `GATEWAY_TEST_S3`、`GATEWAY_TEST_ES_URL`、`GATEWAY_TEST_FASTEMBED` 的原有用例：`test_s3_store_roundtrip`、`test_live_sync_and_hybrid_search`、`test_fastembed_bge_small_zh`。warnings 仍是 Starlette `BlockingPortal` 弃用和测试短 HMAC 的 `InsecureKeyLengthWarning`。

所改 5 个文件的 Ruff check 与 format check 退出码 0。最终 `skills.py` 再次检查也是 0。

## 4. 命令

| 命令 | 退出码 | 结果 |
|---|---|---|
| 修复前 `.venv/bin/mypy packages/contract/src services/capability-gateway/src services/policy/src` | 1 | 12 errors / 40 files |
| 最终同一 mypy 命令 | 0 | 0 errors / 40 files |
| 最终 `.venv/bin/python -m pytest services/capability-gateway -q -p no:cacheprovider` | 0 | 241 passed，3 skipped，2 warnings，45.14 秒 |
| 所改文件经 `scripts/lint_ruff.py` 的 check 与 format 逻辑 | 0 | 通过 |

未跑全仓 pytest、浏览器、Console、Portal、Runtime、Policy 的 Cedar 进程。未启动 Docker。未设置真实 ES、S3、MySQL 或 FastEmbed。

## 5. 未解决项

没有留下这 12 项中的未修诊断。下面各项本来就不在本轮修复范围：

- SDK 生成类型的扩展 strict（B2 记录对齐后 98 项）仍在，生成文件未改。
- 标准 `make lint` 原先就不包含 Gateway。这次通过只说明上述三目录的 strict mypy 现在是 0，不能代替全仓。
- 真实 MinIO、Elasticsearch、FastEmbed 和 MySQL 目录库仍按原 skip 未运行。

没有需要改变公共接口才能继续的暂停项。

## 6. 临时资源

临时根 `/tmp/b2-gw-mypy-80e234dd` 存放 mypy 缓存、日志和 pytest `--basetemp`。报告落盘后已删除该目录，未残留。未连接原库、真实服务、模型或密钥。未新增仓库内测试文件。

## 7. 给主 agent 的集成建议

1. 保留 SDK 生成类型、B1、验收矩阵、`scripts/lint_ruff.py` 和演示脚本上已有改动，只叠加这 5 个 Gateway 文件和本报告。
2. 集成后重跑 `.venv/bin/mypy packages/contract/src services/capability-gateway/src services/policy/src`，预期 0。
3. Gateway 定向回归可重跑 `pytest services/capability-gateway`。不要把 241 passed 与 Runtime、SDK 或浏览器数字相加。
4. 全仓 pytest 仍要单独确认 Cedar 监听和 Docker 沙箱是否被允许。本轮没有跑。
5. 不要为了 SDK 的 98 项诊断去改 Gateway，也不要把这次 mypy 通过写成生产验收。
