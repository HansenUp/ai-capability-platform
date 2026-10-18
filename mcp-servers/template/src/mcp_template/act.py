"""能力网关签发的 act 断言（请求头 ``X-AI-Act-Assertion``）校验，方案 S8.4。

断言是网关以 ES256 签名的 JWT，公钥经网关 JWKS（``/.well-known/jwks.json``）获取并缓存。声明：

- ``iss`` = 能力网关（默认 ``capability-gateway``）；``aud`` = 本连接器；``sub`` = 终端用户（必须与 OBO 令牌 sub 一致）；
- ``act`` = ``{"sub": "agent:<id>@<ver>", "act": {"sub": "client:ai-runtime-svc"}}``（嵌套委托链，最外层为执行 Agent）；
- ``obo_hash`` = 同一请求 OBO 令牌的 SHA-256（十六进制，或无填充 base64url），把断言绑定到这一枚令牌；
- ``exp`` − ``iat`` ≤ 60 s；``jti`` 防重放（记到断言过期为止）：配置 ``<P>_ACT_REPLAY_REDIS_URL`` 时用 Redis 共享存储
  （``SET NX`` + 过期时间，多副本之间同样防重放；Redis 不可用 503，失败关闭，需安装可选依赖 ``redis``），未配置时为进程内缓存
  （单副本）；可选 ``run_id`` / ``call_id``。

判定（Bearer 路径，见 :func:`resolve_act`）：
- 带断言：逐项校验，通过后采信断言里的 act 链；断言本身无效（签名 / 算法 / 签发者 / 受众 / 过期 / 寿命过长 / 重放 / 缺字段）→ 401，
  断言有效但不属于本请求（sub 不一致、obo_hash 不匹配）→ 403，网关 JWKS 不可达 → 503，未配置网关 JWKS → 500；
- 不带断言：生产态 401；开发态回退到裸 ``X-AI-Act`` 头（仅当 OBO 的 azp 属于可信客户端时采信）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import HTTPException
from jwt.types import Options

from mcp_template.jwks import JWKSUnavailable, jwks_cache

ACT_ALGORITHM = "ES256"  # 网关只用 ES256 签断言；其他算法一律拒绝
DEFAULT_ACT_ISSUER = "capability-gateway"
MAX_TTL_S = 60  # 断言最长寿命（exp - iat）
LEEWAY_S = 5  # 时钟偏差容忍
MAX_ACT_DEPTH = 5  # act 嵌套链最多解析的层数
REPLAY_CACHE_MAX = 100_000  # jti 缓存上限；清理过期项后仍满则拒绝（失败关闭）
REPLAY_KEY_PREFIX = "ai:act-jti:"  # Redis 中 jti 记录的键前缀


def unauthorized(detail: str) -> HTTPException:
    """401，附 RFC 6750 的 WWW-Authenticate 头。"""
    return HTTPException(401, detail, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


@dataclass(frozen=True)
class ActConfig:
    """act 断言校验配置。"""

    jwks_url: str | None  # 网关 JWKS 地址，如 http://127.0.0.1:8082/.well-known/jwks.json
    issuer: str = DEFAULT_ACT_ISSUER
    audience: str = ""  # 本连接器的受众名，与 OBO 令牌 aud 相同
    replay_redis_url: str | None = None  # jti 防重放的共享存储（Redis）；None 为进程内缓存


@dataclass(frozen=True)
class ActContext:
    """确定下来的执行方：act 为最外层执行 Agent（如 ``agent:ticket-summary@1``），chain 为完整委托链。"""

    act: str | None = None
    chain: tuple[str, ...] = ()
    run_id: str | None = None
    call_id: str | None = None
    verified: bool = False  # 是否来自已验签的断言（False 表示开发态裸头或无 act）


class ReplayCache:
    """jti 防重放：按（受众, jti）记到断言过期为止。线程安全（同步端点在线程池中执行）。"""

    def __init__(self, max_entries: int = REPLAY_CACHE_MAX) -> None:
        self._max = max_entries
        self._seen: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def check_and_add(self, audience: str, jti: str, exp: float) -> bool:
        """首次出现返回 True 并记下；未过期的重复 jti 返回 False。缓存满（清理后仍满）时抛 HTTPException 503。"""
        now = time.time()
        key = (audience, jti)
        with self._lock:
            seen_exp = self._seen.get(key)
            if seen_exp is not None and seen_exp + LEEWAY_S >= now:
                return False
            if len(self._seen) >= self._max:
                self._seen = {k: e for k, e in self._seen.items() if e + LEEWAY_S >= now}
                if len(self._seen) >= self._max:
                    raise HTTPException(503, "act assertion replay cache is full")
            self._seen[key] = exp
            return True

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


replay_cache = ReplayCache()


def _default_redis_factory(url: str) -> Any:
    import redis.asyncio as aioredis  # 可选依赖：只在配置了 <P>_ACT_REPLAY_REDIS_URL 时需要

    return aioredis.from_url(url, socket_timeout=2, socket_connect_timeout=2)


_redis_factory: Callable[[str], Any] = _default_redis_factory


class RedisReplayCache:
    """多副本共享的 jti 防重放：``SET <前缀><受众>:<jti> NX PX <到断言过期 + 容差>``；键已存在即重放。
    Redis 客户端按事件循环缓存（异步连接不能跨循环复用）。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self._clients: dict[int, Any] = {}

    def _client(self) -> Any:
        loop_id = id(asyncio.get_running_loop())
        client = self._clients.get(loop_id)
        if client is None:
            client = self._clients[loop_id] = _redis_factory(self.url)
        return client

    async def check_and_add(self, audience: str, jti: str, exp: float) -> bool:
        """首次出现返回 True 并记下；重复返回 False；Redis 不可用抛 HTTPException 503（失败关闭）。"""
        ttl_ms = max(1000, int((exp + LEEWAY_S - time.time()) * 1000))
        try:
            fresh = await self._client().set(f"{REPLAY_KEY_PREFIX}{audience}:{jti}", "1", nx=True, px=ttl_ms)
        except Exception as e:  # noqa: BLE001 —— redis 为可选依赖，其异常类型不在此导入；任何存储失败一律失败关闭
            raise HTTPException(503, f"act assertion replay store unavailable: {type(e).__name__}") from None
        return bool(fresh)


