"""能力网关配置：全部来自环境变量，进程启动时读取一次。

各模块在调用时经 ``config.<名字>`` 取值（而不是 ``from config import X``），测试可以直接 monkeypatch 本模块。

| 变量 | 说明 | 默认 |
|---|---|---|
| ``GATEWAY_ENV`` | dev / prod；prod 下缺关键配置拒绝启动 | dev |
| ``POLICY_URL`` | 策略服务地址（``/authz/check-batch``，契约 1）；不可达一律 deny | http://localhost:8083 |
| ``POLICY_SERVICE_TOKEN`` | 调用策略服务的共享服务令牌（``X-Policy-Service-Token``；与策略服务同值，prod 必填） | 无（开发态） |
| ``KEYCLOAK_TOKEN_URL`` | Keycloak 令牌端点；设置即"已配置 Keycloak"：OBO 换取 + 服务间鉴权 + 用户令牌验签 | 无（开发态） |
| ``GATEWAY_JWKS_URL`` / ``GATEWAY_JWT_ISSUER`` | 验签用 JWKS 与签发者；缺省由 KEYCLOAK_TOKEN_URL 推导 | 推导 |
| ``GATEWAY_SERVICE_AUDIENCE`` | 服务令牌（ai-runtime-svc）的 aud | GATEWAY_CLIENT_ID |
| ``GATEWAY_USER_AUDIENCE`` | 终端用户令牌的 aud（逗号分隔；未设置时不校验 aud，prod 必填） | 无 |
| ``GATEWAY_TRUSTED_CALLERS`` | 允许调用 /invoke 等接口的服务令牌 azp（逗号分隔） | ai-runtime-svc |
| ``CATALOG_BACKEND`` | memory（单测 / 本机）或 mysql | memory |
| ``GATEWAY_DATABASE_URL`` | 目录库连接串（独立库，如 mysql+asyncmy://root:root@localhost:3316/ai_gateway） | 无 |
| ``GATEWAY_MIGRATE_ON_START`` | mysql 后端启动时执行 Alembic 迁移 | true |
| ``ES_URL`` | Elasticsearch 地址（8.x）；未设置时在进程内做混合检索 | 无 |
| ``ES_INDEX_ALIAS`` | 检索索引别名（索引名 ``<别名>-v1-<provider><dim>``）；同一 ES 集群上的多套环境用不同别名隔离 | cg-capabilities |
| ``EMBED_PROVIDER`` | hash / fastembed / litellm（见 embed.py） | hash |
| ``GATEWAY_ACT_SIGNING_KEY_FILE`` | act 断言 ES256 私钥（PEM）；开发态缺省时启动生成临时密钥并告警 | 无 |
"""

from __future__ import annotations

import os

import httpx


def _csv(value: str) -> frozenset[str]:
    return frozenset(p.strip() for p in value.split(",") if p.strip())


def _derive_issuer(token_url: str | None) -> str | None:
    """.../realms/<r>/protocol/openid-connect/token → .../realms/<r>。"""
    if not token_url or "/protocol/openid-connect/" not in token_url:
        return None
    return token_url.split("/protocol/openid-connect/", 1)[0]


GATEWAY_ENV = os.environ.get("GATEWAY_ENV", "dev")  # dev | prod
POLICY_URL = os.environ.get("POLICY_URL", "http://localhost:8083")
POLICY_TIMEOUT_S = float(os.environ.get("POLICY_TIMEOUT_S", "2"))
POLICY_SERVICE_TOKEN = os.environ.get("POLICY_SERVICE_TOKEN", "")  # 策略服务的服务间共享令牌（X-Policy-Service-Token）
KEYCLOAK_TOKEN_URL = os.environ.get("KEYCLOAK_TOKEN_URL") or None  # 例：https://idp/realms/ai/protocol/openid-connect/token
GATEWAY_CLIENT_ID = os.environ.get("GATEWAY_CLIENT_ID", "capability-gateway")  # 网关在 Keycloak 中的 Client
GATEWAY_CLIENT_SECRET = os.environ.get("GATEWAY_CLIENT_SECRET", "")
GATEWAY_ADMIN_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")  # 登记类接口的管理令牌（X-Gateway-Admin-Token）
GATEWAY_AUTHZ_TOKEN = os.environ.get("GATEWAY_AUTHZ_TOKEN", "")  # 第三方数据面回调 /authz/mcp-check 的共享密钥
JWT_ISSUER = os.environ.get("GATEWAY_JWT_ISSUER") or _derive_issuer(KEYCLOAK_TOKEN_URL)
JWKS_URL = os.environ.get("GATEWAY_JWKS_URL") or (f"{JWT_ISSUER}/protocol/openid-connect/certs" if JWT_ISSUER else None)
SERVICE_AUDIENCE = os.environ.get("GATEWAY_SERVICE_AUDIENCE") or GATEWAY_CLIENT_ID
USER_AUDIENCE = _csv(os.environ.get("GATEWAY_USER_AUDIENCE", ""))
TRUSTED_CALLERS = _csv(os.environ.get("GATEWAY_TRUSTED_CALLERS", "ai-runtime-svc"))
JWKS_TTL_S = float(os.environ.get("GATEWAY_JWKS_TTL_S", "300"))

