"""测试公共件：测试用 IdP（现场生成 RSA 密钥、签发令牌、以 MockTransport 提供 JWKS）与固定身份常量。

不命名为 conftest 以便测试文件直接导入，且与仓库内其他 tests 目录的模块名不冲突。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "http://idp.test/realms/ai-platform"
AUDIENCE = "mcp-ticket"
JWKS_URL = "http://idp.test/realms/ai-platform/protocol/openid-connect/certs"

ALICE = "00000000-0000-4000-8000-0000000a11ce"  # sales，tenant default
BOB = "00000000-0000-4000-8000-000000000b0b"  # aftersales，tenant default
DAVE = "00000000-0000-4000-8000-0000000da7e0"  # sales，tenant t2


class IdP:
    """测试用 IdP：持有签名密钥，按 kid 签发令牌，并以 MockTransport 提供 JWKS。"""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.down = False
        self.fetches = 0

    def _jwks(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        if self.down:
            return httpx.Response(503)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key())) | {"kid": "k1", "use": "sig", "alg": "RS256"}
        return httpx.Response(200, json={"keys": [jwk]})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._jwks)

    def token(
        self,
        sub: str = ALICE,
        tenant: str | None = "default",
        roles: tuple[str, ...] = (),
        azp: str = "capability-gateway",
        drop: tuple[str, ...] = (),
        **claims: Any,
    ) -> str:
        now = int(time.time())
        body: dict[str, Any] = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": sub,
            "iat": now,
            "exp": now + 300,
            "azp": azp,
            "realm_access": {"roles": list(roles)},
        }
        if tenant is not None:
            body["tenant"] = tenant
        body |= claims
        for k in drop:
            body.pop(k, None)
        return jwt.encode(body, self.key, algorithm="RS256", headers={"kid": "k1"})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
