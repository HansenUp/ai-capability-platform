from __future__ import annotations

import asyncio
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from console_api.owner_jwks import IsolatedJwks, JwksUnavailable


@pytest.fixture
def keys():
    result = []
    for kid in ("old", "new"):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        result.append(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())) | {"kid": kid})
    return result


@pytest.fixture
def source(keys):
    state = {"time": 0.0, "keys": [keys[0]], "calls": [], "mode": "ok"}

    async def handler(request):
        state["calls"].append(request)
        assert request.method == "GET" and str(request.url) == "https://identity.test/certs"
        assert "authorization" not in request.headers
        assert request.extensions["timeout"] == {"connect": 5, "read": 5, "write": 5, "pool": 5}
        if state["mode"] == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if state["mode"] == "redirect":
            return httpx.Response(302, headers={"Location": "https://forbidden.test/certs"})
        if state["mode"] == "large":
            return httpx.Response(200, content=b" " * 65537)
        if state["mode"] == "duplicate-json":
            return httpx.Response(200, content=b'{"keys": [], "keys": []}')
        return httpx.Response(200, json={"keys": state["keys"]})

    cache = IsolatedJwks("https://identity.test/certs", httpx.MockTransport(handler), lambda: state["time"])
    return cache, state


async def test_jwks_rotation_ttl_and_removed_key(keys, source):
    cache, state = source
    assert json.loads(await cache.resolve("old"))["kid"] == "old"
    state["keys"] = [keys[1]]
    state["time"] = 299
    await cache.resolve("old")
    assert len(state["calls"]) == 1
    state["time"] = 300
    assert json.loads(await cache.resolve("new"))["kid"] == "new"
    with pytest.raises(JwksUnavailable):
        await cache.resolve("old")
    assert len(state["calls"]) == 2


async def test_jwks_unknown_kid_cooldown_and_rotation(keys, source):
    cache, state = source
    await cache.resolve("old")
    state["keys"] = keys
    state["time"] = 29
    with pytest.raises(JwksUnavailable):
        await cache.resolve("new")
    assert len(state["calls"]) == 1
    state["time"] = 30
    await cache.resolve("new")
    assert len(state["calls"]) == 2
    for kid in ("missing-a", "missing-b"):
        with pytest.raises(JwksUnavailable):
            await cache.resolve(kid)
    assert len(state["calls"]) == 2


@pytest.mark.parametrize("mode", ["timeout", "redirect", "large", "duplicate-json"])
async def test_jwks_expired_cache_never_falls_back(source, mode):
    cache, state = source
    await cache.resolve("old")
    state.update(time=300, mode=mode)
    with pytest.raises(JwksUnavailable):
        await cache.resolve("old")
    with pytest.raises(JwksUnavailable):
        await cache.resolve("old")
    assert len(state["calls"]) == 2


@pytest.mark.parametrize("fault", ["duplicate", "private", "algorithm", "use", "operations", "empty", "malformed", "weak"])
async def test_jwks_entire_document_rejected(keys, source, fault):
    cache, state = source
    if fault == "duplicate":
        state["keys"] = [keys[0], keys[0]]
    elif fault == "empty":
        state["keys"] = []
    elif fault == "malformed":
        state["keys"] = [None]
    elif fault == "weak":
        state["keys"] = [keys[0] | {"n": jwt.utils.to_base64url_uint((1 << 1023) + 1).decode()}]
    else:
        name, value = {
            "private": ("d", "forbidden"),
            "algorithm": ("alg", "HS256"),
            "use": ("use", "enc"),
            "operations": ("key_ops", ["sign"]),
        }[fault]
        state["keys"] = [keys[0] | {name: value}]
    with pytest.raises(JwksUnavailable):
        await cache.resolve("old")
    assert len(state["calls"]) == 1


@pytest.mark.parametrize("kid", ["old", "missing"])
async def test_jwks_concurrent_refresh_is_singleflight(keys, kid):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"keys": [keys[0]]})

    cache = IsolatedJwks("https://identity.test/certs", httpx.MockTransport(handler))
    first = asyncio.create_task(cache.resolve(kid))
    await entered.wait()
    waiting = [asyncio.create_task(cache.resolve(kid)) for _ in range(8)]
    release.set()
    results = await asyncio.gather(first, *waiting, return_exceptions=True)
    assert len(calls) == 1
    assert all(isinstance(result, str if kid == "old" else JwksUnavailable) for result in results)


async def test_jwks_invalidate_during_refresh_discards_old_generation(keys):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"keys": keys})

    clock = [0]
    cache = IsolatedJwks("https://identity.test/certs", httpx.MockTransport(handler), lambda: clock[0])
    pending = asyncio.create_task(cache.resolve("old"))
    await entered.wait()
    cache.invalidate()
    release.set()
    with pytest.raises(JwksUnavailable):
        await pending
    assert cache._keys == {}
    clock[0] = 30
    await cache.resolve("new")


async def test_jwks_live_cache_remains_bounded_during_unknown_failure(source):
    cache, state = source
    await cache.resolve("old")
    state.update(time=30, mode="timeout")
    with pytest.raises(JwksUnavailable):
        await cache.resolve("unknown")
    await cache.resolve("old")
    assert len(state["calls"]) == 2
    state["time"] = 300
    with pytest.raises(JwksUnavailable):
        await cache.resolve("old")


@pytest.mark.parametrize(
    "uri",
    [
        "http://identity.test/certs",
        "https://user@identity.test/certs",
        "https://identity.test/certs?target=other",
        "https://identity.test/certs#fragment",
    ],
)
def test_jwks_requires_fixed_trust_root(uri):
    with pytest.raises(ValueError):
        IsolatedJwks(uri, httpx.MockTransport(lambda request: httpx.Response(200)))


def test_jwks_rejects_real_transport():
    with pytest.raises(ValueError):
        IsolatedJwks("https://identity.test/certs", None)


async def test_jwks_concurrent_failure_is_not_retried(keys):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        entered.set()
        await release.wait()
        raise httpx.ConnectError("synthetic", request=request)

    cache = IsolatedJwks("https://identity.test/certs", httpx.MockTransport(handler))
    first = asyncio.create_task(cache.resolve("old"))
    await entered.wait()
    pending = [asyncio.create_task(cache.resolve("old")) for _ in range(8)]
    release.set()
    results = await asyncio.gather(first, *pending, return_exceptions=True)
    assert len(calls) == 1 and all(isinstance(result, JwksUnavailable) for result in results)


async def test_jwks_cache_invalidation_cannot_reuse_unexpired_key(source):
    cache, state = source
    await cache.resolve("old")
    cache.invalidate()
    with pytest.raises(JwksUnavailable):
        await cache.resolve("old")
    assert cache._keys == {} and len(state["calls"]) == 1
    state["time"] = 30
    await cache.resolve("old")
    assert len(state["calls"]) == 2
