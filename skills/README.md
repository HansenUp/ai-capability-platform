# Skills（Agent Skills 开放规范）

每个子目录为一个技能包：`SKILL.md`（frontmatter 含 `name`、`description` 与 `metadata`）+ 必需 `evals/cases.jsonl` + 可选 `scripts/`、`references/`、`assets/`。

`metadata`（字符串映射）：`version`（x.y.z）、`ai-platform.capabilities`（能力 id，空白 / 逗号分隔）、`ai-platform.risk-level`（read / write_low / write_high）、
`ai-platform.data-class`（public / internal / confidential）、`ai-platform.sandbox`（none / read-only / workspace-write / full-access；带 `scripts/` 时不得为 none）。
`evals/cases.jsonl` 每行 `{id, input, must_call, must_not_call, contains, data_class: public}`，至少 10 条。

上架流水线（能力网关 `/skills`，S9）：上传源 tgz → 服务端静态扫描（规则 id 见 `capability_gateway.scan.RULES`）→ Console 评测回放 → skill-reviewer 审批（禁自批）→ 平台 ES256 签名（cosign sign-blob 兼容）→ listed。
离线自检：`.venv/bin/python -c "from capability_gateway.scan import scan_dir; print(scan_dir('skills/<name>').model_dump_json(indent=2))"`。
硬规则：禁止运行时远程拉取可执行内容；必须声明所需能力；必须自带评测用例。

评测只执行包内不可变用例，调用方不能替换输入或删除断言。网关只接受 `SKILL_EVAL_TRUSTED_CALLERS` 中的专用 Console 身份回写，生产要求 `langfuse:` 引用与租户绑定。

`read-only` 脚本强制断网。签名清单声明 `workspace-write` 或 `full-access` 的脚本一律按高风险写处理，必须经持久化审批与写前登记；`full-access` 还要求配置沙箱出站代理。权限只取签名清单，模型不能修改脚本或提升级别。
审批绑定签名包／脚本哈希、stdin、Run 工作区、镜像、沙箱配置、执行用户与 Docker 引擎。影响预览是声明式作用域，不执行脚本，也不声称预测文件差异。脚本启动前再次校验审批、操作状态与租约纪元。
异常、非零退出、超时或 Worker 崩溃均可能留下部分副作用，必须由发起人对账 `done`／`skip`；脚本没有下游幂等承诺，禁止 `retry`。对账先在原 Docker 引擎清理该操作标记的容器，清理失败或配置／引擎不一致时不允许结算。`skip` 表示放弃未知结果，不表示撤销已经产生的副作用。
故障接管节点须访问同一持久工作区和 Docker 引擎，使用相同签名信任与沙箱配置。不同引擎或不同工作区路径不做透明重执行；须恢复原执行环境后核实并对账。只读容器使用 UID 65534；写容器使用 Runtime 的非 root UID/GID，Runtime 为 root 时仍用 65534，需预先保证工作区权限，不自动放宽目录权限。
签名 Skill 的正文、名称描述、原生技能目录和工具结果均继承清单数据等级，未经单独签署的元数据不得降级。运行中的技能工具调用也检查吊销，生效边界由吊销列表 TTL 和最大沿用时长控制。
