# 常用命令入口。Python 侧使用 venv + pip（本机无 uv）；前端侧使用 pnpm（见 package.json）。
PY ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PYTHON := $(VENV)/bin/python
# 需要以可编辑模式安装的全部 Python 包及其 extras。本地包名（ai-contract、ai-harness-probe、ai-harness-claude-sdk 等）
# 在 PyPI 上未注册：必须与依赖它们的包在同一条 pip 命令里以 -e 显式给出，pip 才会只用这些本地候选、不去索引查同名包
# （防依赖混淆）；因此 setup 一次性安装、不逐个回退。被依赖者排在前面（contract → probe → 其余）。
# runtime / stdio 带上 probe extra、claude-sdk 带 dev（含 ai-harness-probe），使这些 extras 也在同一次解析里落到本地包；
# stdio 另带 acp extra（goose / dsh 的 ACP 桥，S6）。
# CI 需要 claude-sdk 的真实 SDK 时：make setup CLAUDE_SDK_EXTRAS=dev,sdk
CLAUDE_SDK_EXTRAS ?= dev
PKGS := packages/contract[dev] harness-adapters/probe[dev] packages/sdk-python[dev] services/runtime[dev,probe] services/console-api[dev] services/capability-gateway[dev] services/policy[dev] services/observe-adapter[dev] harness-adapters/claude-sdk[$(CLAUDE_SDK_EXTRAS)] harness-adapters/stdio[dev,probe,acp] mcp-servers/template[dev] mcp-servers/ticket[dev]

.PHONY: setup test test-mysql migrate migrate-new verify-s1 verify-s2 verify-s3 verify-s4 verify-s5 verify-s6 dataplane-poc probe probe-real eval-ticket-summary lint openapi openapi-check sdk-types run-runtime run-console run-gateway run-observe compose-up compose-down compose-ps precommit-install precommit images

# 创建虚拟环境并一次性安装所有本地包（任一失败即整体失败，不静默降级）
setup:
	$(PY) -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install $(foreach p,$(PKGS),-e "$(p)")

# 运行全部 Python 测试（testpaths 见 pyproject.toml）
test:
	$(PYTHON) -m pytest -q

# 用 compose 的 MySQL 跑 Runtime 测试：测试库 ai_platform_test 由 conftest 自动创建并在会话开始前清空重建
TEST_MYSQL_URL ?= mysql+asyncmy://root:root@localhost:3316/ai_platform_test
TEST_MYSQL_CONSOLE_URL ?= mysql+asyncmy://root:root@localhost:3316/ai_platform_console_test
test-mysql:
	RUNTIME_TEST_DATABASE_URL=$(TEST_MYSQL_URL) $(PYTHON) -m pytest services/runtime -q
	CONSOLE_TEST_DATABASE_URL=$(TEST_MYSQL_CONSOLE_URL) $(PYTHON) -m pytest services/console-api -q

# 把 DATABASE_URL 指向的 Runtime 引擎库升级到最新迁移（Runtime 启动时也会自动执行）
migrate:
	cd services/runtime && $(abspath $(VENV))/bin/alembic upgrade head

# 按 ORM 模型与当前库的差异自动生成新迁移：make migrate-new m="add xxx column"
migrate-new:
	cd services/runtime && $(abspath $(VENV))/bin/alembic revision --autogenerate -m "$(m)"

# 静态检查：Ruff 全仓（经 scripts/lint_ruff.py，兼容本机透明加密）+ mypy 严格检查契约、Runtime、claude-sdk / stdio 适配器与契约探针包
lint:
	$(PYTHON) scripts/lint_ruff.py && $(PYTHON) scripts/lint_ruff.py --format && $(VENV)/bin/mypy packages/contract/src services/runtime/src harness-adapters/claude-sdk/src harness-adapters/probe/src harness-adapters/stdio/src

# 契约探针上架门禁（S5 / D86）：对 runtime.harness.registry.KNOWN 中每个 harness 跑五项探针，任一不通过即失败；
# 报告写到 probe-report.json。claude-sdk 的进程内探针需要 SDK：make setup CLAUDE_SDK_EXTRAS=dev,sdk
# goose / dsh（S6）：本机装了二进制就真实运行；CI 不下载第三方二进制，缺失时记 unavailable 不算失败，
# ACP 桥本身由 fake ACP agent 过五项探针（直连与进程池，报告 probe-report-acp-fake.json）。
PROBE_FAKES := harness-adapters/stdio/tests/fakes
probe:
	$(PYTHON) -m ai_harness_probe.gate --report probe-report.json --allow-unavailable goose --allow-unavailable dsh --timeout 60
	PYTHONPATH=$(PROBE_FAKES) $(PYTHON) -m ai_harness_probe.gate --known fake_known:KNOWN --report probe-report-acp-fake.json --timeout 60

# 本机门禁（S6，D98）：goose / dsh 真实二进制 + 脚本化 shim 跑五项探针，二进制缺失即失败（不允许 unavailable）；
# 报告 probe-report-real.json（能力等级以它为准），并按 (harness, 版本) 存档到 evals/reports/probe-real/<harness>-<version>.json
# （只含结果与能力声明，不含路径 / 密钥；门禁失败也存档，退出码仍为门禁的结果）
PROBE_REAL_ARCHIVE := evals/reports/probe-real
probe-real:
	@rc=0; $(PYTHON) -m ai_harness_probe.gate --report probe-report-real.json --harness goose --harness dsh --timeout 90 || rc=$$?; \
	$(PYTHON) -m ai_harness_stdio.probe_archive probe-report-real.json $(PROBE_REAL_ARCHIVE) || rc=1; \
	exit $$rc

