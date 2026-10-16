"""Runtime 服务的配置项。生效优先级：数据库配置 > 环境变量 > 代码默认值。
（Database-backed overrides are applied by state.config at startup; this module holds env + defaults.
数据库覆盖配置由 state.config 在启动时应用；本模块只承载环境变量与代码默认值这两层。）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    """读取环境变量，不存在时回退到给定默认值。"""
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    """Runtime 节点的运行时配置集合，冻结（frozen）以避免运行期被意外修改。"""

    node_id: str = field(default_factory=lambda: _env("NODE_ID", "node-local"))  # 节点标识
    topology: str = field(default_factory=lambda: _env("TOPOLOGY", "central"))  # central | hybrid | standalone（部署拓扑）
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite+aiosqlite:///./runtime.db"))  # 引擎数据库连接串
    workspace_dir: str = field(default_factory=lambda: _env("WORKSPACE_DIR", "./runtime-workspace"))  # Run 工作区根目录
    harness: str = field(default_factory=lambda: _env("HARNESS", "mock"))  # mock | claude-sdk | stdio（默认 harness）
    model_gateway_url: str = field(default_factory=lambda: _env("MODEL_GATEWAY_URL", "http://localhost:4000"))  # 模型网关地址
    capability_gateway_url: str = field(default_factory=lambda: _env("CAPABILITY_GATEWAY_URL", "http://localhost:8082"))  # 能力网关地址
    policy_url: str = field(default_factory=lambda: _env("POLICY_URL", "http://localhost:8083"))  # 策略服务（Cedar）地址
    langfuse_host: str | None = field(default_factory=lambda: os.environ.get("LANGFUSE_HOST"))  # 可观测性 Langfuse 主机
    tool_result_offload_tokens: int = field(
        default_factory=lambda: int(_env("TOOL_RESULT_OFFLOAD_TOKENS", "8000"))
    )  # 工具结果超过该 token 数即外置到工作区
    egress_allowlist: tuple[str, ...] = field(
        default_factory=lambda: tuple(x for x in _env("EGRESS_ALLOWLIST", "").split(",") if x)
    )  # 出站请求白名单
    auth_required: bool = field(default_factory=lambda: _env("AUTH_REQUIRED", "false").lower() == "true")  # 是否强制鉴权
    jwks_url: str | None = field(default_factory=lambda: os.environ.get("JWKS_URL"))  # JWT 验签所需的 JWKS 地址


settings = Settings()  # 进程级单例，供全服务模块直接导入使用
