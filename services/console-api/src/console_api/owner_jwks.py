from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
import jwt

from console_api.owner_profile import document, identifier


class JwksUnavailable(ValueError):
    pass


class IsolatedJwks:
    def __init__(self, uri: str, transport: httpx.MockTransport, clock: Callable[[], float] = time.monotonic):
        parsed = urlsplit(uri)
        if (
            not 1 <= len(uri) <= 2048
            or re.search(r"[\s\x00-\x1f\x7f]", uri)
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not isinstance(transport, httpx.MockTransport)
        ):
            raise ValueError("Fixed HTTPS URI and substitute transport required")
        self.uri = uri
        self.transport = transport
        self.clock = clock
        self._keys: dict[str, str] = {}
        self._expires = 0.0
        self._attempted = float("-inf")
        self._generation = 0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._generation += 1
        self._keys = {}
        self._expires = 0.0

    async def resolve(self, kid: str) -> str:
        identifier(kid)
        generation = self._generation
        async with self._lock:
            if generation != self._generation:
                raise JwksUnavailable("JWKS configuration changed")
            stamp = self.clock()
            if stamp < self._expires and kid in self._keys:
                return self._keys[kid]
            if stamp - self._attempted < 30:
                raise JwksUnavailable("JWKS refresh limited")
            self._attempted = stamp
            try:
                async with asyncio.timeout(5):
                    async with httpx.AsyncClient(transport=self.transport, timeout=5, trust_env=False, follow_redirects=False) as client:
                        async with client.stream("GET", self.uri) as response:
                            if response.status_code != 200:
                                raise ValueError
                            content = bytearray()
                            async for chunk in response.aiter_bytes():
                                content.extend(chunk)
                                if len(content) > 65536:
                                    raise ValueError
                body = document(content.decode("utf-8"), {"keys"})
                if not isinstance(body["keys"], list) or not 1 <= len(body["keys"]) <= 32:
                    raise ValueError
                keys = {}
                for entry in body["keys"]:
                    key = document(json.dumps(entry), {"kty", "kid", "n", "e"}, frozenset({"alg", "use", "key_ops"}))
                    name = identifier(key["kid"])
                    if (
                        name in keys
                        or key["kty"] != "RSA"
                        or key.get("alg", "RS256") != "RS256"
                        or key.get("use", "sig") != "sig"
                        or key.get("key_ops", ["verify"]) != ["verify"]
                        or jwt.PyJWK.from_dict(key, algorithm="RS256").key.key_size < 2048
                    ):
                        raise ValueError
                    keys[name] = json.dumps(key)
                if generation != self._generation:
                    raise ValueError
                self._keys = keys
                self._expires = self.clock() + 300
            except (ValueError, TypeError, KeyError, UnicodeError, jwt.PyJWTError, httpx.HTTPError, TimeoutError) as exc:
                raise JwksUnavailable("JWKS unavailable") from exc
            if kid not in self._keys:
                raise JwksUnavailable("Unknown JWKS kid")
            return self._keys[kid]
