"""Disposable F4 Runtime -> actual Gateway -> reviewed Data Hub, with synthetic providers only."""

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

    path = temporary / "f4-synthetic.sqlite"
    if path.exists():
        raise RuntimeError("fresh F4 database required")
    view_sql = canonical_view_sql("report", "synthetic", "tenant", ("item", "secret"))
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE synthetic (tenant TEXT, item TEXT, secret TEXT)")
        connection.executemany(
            "INSERT INTO synthetic VALUES (?,?,?)",
            [("default", "a1", "NEVER-RETURN-1"), ("default", "a2", "NEVER-RETURN-2"), ("foreign", "foreign-row", "NEVER-RETURN-3")],
        )
        connection.execute(view_sql)
        source_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='synthetic'").fetchone()[0]
    rule = ViewRule(
        "default", "orders", "v1", "report", "tenant", ("item", "secret"), frozenset({"secret"}), frozenset({"alice"}), frozenset()
    )
    binding = ReviewedBinding(
        "default",
        "orders",
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
        "f4-synthetic-v1",
        (
            LocalPolicyRule(
                "default",
                "orders",
                "v1",
                1,
                binding.view_sha256,
                binding.source_sha256,
                frozenset({"alice"}),
                frozenset(),
                frozenset({"item", "secret"}),
                frozenset({"item"}),
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
    gateway_key = ec.generate_private_key(ec.SECP256R1())
    act.reset_for_tests(gateway_key)
    auth.reset_for_tests()
    config.KEYCLOAK_TOKEN_URL = settings.jwt_issuer + "/protocol/openid-connect/token"
    config.JWT_ISSUER, config.JWKS_URL = settings.jwt_issuer, settings.jwks_url
    config.SERVICE_AUDIENCE = "capability-gateway"
    config.USER_AUDIENCE = frozenset({"ai-runtime"})
    config.TRUSTED_CALLERS = frozenset({"ai-runtime-svc"})
    expected = ContextVar("f4_expected_context")
    cap = Capability(
        id="cap.data.query",
        name="data.query",
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
                settings.jwt_issuer, "data-hub", "gateway", config.ACT_ISSUER, "ai-runtime-svc", "f4-data-query", "1", run_id, call_id
            ),
            tokens,
            verify_obo,
            verify_act,
        )

        from dataclasses import replace

        from runtime.api.isolated_policies import current_pair

        try:
            initial_pair = await current_pair("default")
            effective_binding, effective_rule, effective_policy = binding, rule, read_policy
            if initial_pair is not None:
                manifest, permit = initial_pair["manifest"], initial_pair["policy"]
                recipient = permit["recipient_binding"]
                user = await identity()
                if any(getattr(user, field) != recipient[field] for field in ("issuer", "audience", "client_id", "tenant", "subject")):
                    raise QueryDenied()
                if manifest["source"] != runtime_app.state.isolated_view_source:
                    raise QueryDenied()
                effective_binding = replace(
                    binding,
                    version=manifest["version"],
                    generation=manifest["generation"],
                    maintainer=manifest["maintainer"],
                    reviewers=frozenset(manifest["reviewers"]),
                )
                # Physical view stays the reviewed two-column synthetic projection. Policy only narrows exposure.
                effective_rule = replace(rule, version=manifest["version"], subjects=frozenset({recipient["subject"]}))
                proposal = permit["proposal"]
                effective_policy = LocalReadDataPolicy(
                    f"{permit['id']}:{permit['generation']}:{permit['revision']}:{permit['digest']}",
                    (
                        LocalPolicyRule(
                            "default",
                            "orders",
                            manifest["version"],
                            manifest["generation"],
                            binding.view_sha256,
                            binding.source_sha256,
                            frozenset({recipient["subject"]}),
                            frozenset(),
                            frozenset(proposal["columns"]),
                            frozenset({"item"}) if proposal["filter_item"] else frozenset(),
                            proposal["limit"],
                        ),
                    ),
                )

            async def binding_provider(user, query):
                if await current_pair("default") != initial_pair:
                    raise QueryDenied()
                return effective_binding

            guard = ReviewedQuery(
                path,
                Registry((effective_rule,)),
                UserProfile(settings.jwt_issuer, "data-hub", frozenset({"gateway"}), allow_delegated=True),
                identity,
                binding_provider,
                effective_policy,
                gate_timeout_s=1,
            )
            result = await LocalDataQuery(guard).invoke(json.loads(request.content))
            if await current_pair("default") != initial_pair:
                raise QueryDenied()
            print("F4_VERIFIED " + json.dumps({"run_id": run_id, "call_id": call_id, "verified": True}), flush=True)
            return httpx.Response(200, json=result)
        except (QueryDenied, HTTPException):
            return httpx.Response(403, json={"detail": "Restricted query denied"})

    main.set_invoke_transport_for_tests(httpx.MockTransport(transport))
    previous = toolbroker.execute_tool

    async def execute(tool, tool_input, dry_run=False, rc=None, call_id=None, idempotency_key=None):
        if tool.name != "data.query":
            return await previous(tool, tool_input, dry_run, rc, call_id, idempotency_key)
        if dry_run or rc is None or not rc.credential or not call_id:
            raise toolbroker.GatewayToolError("isolated query denied")
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
                raise toolbroker.GatewayToolError("isolated query denied")
            return response["output"]
        except Exception as exc:
            raise toolbroker.GatewayToolError("isolated query denied") from exc
        finally:
            expected.reset(context)

    toolbroker.execute_tool = execute