_redis_caches: dict[str, RedisReplayCache] = {}


def set_redis_factory_for_tests(factory: Callable[[str], Any] | None) -> None:
    """测试注入：Redis 客户端工厂（如 fakeredis）；None 恢复默认。清空已建的共享缓存。"""
    global _redis_factory
    _redis_factory = factory or _default_redis_factory
    _redis_caches.clear()


async def _first_use(cfg: ActConfig, jti: str, exp: float) -> bool:
    if cfg.replay_redis_url:
        cache = _redis_caches.get(cfg.replay_redis_url)
        if cache is None:
            cache = _redis_caches[cfg.replay_redis_url] = RedisReplayCache(cfg.replay_redis_url)
        return await cache.check_and_add(cfg.audience, jti, exp)
    return replay_cache.check_and_add(cfg.audience, jti, exp)


def obo_hash(token: str) -> str:
    """OBO 令牌的绑定哈希（网关签发时使用的规范形式：SHA-256 十六进制小写）。"""
    return hashlib.sha256(token.encode()).hexdigest()


def _obo_hash_matches(claimed: Any, token: str) -> bool:
    """obo_hash 接受十六进制或无填充 base64url 两种编码，常量时间比较。"""
    if not isinstance(claimed, str) or not claimed:
        return False
    digest = hashlib.sha256(token.encode()).digest()
    candidates = (digest.hex(), base64.urlsafe_b64encode(digest).rstrip(b"=").decode())
    return any(hmac.compare_digest(claimed.lower() if len(claimed) == 64 else claimed, c) for c in candidates)


def _act_chain(claim: Any) -> tuple[str, ...]:
    """把嵌套 act 声明展开为委托链（外层在前）。"""
    chain: list[str] = []
    node = claim
    while isinstance(node, dict) and len(chain) < MAX_ACT_DEPTH:
        sub = node.get("sub")
        if not isinstance(sub, str) or not sub:
            break
        chain.append(sub)
        node = node.get("act")
    return tuple(chain)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


