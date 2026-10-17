# 常用命令入口。Python 侧使用 venv + pip（本机无 uv）；前端侧使用 pnpm（见 package.json）。
PY ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PYTHON := $(VENV)/bin/python
# 需要以可编辑模式安装的全部 Python 包（顺序无关，contract 会作为依赖被自动安装）
PKGS := packages/contract packages/sdk-python services/runtime services/console-api services/capability-gateway services/policy services/observe-adapter harness-adapters/claude-sdk harness-adapters/stdio mcp-servers/template

.PHONY: setup test test-mysql migrate migrate-new verify-s1 verify-s2 verify-s3 lint openapi openapi-check sdk-types run-runtime run-console run-gateway run-observe compose-up compose-down compose-ps precommit-install precommit images

# 创建虚拟环境并安装所有包；优先带 [dev] 额外依赖，包未声明 dev 时退回普通安装
setup:
	$(PY) -m venv $(VENV)
	$(PIP) install -U pip
	for p in $(PKGS); do $(PIP) install -e "$$p[dev]" || $(PIP) install -e $$p; done

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

# 静态检查：Ruff 全仓（经 scripts/lint_ruff.py，兼容本机透明加密）+ mypy 严格检查契约与 Runtime
lint:
	$(PYTHON) scripts/lint_ruff.py && $(PYTHON) scripts/lint_ruff.py --format && $(VENV)/bin/mypy packages/contract/src services/runtime/src harness-adapters/claude-sdk/src

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

verify-s3:  ## S3 本机出口验证：Console 灰度放量与回滚、门户 PKCE 登录后对话、反馈进 Langfuse（需 keycloak、litellm、observe profile）
	$(PYTHON) scripts/verify_s3_e2e.py

verify-s2:  ## S2 本机出口验证：Keycloak JWT、统一审计、Vault 密钥注入、Prompt Cache 命中统计（需 keycloak、vault、observe profile）
	$(PYTHON) scripts/verify_s2_e2e.py
