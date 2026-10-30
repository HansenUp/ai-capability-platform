from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import jwt


def tokens(provider: Any) -> dict[str, str]:
    return {
        "owner_" + name: provider.token(
            {
                "sub": name,
                "aud": "console-owner-test",
                "azp": "console-test",
                "identity_type": "user",
                "nbf": int(time.time()) - 1,
                "exp": int(time.time()) + 1800,
                "realm_access": {"roles": ["owner-admin"]},
            }
        )
        for name in ("alice", "bob")
    } | {
        "node_" + name: provider.token(
            {
                "sub": name,
                "aud": "console-owner-test",
                "azp": "console-test",
                "identity_type": "user",
                "nbf": int(time.time()) - 1,
                "exp": int(time.time()) + 1800,
                "realm_access": {"roles": ["node-admin"]},
            }
        )
        for name in ("alice", "bob")
    }


def configure(provider: Any) -> None:
    from console_api import owner_auth, owner_metrics
    from console_api.auth import Role

    os.environ["CONSOLE_OWNER_LOCAL_READ"] = "1"
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(provider.keys["k1"].public_key())) | {"kid": "k1"}
    issuer = "http://idp.test/realms/ai-platform"
    owner_auth._profile = owner_auth.LocalProfile(
        issuer,
        "console-owner-test",
        "console-test",
        json.dumps(public),
        tuple(owner_auth.Binding(issuer, "default", name, "owner-" + name) for name in ("alice", "bob")),
        (("owner-admin", Role.ADMIN), ("alert-admin", Role.TENANT_ADMIN), ("node-admin", Role.PLATFORM_ADMIN)),
    )

    def source(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            raise AssertionError("Owner fixture only supports GET")
        row = {"id": "owner-history", "timestamp": "2026-10-21T12:00:00Z", "metadata": {"tenant_id": "default", "agent_id": "s12-owner"}}
        if request.url.path == "/api/public/traces":
            return httpx.Response(
                200,
                json={
                    "data": [row],
                    "meta": {"page": 1, "limit": 100, "totalItems": 1, "totalPages": 1},
                },
            )
        if request.url.path != "/api/public/traces/owner-history":
            raise AssertionError("unexpected Owner fixture path")
        return httpx.Response(
            200,
            json=row
            | {
                "observations": [
                    {
                        "id": "owner-generation",
                        "traceId": row["id"],
                        "type": "GENERATION",
                        "usage": {"unit": "TOKENS", "total": 9},
                        "usageDetails": {"total": 9},
                        "totalCost": "0.0009",
                    }
                ]
            },
        )

    owner_metrics._transport = httpx.MockTransport(source)


def seed(console: str) -> None:
    with httpx.Client(base_url=console, trust_env=False, timeout=10) as client:
        assert client.post("/agents", json={"agent_id": "s12-owner", "version": "1"}).status_code == 201
        assert client.put("/agents/s12-owner/admins", json={"users": ["owner-alice"]}).status_code == 200
