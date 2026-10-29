# AI Capability Platform

**English** | [简体中文](README.zh-CN.md)

An open-source, enterprise-grade **AI capability platform**: one place to build, run, govern and observe AI Agents, and to expose enterprise capabilities (business systems, data, knowledge, Skills, models) to those Agents in a discoverable, authorized and auditable way.

The design is built around four unifications: **unified identity · unified capabilities · unified runtime · unified observability and operations**.

- **Backend**: Python 3.11+ · FastAPI · SQLAlchemy 2 (async) · Pydantic v2 · Ruff · Pytest
- **Frontend**: Vue 3 · Vite · TypeScript · Lit (Web Component SDK) · pnpm workspace
- **Infrastructure**: Keycloak (OIDC / Token Exchange) · Apache APISIX · LiteLLM (model gateway) · Cedar (policy as code) · Langfuse + OpenTelemetry (observability & evaluation) · MySQL / Redis / MinIO / Elasticsearch · Docker Compose / Helm / Argo CD

---

## Contents

- [Product Architecture](#product-architecture)
- [Technical Architecture](#technical-architecture)
- [Repository Layout](#repository-layout)
- [Quick Start](#quick-start)
- [Documentation](#documentation)
- [License](#license)

---

## Product Architecture

Five core layers (AI touchpoints → Agent Runtime → AI Capability Gateway → Enterprise AI capabilities → Infrastructure) plus two cross-cutting pillars: identity, permissions & audit, and observability, operations & evaluation.

![Functional architecture](docs/architecture/01-%E5%8A%9F%E8%83%BD%E6%9E%B6%E6%9E%84%28%E7%AE%80%E6%98%8E%29.png)

---

## Technical Architecture

Touchpoints → unified access gateway → stateless, horizontally scalable Agent Runtime nodes (alongside Agent Console and identity & policy) → capability gateway, model gateway and observability → infrastructure.

![Technical architecture](docs/architecture/02-%E6%8A%80%E6%9C%AF%E6%9E%B6%E6%9E%84%28%E7%AE%80%E6%98%8E%29.png)

### Architecture → code

| Layer | Code |
|---|---|
| AI touchpoints (portal, Console, desktop, embeddable widget) | `apps/portal-web`, `apps/console-web`, `apps/desktop`, `packages/sdk-web`, `packages/sdk-python` |
| Agent Runtime (Run execution, context engineering, tool gate, Harness adapters) | `services/runtime`, `harness-adapters/*`, `packages/contract` |
| AI Capability Gateway (catalog, two-level discovery, authz, Token Exchange, MCP, Skill store) | `services/capability-gateway`, `services/data-hub`, `mcp-servers/*`, `skills/` |
| Enterprise AI capabilities (model gateway, evaluation) | `services/model-gateway`, `evals/` |
| Infrastructure (identity, storage, deployment) | `services/identity`, `deploy/` |
| Cross-cutting: identity · permissions · audit | `services/policy`, `services/console-api` |
| Cross-cutting: observability · operations · evaluation | `services/observe-adapter`, `evals/` |

---

## Repository Layout

```
ai-capability-platform/
├── packages/
│   ├── contract/          # Runtime Contract: events, Run state machine, ToolSpec, HarnessAdapter (single source)
│   ├── sdk-python/        # Python client: chat / create_run / get_run / approve
│   └── sdk-web/           # <ai-assistant> Lit web component: SSE, approval cards
├── services/
│   ├── runtime/           # Agent Runtime node (:8080)
│   ├── console-api/       # Agent Console backend (:8081)
│   ├── capability-gateway/# Capability gateway (:8082): discovery, authz proxy, token exchange, MCP, Skills
│   ├── policy/            # Policy service (:8083): Cedar policies & schema, /authz/check
│   ├── observe-adapter/   # Open reporting (:8084): feedback / usage / logs / status → Langfuse
│   ├── data-hub/          # Agent Data Hub
│   ├── model-gateway/     # LiteLLM config: pinned model aliases, routing, fallback
│   └── identity/          # Keycloak realm export
├── harness-adapters/      # stdio (NDJSON subprocess), claude-sdk (in-process), probe (contract probes)
├── mcp-servers/           # MCP Server template + example servers
├── skills/                # Agent Skills packages (SKILL.md + references + scripts)
├── evals/                 # Snapshot replay tests, datasets, security cases
├── apps/                  # portal-web, console-web (Vue 3), desktop (Tauri)
├── deploy/                # compose-dev, helm, argocd, apisix
├── docs/                  # architecture diagrams (docs/architecture) · OpenAPI specs (docs/api)
├── Makefile               # setup / test / lint / run-* / compose-* commands
└── package.json           # pnpm workspace (apps/*, packages/sdk-web)
```

---

## Quick Start

Prerequisites: Python 3.11+, Node.js with pnpm, Docker (for local dependencies).

### Backend

```bash
make setup        # create .venv and install all Python packages in editable mode
make test         # run all Python tests
make run-runtime  # start the Runtime on :8080 (mock Harness, SQLite, no auth by default)
```

Create and publish an Agent, then chat with it (in dev mode the `X-AI-Subject` header carries the identity):

```bash
curl -s localhost:8080/admin/agents -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"agent_id":"demo","version":"1","model_alias":"claude-sonnet-2026-06","system_prompt":"You are an enterprise assistant","config":{"tools":["echo","ticket.search","ticket.close_batch"],"skills":[{"name":"ticket-summary"}]}}'
```

```bash
curl -s -X POST localhost:8080/admin/agents/demo/versions/1/publish -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"eval_run_ref":"eval-1","eval_passed":true}'
```

```bash
curl -N localhost:8080/v1/messages -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"agent_id":"demo","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

Trigger a high-risk tool and go through approval (mock Harness convention: a user message starting with `!tool <name> {json}` issues a tool call):

```bash
curl -N -D - localhost:8080/v1/messages -H 'content-type: application/json' -H 'X-AI-Subject: human:dev' \
  -d '{"agent_id":"demo","messages":[{"role":"user","content":"!tool ticket.close_batch {\"ids\":[\"T-1\",\"T-2\"]}"}],"stream":true}'
```

The `X-Run-Id` response header is the Run ID; the `card` event in the stream carries `impact_preview` and `call_id`. Then call `/v1/runs/{run_id}/approve` and `/v1/runs/{run_id}/resume` to finish execution.

Other services:

```bash
make run-console   # Console API        :8081
make run-gateway   # Capability gateway :8082
make run-observe   # Observe adapter    :8084
.venv/bin/uvicorn policy_service.main:app --port 8083 --app-dir services/policy/src   # Policy service :8083
```

### Frontend

```bash
pnpm install && pnpm -r dev
```

The Vite dev proxies of `portal-web` and `console-web` point to `:8080` (Runtime) and `:8081` (Console API).

### Local dependencies

```bash
make compose-up                        # infrastructure + observe profile (default)
make compose-up COMPOSE_PROFILES=      # infrastructure only
make compose-down
```

Infrastructure: MySQL, Redis, MinIO, Elasticsearch, Keycloak, Vault, LiteLLM. The `observe` profile (on by default) adds Langfuse (web, worker, Postgres, ClickHouse) and the OTel Collector. If a `.env` file exists at the repo root it is loaded automatically (see `.env.example`).

### Full local stack

With dependencies running, start all application processes and seed demo data:

```bash
.venv/bin/python scripts/dev_stack.py start    # MCP server, gateway, Runtime, Console API, observe adapter, portal, Console UI
.venv/bin/python scripts/dev_seed.py           # demo data (idempotent)
.venv/bin/python scripts/dev_stack.py status
.venv/bin/python scripts/dev_stack.py stop
```

### Key Runtime environment variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./runtime.db` | Use MySQL in production (`mysql+asyncmy://…`) |
| `HARNESS` | `mock` | `mock` / `claude-sdk` / `stdio` |
| `MODEL_GATEWAY_URL` | `http://localhost:4000` | LiteLLM Proxy |
| `CAPABILITY_GATEWAY_URL` | `http://localhost:8082` | Falls back to the local tool table when unreachable |
| `POLICY_URL` | `http://localhost:8083` | Falls back to built-in baseline rules when unreachable (dev only) |
| `TOOL_RESULT_OFFLOAD_TOKENS` | `8000` | Larger tool results are offloaded to the workspace and returned by reference |
| `EGRESS_ALLOWLIST` | empty | Comma-separated egress allowlist |
| `AUTH_REQUIRED` / `JWKS_URL` | `false` / empty | When `true`, JWT signature verification is enforced |
| `TOPOLOGY` / `NODE_ID` | `central` / `node-local` | Node topology: central / hybrid / standalone |

Precedence: database config > environment variables > code defaults; secrets come only from Vault.

### Tests & quality

```bash
make test                        # all Python tests (including snapshot replay)
make lint                        # Ruff + mypy
pnpm -r build                    # build frontend packages
make security                    # Bandit, dependency audit, egress audit, security tests
```

---

## Documentation

| Path | Content |
|---|---|
| [docs/architecture/](docs/architecture/) | Architecture diagrams: [functional](docs/architecture/01-功能架构%28简明%29.png) · [technical](docs/architecture/02-技术架构%28简明%29.png) · [product](docs/architecture/03-总体产品架构%28详图%29.png) · [system components](docs/architecture/04-技术架构%28系统组件详图%29.png) · [core flows](docs/architecture/05-核心链路.png) · [deployment](docs/architecture/06-部署架构.png) · [data](docs/architecture/07-数据架构.png) · [roadmap](docs/architecture/08-建设路线.png) |
| [docs/api/](docs/api/) | OpenAPI specs: [Runtime](docs/api/runtime-openapi.json) · [Capability Gateway](docs/api/gateway-openapi.json) |
| [harness-adapters/stdio/PROTOCOL.md](harness-adapters/stdio/PROTOCOL.md) | stdio Harness protocol |
| [skills/README.md](skills/README.md) | Skill package format and supply-chain rules |
| [deploy/helm/README.md](deploy/helm/README.md) · [deploy/apisix/README.md](deploy/apisix/README.md) | Deployment |

---

## License

[MIT](LICENSE) © HansenUp
