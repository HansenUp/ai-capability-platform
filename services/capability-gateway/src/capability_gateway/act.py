"""act 断言（S8.4 / D129，契约 2）：网关为每次转发签发 ``X-AI-Act-Assertion``（ES256 JWT），表达嵌套委托链。

声明：iss=GATEWAY_ACT_ISSUER（capability-gateway）、aud=连接器名、sub=终端用户、
``act={sub:"agent:<id>@<ver>", act:{sub:"client:<调用方 azp>"}}``、run_id、call_id、
``obo_hash``=sha256(OBO 令牌 UTF-8) 的小写十六进制（开发态无 OBO 时省略）、iat、exp（≤ iat+60）、jti。
MCP 端校验 OBO 与本断言的签名、两者 sub 一致、obo_hash 匹配后才采信 act；公钥见 ``GET /.well-known/jwks.json``。

签名密钥：``GATEWAY_ACT_SIGNING_KEY_FILE``（PEM，EC P-256 私钥）。开发态未配置时首次使用生成进程内临时密钥并告警
（重启即变，MCP 端需重新拉取 JWKS）；GATEWAY_ENV=prod 未配置时网关拒绝启动。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from capability_gateway import config

log = logging.getLogger("capability_gateway.act")
HEADER = "X-AI-Act-Assertion"

_lock = threading.Lock()
_key: ec.EllipticCurvePrivateKey | None = None
_kid: str | None = None


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()


def _public_jwk(key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    nums = key.public_key().public_numbers()
    return {"kty": "EC", "crv": "P-256", "x": _b64(nums.x), "y": _b64(nums.y)}


def _thumbprint(jwk: dict[str, str]) -> str:
    """RFC 7638 JWK 指纹作为 kid。"""
    canon = json.dumps({k: jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(hashlib.sha256(canon.encode()).digest()).rstrip(b"=").decode()


def load_key(path: str | None = None) -> ec.EllipticCurvePrivateKey:
    """加载（或在开发态生成）签名密钥；进程内只做一次。"""
    global _key, _kid
    with _lock:
        if _key is not None:
            return _key
        path = path or config.ACT_KEY_FILE
        if path:
            loaded = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
            if not isinstance(loaded, ec.EllipticCurvePrivateKey) or loaded.curve.name != "secp256r1":
                raise RuntimeError("GATEWAY_ACT_SIGNING_KEY_FILE must be an EC P-256 private key (PEM)")
            key = loaded
        else:
            if config.GATEWAY_ENV == "prod":
                raise RuntimeError("GATEWAY_ENV=prod requires GATEWAY_ACT_SIGNING_KEY_FILE")
            log.warning("GATEWAY_ACT_SIGNING_KEY_FILE 未配置：生成进程内临时 act 签名密钥（仅限开发，重启即变）")
            key = ec.generate_private_key(ec.SECP256R1())
        _key, _kid = key, _thumbprint(_public_jwk(key))
        return key


def reset_for_tests(key: ec.EllipticCurvePrivateKey | None = None) -> None:
    global _key, _kid
    with _lock:
        _key = key
        _kid = _thumbprint(_public_jwk(key)) if key is not None else None


def jwks() -> dict[str, Any]:
    key = load_key()
    return {"keys": [_public_jwk(key) | {"use": "sig", "alg": "ES256", "kid": _kid}]}


def obo_hash(obo_token: str) -> str:
    return hashlib.sha256(obo_token.encode("utf-8")).hexdigest()


def sign(
    *,
    sub: str,
    audience: str,
    agent_id: str,
    agent_version: str,
    caller: str,
    run_id: str | None,
    call_id: str | None,
    obo_token: str | None,
    now: float | None = None,
) -> str:
    """签发一次转发的 act 断言（有效期 GATEWAY_ACT_TTL_S，上限 60 秒）。"""
    key = load_key()
    iat = int(now if now is not None else time.time())
    claims: dict[str, Any] = {
        "iss": config.ACT_ISSUER,
        "aud": audience,
        "sub": sub,
        "iat": iat,
        "exp": iat + config.ACT_TTL_S,
        "jti": uuid.uuid4().hex,
        "act": {"sub": f"agent:{agent_id}@{agent_version}", "act": {"sub": f"client:{caller}"}},
        "run_id": run_id,
        "call_id": call_id,
    }
    if obo_token:
        claims["obo_hash"] = obo_hash(obo_token)
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": _kid, "typ": "JWT"})
