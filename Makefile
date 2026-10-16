# 常用命令入口。Python 侧使用 venv + pip（本机无 uv）；前端侧使用 pnpm（见 package.json）。
PY ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PYTHON := $(VENV)/bin/python
# 需要以可编辑模式安装的全部 Python 包（顺序无关，contract 会作为依赖被自动安装）
PKGS := packages/contract packages/sdk-python services/runtime services/console-api services/capability-gateway services/policy services/observe-adapter harness-adapters/claude-sdk harness-adapters/stdio mcp-servers/template

.PHONY: setup test lint run-runtime run-console run-gateway compose-up compose-down

# 创建虚拟环境并安装所有包；优先带 [dev] 额外依赖，包未声明 dev 时退回普通安装
setup:
	$(PY) -m venv $(VENV)
	$(PIP) install -U pip
	for p in $(PKGS); do $(PIP) install -e "$$p[dev]" || $(PIP) install -e $$p; done

# 运行全部 Python 测试（testpaths 见 pyproject.toml）
test:
	$(PYTHON) -m pytest -q

# 静态检查：Ruff 全仓 + mypy 检查契约与 Runtime
lint:
	$(VENV)/bin/ruff check . && $(VENV)/bin/mypy packages/contract/src services/runtime/src

# 启动 Runtime 节点（:8080，默认 mock Harness、SQLite）
run-runtime:
	$(VENV)/bin/uvicorn runtime.main:app --port 8080 --reload --app-dir services/runtime/src

# 启动 Console API（:8081）
run-console:
	$(VENV)/bin/uvicorn console_api.main:app --port 8081 --reload --app-dir services/console-api/src

# 启动能力网关（:8082）
run-gateway:
	$(VENV)/bin/uvicorn capability_gateway.main:app --port 8082 --reload --app-dir services/capability-gateway/src

# 启动 / 停止本地基础设施（deploy/compose-dev）
compose-up:
	docker compose -f deploy/compose-dev/docker-compose.yml up -d

compose-down:
	docker compose -f deploy/compose-dev/docker-compose.yml down
