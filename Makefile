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
# 连接器（S8）：ai-mcp-template 是 ai-mcp-ticket / ai-mcp-mocks 的依赖，同样排在它们前面、在同一条命令里以 -e 给出
# （发行名带 ai- 前缀；旧发行名 mcp-template 已弃用，本机残留时 pip uninstall mcp-template）。
# CI 需要 claude-sdk 的真实 SDK 时：make setup CLAUDE_SDK_EXTRAS=dev,sdk
CLAUDE_SDK_EXTRAS ?= dev
# runtime 的 extras；需要 MySQL 驱动（asyncmy，如 CI 的 failover job、make test-mysql）时：make setup RUNTIME_EXTRAS=dev,probe,mysql
RUNTIME_EXTRAS ?= dev,probe
# 能力网关的 extras（S8）：目录 MySQL 后端（make test-gateway-mysql、verify-s8、dev_stack）需要 mysql；本机语义检索需要 fastembed：
# make setup GATEWAY_EXTRAS=dev,mysql,fastembed（CI 的 e2e 用 dev,mysql + EMBED_PROVIDER=hash）
GATEWAY_EXTRAS ?= dev
PKGS := packages/contract[dev] harness-adapters/probe[dev] packages/sdk-python[dev] services/runtime[$(RUNTIME_EXTRAS)] services/console-api[dev] services/capability-gateway[$(GATEWAY_EXTRAS)] services/policy[dev] services/observe-adapter[dev] harness-adapters/claude-sdk[$(CLAUDE_SDK_EXTRAS)] harness-adapters/stdio[dev,probe,acp] mcp-servers/template[dev] mcp-servers/ticket[dev] mcp-servers/mocks[dev]

.PHONY: setup test test-mysql test-gateway-mysql test-gateway-es cedar-build cedar-test cedar-run verify-s8 migrate migrate-new verify-s1 verify-s2 verify-s3 verify-s4 verify-s5 verify-s6 verify-s7 dataplane-poc probe probe-real eval-ticket-summary lint openapi openapi-check sdk-types run-runtime run-console run-gateway run-observe compose-up compose-down compose-ps precommit-install precommit images

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

# 能力网关目录的 SQL 后端测试同时对 compose 的 MySQL 运行（库不存在先创建；用例会删除该库中的 cg_* 表）
TEST_GATEWAY_MYSQL_URL ?= mysql+asyncmy://root:root@localhost:3316/ai_gateway_test
test-gateway-mysql:
	$(PYTHON) scripts/devkit.py ensure-db $(TEST_GATEWAY_MYSQL_URL)
	GATEWAY_TEST_DATABASE_URL=$(TEST_GATEWAY_MYSQL_URL) $(PYTHON) -m pytest services/capability-gateway -q

# 能力网关的真实 Elasticsearch 8.x 用例（索引 / outbox 同步 / 混合检索）；本机默认带 fastembed（bge-small-zh）语义向量用例，
# 不需要时：make test-gateway-es GATEWAY_TEST_FASTEMBED=
TEST_ES_URL ?= http://localhost:9200
GATEWAY_TEST_FASTEMBED ?= 1
test-gateway-es:
	GATEWAY_TEST_ES_URL=$(TEST_ES_URL) GATEWAY_TEST_FASTEMBED=$(GATEWAY_TEST_FASTEMBED) $(PYTHON) -m pytest services/capability-gateway -q

# Cedar 判定引擎 sidecar（Rust，services/policy/cedar-engine，默认监听 127.0.0.1:8183）。
# 本机的全局 ~/.cargo/config 把 crates-io 替换成了镜像，而依赖须取自官方 crates.io：在仓库外的目录运行 cargo
# 并使用独立 CARGO_HOME，避开该配置。CI 没有镜像配置，直接 make cedar-build CEDAR_CARGO=cargo。
CEDAR_DIR := $(abspath services/policy/cedar-engine)
CEDAR_BIN := $(CEDAR_DIR)/target/release/cedar-engine
CEDAR_CARGO ?= cd /tmp && CARGO_HOME=$(HOME)/.cache/cargo-crates-io cargo
cedar-build:
	$(CEDAR_CARGO) build --release --locked --manifest-path $(CEDAR_DIR)/Cargo.toml

# Rust 单元 / 集成测试 + 策略服务对真实引擎的 Python 用例（未编译出引擎即失败，不跳过）
cedar-test: cedar-build
	$(CEDAR_CARGO) test --release --locked --manifest-path $(CEDAR_DIR)/Cargo.toml
	CEDAR_ENGINE_REQUIRED=1 CEDAR_ENGINE_BIN=$(CEDAR_BIN) $(PYTHON) -m pytest services/policy -q

# 前台运行 Cedar 引擎（策略文件缺省为 services/policy/cedar/policies.cedar，每 5 s 检查改动并热加载）
cedar-run: cedar-build
	$(CEDAR_BIN)

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

# 启动观测适配器（:8084）
run-observe:
	$(VENV)/bin/uvicorn observe_adapter.main:app --port 8084 --reload --app-dir services/observe-adapter/src

# 启动能力网关（:8082）
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

verify-s7:  ## S7 本机出口验证：Run 持久化执行（API×2 + Worker×2、接管 / 对账 / 取消 / 迁移 / 凭据 / 预算；需 compose 的 mysql、redis、keycloak）
	$(PYTHON) scripts/verify_s7_e2e.py

verify-s8:  ## S8 本机出口验证：能力网关 V1 五条链路（Keycloak + 网关 mysql/ES/fastembed + 策略服务 + Cedar + 生产态连接器 + Runtime 服务客户端；需 compose 的 mysql、redis、elasticsearch、keycloak 与 make cedar-build）
	$(PYTHON) scripts/verify_s8_e2e.py

verify-s2:  ## S2 本机出口验证：Keycloak JWT、统一审计、Vault 密钥注入、Prompt Cache 命中统计（需 keycloak、vault、observe profile）
	$(PYTHON) scripts/verify_s2_e2e.py