CATALOG_BACKEND = os.environ.get("CATALOG_BACKEND", "memory")  # memory | mysql
DATABASE_URL = os.environ.get("GATEWAY_DATABASE_URL") or None
MIGRATE_ON_START = os.environ.get("GATEWAY_MIGRATE_ON_START", "true").strip().lower() != "false"
ES_URL = (os.environ.get("ES_URL") or "").rstrip("/") or None
ES_TIMEOUT_S = float(os.environ.get("ES_TIMEOUT_S", "2"))
ES_ALIAS = os.environ.get("ES_INDEX_ALIAS") or "cg-capabilities"
INDEXER_INTERVAL_S = float(os.environ.get("GATEWAY_INDEXER_INTERVAL_S", "1"))

ACT_KEY_FILE = os.environ.get("GATEWAY_ACT_SIGNING_KEY_FILE") or None
ACT_ISSUER = os.environ.get("GATEWAY_ACT_ISSUER", "capability-gateway")
ACT_TTL_S = min(60, int(os.environ.get("GATEWAY_ACT_TTL_S", "60")))  # act 断言有效期，上限 60 秒

CORE_LIMIT = int(os.environ.get("CORE_TOOL_LIMIT", "20"))  # 核心工具预载上限
INVOKE_TIMEOUT_S = float(os.environ.get("INVOKE_TIMEOUT_S", "30"))
PROBE_INTERVAL_S = float(os.environ.get("GATEWAY_PROBE_INTERVAL_S", "30"))  # 连接器探活周期
PROBE_FAIL_THRESHOLD = int(os.environ.get("GATEWAY_PROBE_FAIL_THRESHOLD", "3"))  # 连续失败次数达到即 down
PROBE_TIMEOUT_S = float(os.environ.get("GATEWAY_PROBE_TIMEOUT_S", "5"))

# 测试注入：网关发出的全部 HTTP 请求（Keycloak、JWKS、策略服务、连接器、ES、模型网关）经该 transport
transport: httpx.AsyncBaseTransport | None = None


def keycloak_enabled() -> bool:
    """已配置 Keycloak：OBO 换取、服务间鉴权与用户令牌验签全部生效；否则为开发态（dev 头）。"""
    return bool(KEYCLOAK_TOKEN_URL)


def prod_problems() -> list[str]:
    """GATEWAY_ENV=prod 时缺失的关键配置（为空表示可以启动）。"""
    if GATEWAY_ENV != "prod":
        return []
    need = {
        "KEYCLOAK_TOKEN_URL": KEYCLOAK_TOKEN_URL,
        "GATEWAY_ADMIN_TOKEN": GATEWAY_ADMIN_TOKEN,
        "GATEWAY_AUTHZ_TOKEN": GATEWAY_AUTHZ_TOKEN,
        "POLICY_SERVICE_TOKEN": POLICY_SERVICE_TOKEN,
        "GATEWAY_TRUSTED_CALLERS": os.environ.get("GATEWAY_TRUSTED_CALLERS"),
        "GATEWAY_USER_AUDIENCE": os.environ.get("GATEWAY_USER_AUDIENCE"),
        "GATEWAY_ACT_SIGNING_KEY_FILE": ACT_KEY_FILE,
        "GATEWAY_DATABASE_URL": DATABASE_URL,
        "GATEWAY_JWKS_URL / GATEWAY_JWT_ISSUER": JWKS_URL and JWT_ISSUER,
    }
    out = [k for k, v in need.items() if not v]
    if CATALOG_BACKEND != "mysql":
        out.append("CATALOG_BACKEND=mysql")
    return out


def check_prod() -> None:
    """生产必须：Keycloak（OBO + 服务间鉴权）、管理令牌、判定端点密钥、act 签名密钥、MySQL 目录。"""
    problems = prod_problems()
    if problems:
        raise RuntimeError(f"GATEWAY_ENV=prod requires: {', '.join(problems)}")