async def verify_act_assertion(assertion: str, *, obo_token: str, obo_sub: str, cfg: ActConfig) -> ActContext:
    """校验 act 断言并返回执行方上下文；错误映射见模块文档。"""
    if not cfg.jwks_url:
        raise HTTPException(500, "act assertion JWKS (gateway /.well-known/jwks.json) is not configured")
    try:
        header = jwt.get_unverified_header(assertion)
    except jwt.PyJWTError:
        raise unauthorized("malformed act assertion") from None
    alg, kid = header.get("alg"), header.get("kid")
    if alg != ACT_ALGORITHM:
        raise unauthorized(f"unsupported act assertion algorithm: {alg}")
    if not isinstance(kid, str) or not kid:
        raise unauthorized("act assertion header has no kid")
    try:
        key = await jwks_cache(cfg.jwks_url).get(kid)
    except JWKSUnavailable:
        raise HTTPException(503, "capability gateway JWKS unavailable") from None
    if key is None:
        raise unauthorized("unknown act assertion signing key")
    if key.algorithm_name and key.algorithm_name != ACT_ALGORITHM:
        raise unauthorized("act assertion algorithm does not match signing key")
    options: Options = {"require": ["exp", "iat", "sub", "iss", "aud", "jti"]}
    try:
        claims: dict[str, Any] = jwt.decode(
            assertion, key.key, algorithms=[ACT_ALGORITHM], issuer=cfg.issuer, audience=cfg.audience, options=options, leeway=LEEWAY_S
        )
    except jwt.ExpiredSignatureError:
        raise unauthorized("act assertion expired") from None
    except jwt.PyJWTError as e:
        raise unauthorized(f"invalid act assertion: {type(e).__name__}") from None
    exp, iat = claims["exp"], claims["iat"]
    if not isinstance(exp, int | float) or not isinstance(iat, int | float):
        raise unauthorized("invalid act assertion: exp / iat must be numeric")
    if exp - iat > MAX_TTL_S or exp - time.time() > MAX_TTL_S + LEEWAY_S:
        raise unauthorized(f"act assertion lifetime exceeds {MAX_TTL_S}s")
    chain = _act_chain(claims.get("act"))
    if not chain or not chain[0].startswith("agent:"):
        raise unauthorized("act assertion has no agent actor")
    jti = claims["jti"]
    if not isinstance(jti, str) or not jti:
        raise unauthorized("invalid act assertion: jti")
    if claims["sub"] != obo_sub:
        raise HTTPException(403, "act assertion subject does not match the OBO token")
    if not _obo_hash_matches(claims.get("obo_hash"), obo_token):
        raise HTTPException(403, "act assertion is not bound to this OBO token")
    # 重放检查放在最后：只有完整通过校验的断言才占用 jti
    if not await _first_use(cfg, jti, float(exp)):
        raise unauthorized("act assertion replayed")
    return ActContext(
        act=chain[0],
        chain=chain,
        run_id=_optional_str(claims.get("run_id")),
        call_id=_optional_str(claims.get("call_id")),
        verified=True,
    )


async def resolve_act(
    *,
    assertion: str | None,
    bare_act: str | None,
    obo_token: str,
    obo_sub: str,
    azp: str | None,
    trusted_azp: frozenset[str],
    dev_auth: bool,
    cfg: ActConfig,
) -> ActContext:
    """Bearer（OBO 令牌）路径下确定执行方。开发头（X-AI-Dev-Sub）路径不经此函数，直接采信裸 X-AI-Act。"""
    if assertion and assertion.strip():
        return await verify_act_assertion(assertion.strip(), obo_token=obo_token, obo_sub=obo_sub, cfg=cfg)
    if not dev_auth:
        raise unauthorized("act assertion (X-AI-Act-Assertion) required")
    act = bare_act.strip() if bare_act and azp in trusted_azp else None
    return ActContext(act=act or None, chain=(act,) if act else ())
