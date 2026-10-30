"""Disposable F6.3 Runtime -> actual Gateway -> reviewed Data Hub, with synthetic providers only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException


def install(provider, temporary):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/data-hub/src"))
    from capability_gateway import act, auth, config, connectors, main, policy, state
    from capability_gateway.registry import Capability
    from data_hub.delegated_identity import DelegatedIdentity, DelegationProfile
    from data_hub.local_policy import LocalPolicyRule, LocalReadDataPolicy
    from data_hub.local_tool import LocalDataQuery
    from data_hub.query import QueryDenied, Registry, ViewRule
    from data_hub.reviewed import ReviewedBinding, ReviewedQuery, UserProfile, canonical_view_sql
    from runtime.config import settings
    from runtime.isolated_read import invoke_read
    from runtime.loop import toolbroker
    from runtime.state import db

    path = temporary / "f63-synthetic.sqlite"
    if path.exists():
        raise RuntimeError("fresh F6.3 database required")
    from data_hub.order_metrics import FIELDS, collect, window

    view_sql = canonical_view_sql("report", "synthetic", "tenant", FIELDS)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE synthetic (tenant TEXT, order_id TEXT, created_at TEXT, status TEXT, "
            "amount_minor INTEGER, currency TEXT, updated_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO synthetic VALUES (?,?,?,?,?,?,?)",
            [
                ("default", "A", "2026-10-01T00:00:00Z", "completed", 12000, "CNY", "2026-10-02T00:00:00Z"),
                ("default", "B", "2026-10-01T08:00:00Z", "completed", 500, "USD", "2026-10-02T00:00:00Z"),
                ("default", "C", "2026-10-01T09:00:00Z", "pending", 999, "CNY", "2026-10-02T00:00:00Z"),
                ("default", "D", "2026-10-02T09:00:00Z", "completed", None, "CNY", "invalid"),
                ("foreign", "NEVER-MODEL", "2026-10-01T00:00:00Z", "completed", 999999, "CNY", "invalid"),
            ],
        )
        connection.execute(view_sql)
        source_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='synthetic'").fetchone()[0]
    rule = ViewRule("default", "synthetic_orders", "v1", "report", "tenant", FIELDS, frozenset(), frozenset({"alice"}), frozenset())
    binding = ReviewedBinding(
        "default",
        "synthetic_orders",
        "v1",
        "report",
        "synthetic",
        "tenant",
        hashlib.sha256(view_sql.encode()).hexdigest(),
        hashlib.sha256(source_sql.encode()).hexdigest(),
        1,
        "synthetic-maintainer",
        frozenset({"reviewer-a", "reviewer-b"}),
        "active",
    )
    read_policy = LocalReadDataPolicy(
        "f63-policy-v1",
        (
            LocalPolicyRule(
                "default",
                "synthetic_orders",
                "v1",
                1,
                binding.view_sha256,
                binding.source_sha256,
                frozenset({"alice"}),
                frozenset(),
                frozenset(FIELDS),
                frozenset(),
                100,
            ),
        ),
    )
    from runtime.main import app as runtime_app

    runtime_app.state.isolated_view_source = {
        "profile": "synthetic-direct-view-v1",
        "view_sha256": binding.view_sha256,
        "source_sha256": binding.source_sha256,
    }
    authority = {"binding": binding, "policy": read_policy, "revision": 1}
    gateway_key = ec.generate_private_key(ec.SECP256R1())
    act.reset_for_tests(gateway_key)
    auth.reset_for_tests()
    config.KEYCLOAK_TOKEN_URL = settings.jwt_issuer + "/protocol/openid-connect/token"
    config.JWT_ISSUER, config.JWKS_URL = settings.jwt_issuer, settings.jwks_url
    config.SERVICE_AUDIENCE = "capability-gateway"
    config.USER_AUDIENCE = frozenset({"ai-runtime"})
    config.TRUSTED_CALLERS = frozenset({"ai-runtime-svc"})
    expected = ContextVar("f63_expected_context")
    cap = Capability(
        id="cap.analytics.orders",
        name="analytics.orders",
        description="synthetic",
        tenant="default",
        system="data",
        risk_level="read",
        data_class="confidential",
    )

    class Catalog:
        async def by_name(self, name, tenant):
            return cap if name == cap.name and tenant == cap.tenant else None

        async def connector_for(self, capability):
            return SimpleNamespace(name="data-hub", endpoint="http://data-hub.test", protocol="rest", health="up")

    state.catalog = Catalog()
    from ai_contract import ToolSpec
    from runtime.assemble.assembler import LOCAL_TOOLS

    LOCAL_TOOLS["analytics.orders"] = ToolSpec(
        name="analytics.orders",
        description="合成订单经营摘要",
        capability_id="cap.analytics.orders",
        data_class="confidential",
        input_schema={
            "type": "object",
            "properties": {"since": {"type": "string"}, "until": {"type": "string"}},
            "required": ["since", "until"],
            "additionalProperties": False,
        },
    )

    async def invoke_policy(*args, **kwargs):
        return policy.Decision("allow")  # Synthetic capability gate; read_data below is independent and exact.

    async def compliant(*args):
        return None

    async def exchange(*args):
        token = expected.get()[2]
        claims = jwt.decode(
            token, provider.keys["k1"].public_key(), algorithms=["RS256"], audience="ai-runtime", issuer=settings.jwt_issuer
        )
        return provider.token(
            {
                "sub": claims["sub"],
                "tenant": claims["tenant"],
                "aud": "data-hub",
                "azp": "gateway",
                "token_use": "access",
                "principal_type": "user",
                "realm_access": {"roles": []},
                "exp": int(time.time()) + 120,
            },
            typ="at+jwt",
        ), time.monotonic() + 60

    policy.check, connectors.require_compliant_tool, main._obo_token = invoke_policy, compliant, exchange

    async def transport(request):
        if request.url.host == "idp.test" and request.url.path.endswith("/certs"):
            return provider._jwks(request)
        if request.url.host != "data-hub.test" or request.method != "POST":
            raise AssertionError("unexpected F4 upstream")
        run_id, call_id, _ = expected.get()
        async with db.SessionLocal() as session:
            stored = await session.get(db.Run, run_id)
            agent_id, agent_version = stored.agent_id, stored.agent_version
        obo = request.headers["authorization"].removeprefix("Bearer ")
        assertion = request.headers[act.HEADER]

        async def tokens():
            return obo, assertion

        async def verify_obo(token):
            return jwt.decode(
                token,
                provider.keys["k1"].public_key(),
                algorithms=["RS256"],
                audience="data-hub",
                issuer=settings.jwt_issuer,
                options={"require": ["iss", "aud", "exp", "iat"]},
            )

        async def verify_act(token):
            return jwt.decode(
                token,
                gateway_key.public_key(),
                algorithms=["ES256"],
                audience="data-hub",
                issuer=config.ACT_ISSUER,
                options={"require": ["iss", "aud", "exp", "iat"]},
            )

        identity = DelegatedIdentity(
            DelegationProfile(
                settings.jwt_issuer, "data-hub", "gateway", config.ACT_ISSUER, "ai-runtime-svc", agent_id, agent_version, run_id, call_id
            ),
            tokens,
            verify_obo,
            verify_act,
        )

        try:
            arguments = json.loads(request.content)
            window(arguments)

            async def binding_provider(user, query):
                return authority["binding"]

            guard = ReviewedQuery(
                path,
                Registry((rule,)),
                UserProfile(settings.jwt_issuer, "data-hub", frozenset({"gateway"}), allow_delegated=True),
                identity,
                binding_provider,
                authority["policy"],
                gate_timeout_s=1,
            )

            async def reader():
                return await LocalDataQuery(guard).invoke(
                    dict(view_id="synthetic_orders", version="v1", columns=list(FIELDS), filters=[], limit=100)
                )

            output = await collect(reader, arguments, lambda: authority["revision"])
            output.update(policy_version="f63-policy-v1", data_class="confidential")
            print("F63_VERIFIED " + json.dumps({"run_id": run_id, "call_id": call_id, "verified": True}), flush=True)
            return httpx.Response(200, json=output)
        except (QueryDenied, HTTPException, ValueError) as exc:
            print("F63_DENIED " + type(exc).__name__, flush=True)
            return httpx.Response(403, json={"detail": "Restricted analysis unavailable"})

    main.set_invoke_transport_for_tests(httpx.MockTransport(transport))
    previous = toolbroker.execute_tool

    async def execute(tool, tool_input, dry_run=False, rc=None, call_id=None, idempotency_key=None):
        if tool.name != "analytics.orders":
            return await previous(tool, tool_input, dry_run, rc, call_id, idempotency_key)
        if dry_run or rc is None or not rc.credential or not call_id:
            raise toolbroker.GatewayToolError("isolated query denied")
        from runtime.api.auth import principal_from_claims, verify_jwt

        claims = await verify_jwt(rc.credential)
        principal = principal_from_claims(claims)
        if "act" in claims or principal.auth_method != "user" or claims.get("principal_type", "user") != "user":
            return {"error": "direct user required"}
        context = expected.set((rc.run_id, call_id, rc.credential))
        try:
            service = provider.token({"sub": "sa-runtime", "azp": "ai-runtime-svc", "aud": "capability-gateway"})
            async with db.SessionLocal() as session:
                response = await invoke_read(
                    session,
                    run_id=rc.run_id,
                    call_id=call_id,
                    user_token=rc.credential,
                    service_token=service,
                    gateway=httpx.ASGITransport(app=main.app),
                )
            if response.get("ok") is not True:
                return {"error": "Restricted analysis unavailable"}
            return response["output"]
        except Exception as exc:
            raise toolbroker.GatewayToolError("isolated query denied") from exc
        finally:
            expected.reset(context)

    toolbroker.execute_tool = execute