# 导出 Runtime / 能力网关的 OpenAPI 文档到 docs/api/（info.version 固定 v1，键排序，可重复生成）
openapi:
	$(PYTHON) scripts/export_openapi.py

# 校验 docs/api 下的 OpenAPI 与代码一致：重新导出到临时目录后比较，有差异则失败
openapi-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) scripts/export_openapi.py --out-dir $$tmp >/dev/null || { rm -rf $$tmp; exit 1; }; \
	if diff -u docs/api/runtime-openapi.json $$tmp/runtime-openapi.json && diff -u docs/api/gateway-openapi.json $$tmp/gateway-openapi.json; then \
		rm -rf $$tmp; echo "docs/api OpenAPI 与代码一致"; \
	else \
		rm -rf $$tmp; echo "docs/api 下的 OpenAPI 已过期：请运行 make openapi（必要时再运行 make sdk-types）并提交结果" >&2; exit 1; \
	fi

# 由 docs/api 下的 OpenAPI 生成 Python / Web SDK 类型骨架（见 scripts/gen_sdk_types.sh）
sdk-types:
	VENV=$(VENV) bash scripts/gen_sdk_types.sh

# 启动 Runtime 节点（:8080，默认 mock Harness、SQLite）
run-runtime:
	$(VENV)/bin/uvicorn runtime.main:app --port 8080 --reload --app-dir services/runtime/src

# 启动 Console API（:8081）
run-console:
	$(VENV)/bin/uvicorn console_api.main:app --port 8081 --reload --app-dir services/console-api/src

# 启动能力网关（:8082）
run-observe:
	$(VENV)/bin/uvicorn observe_adapter.main:app --port 8084 --reload --app-dir services/observe-adapter/src

run-gateway:
	$(VENV)/bin/uvicorn capability_gateway.main:app --port 8082 --reload --app-dir services/capability-gateway/src

# 启动 / 停止本地基础设施（deploy/compose-dev）。根目录存在 .env 时自动读取（模型 Key 等，见 .env.example）；
# 默认带 observe profile（Langfuse 全家桶），只要基础设施时：make compose-up COMPOSE_PROFILES=
COMPOSE_PROFILES ?= observe
COMPOSE_ENV := $(if $(wildcard .env),--env-file .env,)
COMPOSE := docker compose $(COMPOSE_ENV) -f deploy/compose-dev/docker-compose.yml $(if $(COMPOSE_PROFILES),--profile $(COMPOSE_PROFILES),)
compose-up:
	$(COMPOSE) up -d

compose-down:
	$(COMPOSE) down

compose-ps:
	$(COMPOSE) ps

# 安装 git pre-commit 钩子（ruff / ruff-format / mypy；gitleaks 为 manual 阶段，由 CI 强制）
precommit-install:
	$(VENV)/bin/pip install -q pre-commit mypy
	$(VENV)/bin/pre-commit install

# 对全仓执行全部 pre-commit 钩子
precommit:
	$(VENV)/bin/pre-commit run --all-files

# 构建 5 个服务镜像（构建上下文为仓库根目录，因为需要拷贝 packages/contract）
images:
	for s in runtime console-api capability-gateway policy observe-adapter; do \
	  docker build -f services/$$s/Dockerfile -t ai-platform/$$s:dev . || exit 1; \
	done

verify-s1:  ## S1 本机出口验证：两种 Harness 真实对话 + Langfuse 查 Trace（需 compose observe profile 与模型 Key）
	$(PYTHON) scripts/verify_s1_e2e.py

verify-s4:  ## S4 本机出口验证：工单总结 Agent 全链路（网关 + MCP Server + OBO）、30 条评测与闸门、安全测试（需 keycloak 26.3、litellm、observe profile）
	$(PYTHON) scripts/verify_s4_e2e.py

eval-ticket-summary:  ## 对运行中的 Runtime 执行工单总结评测（30 条，结果写回 Langfuse）
	$(PYTHON) scripts/eval_ticket_summary.py --runtime http://localhost:8080 --agent ticket-summary --version 1

verify-s3:  ## S3 本机出口验证：Console 灰度放量与回滚、门户 PKCE 登录后对话、反馈进 Langfuse（需 keycloak、litellm、observe profile）
	$(PYTHON) scripts/verify_s3_e2e.py

verify-s5:  ## S5 本机出口验证：Contract 六要素完整化（hooks、结构化续跑、安全遗留；需 litellm、observe、keycloak 与模型 Key）
	$(PYTHON) scripts/verify_s5_e2e.py

dataplane-poc:  ## ADR-0005 数据面 POC（需 keycloak + observe profile，镜像见 evals/dataplane-poc/README.md）
	$(PYTHON) evals/dataplane-poc/poc.py up && $(PYTHON) evals/dataplane-poc/poc.py bench $(POC_ARGS) && $(PYTHON) evals/dataplane-poc/cases.py && $(PYTHON) evals/dataplane-poc/poc.py report; $(PYTHON) evals/dataplane-poc/poc.py down

verify-s6:  ## S6 本机出口验证：harness 切换演练 claude-sdk → goose → dsh → claude-sdk（需 litellm、observe、keycloak、DeepSeek Key 与本机 goose / dsh）
	$(PYTHON) scripts/verify_s6_e2e.py

verify-s2:  ## S2 本机出口验证：Keycloak JWT、统一审计、Vault 密钥注入、Prompt Cache 命中统计（需 keycloak、vault、observe profile）
	$(PYTHON) scripts/verify_s2_e2e.py
