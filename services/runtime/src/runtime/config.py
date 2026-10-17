"""Runtime 服务的配置项。生效优先级：数据库配置 > 环境变量 > 代码默认值。
（Database-backed overrides are applied by state.config at startup; this module holds env + defaults.
数据库覆盖配置由 state.config 在启动时应用；本模块只承载环境变量与代码默认值这两层。）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from runtime import secret_store


def _env(name: str, default: str) -> str:
    """读取环境变量，不存在时回退到给定默认值。"""
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    """Runtime 节点的运行时配置集合，冻结（frozen）以避免运行期被意外修改。"""

    node_id: str = field(default_factory=lambda: _env("NODE_ID", "node-local"))  # 节点标识
    topology: str = field(default_factory=lambda: _env("TOPOLOGY", "central"))  # central | hybrid | standalone（部署拓扑）
    runtime_env: str = field(default_factory=lambda: _env("RUNTIME_ENV", "dev"))  # dev | prod；prod 启动时做安全基线校验
    # 引擎数据库连接串（含口令，属密钥项：生产经 Vault / 挂载文件注入，见 runtime.secret_store）
    database_url: str = field(
        default_factory=lambda: secret_store.get("DATABASE_URL", "sqlite+aiosqlite:///./runtime.db") or "", repr=False
    )
    workspace_dir: str = field(default_factory=lambda: _env("WORKSPACE_DIR", "./runtime-workspace"))  # Run 工作区根目录
    harness: str = field(default_factory=lambda: _env("HARNESS", "mock"))  # mock | claude-sdk | stdio（默认 harness）
    model_gateway_url: str = field(default_factory=lambda: _env("MODEL_GATEWAY_URL", "http://localhost:4000"))  # 模型网关地址
    # 调用模型网关的 Key：本机为 LiteLLM 开发主密钥，生产环境使用按 Agent 签发的虚拟 Key
    model_gateway_api_key: str = field(default_factory=lambda: secret_store.get("MODEL_GATEWAY_API_KEY", DEV_GATEWAY_KEY) or "", repr=False)
    # claude-sdk Harness 调网关的凭据；未单独配置时与 model_gateway_api_key 相同
    claude_sdk_auth_token: str | None = field(default_factory=lambda: secret_store.get("CLAUDE_SDK_AUTH_TOKEN"), repr=False)
    llm_timeout_s: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_S", "120")))  # 单次模型请求超时（秒）
    llm_max_retries: int = field(default_factory=lambda: int(_env("LLM_MAX_RETRIES", "2")))  # 可重试错误（429/5xx/网络）的最大重试次数
    # OTLP/HTTP 端点（基址），未设置则只装 TracerProvider、不导出 Trace
    otel_exporter_endpoint: str | None = field(default_factory=lambda: os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
    # 数据分级最小矩阵（S4.9）：Run 带 confidential 污点后，只有这些模型别名可以继续接收上下文（如私有化部署的模型）；
    # 空表示一律阻断。逗号分隔。
    confidential_model_aliases: tuple[str, ...] = field(
        default_factory=lambda: tuple(x for x in _env("CONFIDENTIAL_MODEL_ALIASES", "").split(",") if x)
    )
    # confidential 污点下一律拒绝的检索 / 外部查询类工具（按名称前缀）：查询语句本身会把机密上下文带出去
    confidential_blocked_tool_prefixes: tuple[str, ...] = field(
        default_factory=lambda: tuple(x for x in _env("CONFIDENTIAL_BLOCKED_TOOL_PREFIXES", "knowledge.,web.,search.").split(",") if x)
    )
    # 发布时校验模型别名在模型网关上存在（受控模型出口，S4.8）；mock harness 不校验
    publish_check_model_alias: bool = field(default_factory=lambda: _env("PUBLISH_CHECK_MODEL_ALIAS", "true").lower() == "true")
    # 工具执行方式：local = 进程内开发执行器（测试 / 无网关时）；gateway = 经能力网关分发到 MCP Server（S4.3）
    tool_dispatch: str = field(default_factory=lambda: _env("TOOL_DISPATCH", "local"))
    capability_gateway_url: str = field(default_factory=lambda: _env("CAPABILITY_GATEWAY_URL", "http://localhost:8082"))  # 能力网关地址
    policy_url: str = field(default_factory=lambda: _env("POLICY_URL", "http://localhost:8083"))  # 策略服务（Cedar）地址
    # 观测适配器（S3.5 反馈写 Langfuse score 的落点）；只在内网可达
    observe_adapter_url: str = field(default_factory=lambda: _env("OBSERVE_ADAPTER_URL", "http://localhost:8084"))
    langfuse_host: str | None = field(default_factory=lambda: os.environ.get("LANGFUSE_HOST"))  # 可观测性 Langfuse 主机
    tool_result_offload_tokens: int = field(
        default_factory=lambda: int(_env("TOOL_RESULT_OFFLOAD_TOKENS", "8000"))
    )  # 工具结果超过该 token 数即外置到工作区
    egress_allowlist: tuple[str, ...] = field(
        default_factory=lambda: tuple(x for x in _env("EGRESS_ALLOWLIST", "").split(",") if x)
    )  # 出站请求白名单
    auth_required: bool = field(default_factory=lambda: _env("AUTH_REQUIRED", "false").lower() == "true")  # 是否强制鉴权
    jwks_url: str | None = field(default_factory=lambda: os.environ.get("JWKS_URL"))  # JWT 验签所需的 JWKS 地址
    jwt_issuer: str | None = field(default_factory=lambda: os.environ.get("JWT_ISSUER"))  # 期望的签发者（iss）
    jwt_audience: str | None = field(default_factory=lambda: os.environ.get("JWT_AUDIENCE"))  # 期望的受众（aud），如 ai-runtime
    jwt_tenant_claim: str = field(default_factory=lambda: _env("JWT_TENANT_CLAIM", "tenant"))  # 携带租户的声明名
    jwt_leeway_s: int = field(default_factory=lambda: int(_env("JWT_LEEWAY_S", "30")))  # exp / nbf / iat 校验允许的时钟偏差（秒）
    jwks_cache_ttl_s: float = field(default_factory=lambda: float(_env("JWKS_CACHE_TTL_S", "300")))  # JWKS 缓存有效期（秒）


DEV_GATEWAY_KEY = "sk-dev-master"  # noqa: S105 —— 本机 compose LiteLLM 的公开开发主密钥，生产禁止使用


def production_problems(s: Settings) -> list[str]:
    """RUNTIME_ENV=prod 时的安全基线：返回不满足的条目（空列表表示通过）。

    - 必须强制 JWT 验签，且 JWKS 地址、签发者、受众齐全；
    - 不得使用开发网关主密钥；数据库连接必须经 Vault / 挂载文件注入且不是 SQLite；
    - 密钥项不得来自明文环境变量（只能经 Vault 或挂载文件注入）。
    """
    problems: list[str] = []
    if not s.auth_required:
        problems.append("AUTH_REQUIRED 必须为 true")
    for name, value in (("JWKS_URL", s.jwks_url), ("JWT_ISSUER", s.jwt_issuer), ("JWT_AUDIENCE", s.jwt_audience)):
        if not value:
            problems.append(f"缺少 {name}")
    if not s.model_gateway_api_key or s.model_gateway_api_key == DEV_GATEWAY_KEY:
        problems.append("MODEL_GATEWAY_API_KEY 未配置或仍为开发主密钥")
    if secret_store.source("DATABASE_URL") not in ("vault", "file") or s.database_url.startswith("sqlite"):
        problems.append("DATABASE_URL 未经 Vault / DATABASE_URL_FILE 注入，或仍指向 SQLite")
    if s.tool_dispatch != "gateway":
        problems.append("TOOL_DISPATCH 必须为 gateway（进程内开发执行器只返回模拟结果）")
    for name in secret_store.SECRET_NAMES:
        if secret_store.source(name) == "env":
            problems.append(f"{name} 来自明文环境变量，生产须经 Vault 或 {name}_FILE 注入")
    return problems


def validate_for_production(s: Settings) -> None:
    """RUNTIME_ENV=prod 且安全基线不满足时拒绝启动；其他环境不做检查。"""
    if s.runtime_env != "prod":
        return
    problems = production_problems(s)
    if problems:
        raise RuntimeError("RUNTIME_ENV=prod 安全基线未满足：" + "；".join(problems))


settings = Settings()  # 进程级单例，供全服务模块直接导入使用
