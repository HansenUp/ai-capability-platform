from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import delete, select

from console_api import owner_auth, owner_metrics
from console_api.auth import Role
from console_api.db import Agent, AgentVersion, Audit, SessionLocal, UserAgent


@pytest.fixture
def owner(c, monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())) | {"kid": "owner-isolated"}
    tenant = "owner-" + uuid4().hex[:12]
    binding = owner_auth.Binding("https://owner.test", tenant, "oidc-alice", "legacy-alice")
    profile = owner_auth.LocalProfile(
        binding.issuer,
        "console-owner-test",
        "console-test",
        json.dumps(public),
        (binding, replace(binding, subject="oidc-bob", user="legacy-bob")),
        (("owner-admin", Role.ADMIN), ("tenant-admin", Role.TENANT_ADMIN), ("platform-admin", Role.PLATFORM_ADMIN)),
    )
    monkeypatch.setenv("CONSOLE_OWNER_LOCAL_READ", "1")
    monkeypatch.setenv("CONSOLE_ENV", "dev")
    monkeypatch.setattr(owner_auth, "_profile", profile)

    async def seed():
        async with SessionLocal() as session:
            session.add_all(
                [
                    Agent(tenant_id=tenant, agent_id="owned", created_by="not-the-owner"),
                    Agent(tenant_id=tenant, agent_id="unassigned", created_by="legacy-alice"),
                    Agent(tenant_id=tenant + "-other", agent_id="owned", created_by="legacy-alice"),
                    UserAgent(tenant_id=tenant, agent_id="owned", user="legacy-alice"),
                    UserAgent(tenant_id=tenant, agent_id="owned", user="legacy-bob"),
                    AgentVersion(tenant_id=tenant, agent_id="owned", version="1", status="published", system_prompt="private"),
                    AgentVersion(tenant_id=tenant, agent_id="owned", version="2", status="draft"),
                    AgentVersion(tenant_id=tenant + "-other", agent_id="owned", version="1", status="retired"),
                ]
            )
            await session.commit()

    asyncio.run(seed())
    issued = int(owner_auth.now().timestamp())

    def headers(**changes):
        claims = {
            "iss": profile.issuer,
            "aud": profile.audience,
            "sub": binding.subject,
            "tenant": tenant,
            "azp": profile.client,
            "identity_type": "user",
            "iat": issued - 1,
            "nbf": issued - 1,
            "exp": issued + 600,
            "realm_access": {"roles": ["owner-admin"]},
        } | changes
        return {"Authorization": "Bearer " + jwt.encode(claims, private, algorithm="RS256", headers={"kid": "owner-isolated"})}

    stamp = owner_auth.now() - timedelta(days=10)
    since, until = stamp - timedelta(hours=1), stamp + timedelta(hours=1)
    rows = [
        {"id": "history", "timestamp": stamp.isoformat(), "metadata": {"tenant_id": tenant, "agent_id": "owned"}},
        {"id": "edge", "timestamp": until.isoformat(), "metadata": {"tenant_id": tenant, "agent_id": "owned"}},
        {"id": "foreign", "timestamp": stamp.isoformat(), "metadata": {"tenant_id": "foreign", "agent_id": "owned"}},
    ]
    calls = []
    state = {"rows": rows, "mode": "ok"}

    async def source(request):
        calls.append(request)
        assert request.method == "GET"
        assert "Bearer" not in request.headers.get("authorization", "")
        if state["mode"] == "outage":
            return httpx.Response(500, text="secret upstream body")
        if request.url.path == "/api/public/traces":
            assert request.url.params["name"] == "run owned"
            page = int(request.url.params["page"])
            values = state["rows"]
            return httpx.Response(
                200,
                json={
                    "data": values[(page - 1) * 100 : page * 100],
                    "meta": {"page": page, "limit": 100, "totalItems": len(values), "totalPages": (len(values) + 99) // 100},
                },
            )
        identifier = request.url.path.rsplit("/", 1)[1]
        row = next(row for row in state["rows"] if row["id"] == identifier)
        detail = row | {
            "input": "private-input",
            "observations": [
                {
                    "id": "generation",
                    "traceId": identifier,
                    "type": "GENERATION",
                    "usage": {"unit": "TOKENS", "total": 7},
                    "usageDetails": {"total": 7},
                    "totalCost": "0.002",
                }
            ],
        }
        if state["mode"] == "drift":
            detail["metadata"] = {"tenant_id": "foreign", "agent_id": "owned"}
        if state["mode"] == "missing":
            detail["observations"][0].update(usageDetails={}, totalCost=None)
        if state["mode"] == "revoke":
            await change_assignment(tenant, False)
        if state["mode"] == "aba":
            await change_assignment(tenant, False)
            await change_assignment(tenant, True)
        if state["mode"] == "expire":
            monkeypatch.setattr(owner_auth, "now", lambda: until_now + timedelta(seconds=601))
        if state["mode"] == "binding-change":
            monkeypatch.setattr(owner_auth, "_profile", replace(profile, bindings=(replace(binding, user="new-user"),)))
        return httpx.Response(200, json=detail)

    until_now = owner_auth.now()
    monkeypatch.setattr(owner_metrics, "_transport", httpx.MockTransport(source))
    return {
        "headers": headers,
        "tenant": tenant,
        "profile": profile,
        "calls": calls,
        "state": state,
        "params": {"since": since.isoformat(), "until": until.isoformat()},
        "private": private,
    }


async def change_assignment(tenant, present):
    async with SessionLocal() as session:
        await session.execute(delete(UserAgent).where(UserAgent.tenant_id == tenant, UserAgent.user == "legacy-alice"))
        if present:
            session.add(UserAgent(tenant_id=tenant, agent_id="owned", user="legacy-alice"))
        await session.commit()


SUMMARY = "/owner/agents/owned/summary"
TRACES = "/owner/agents/owned/trace-metrics"


def test_owner_summary_is_explicitly_assigned_and_minimal(c, owner):
    for subject in ("oidc-alice", "oidc-bob"):
        response = c.get(SUMMARY, headers=owner["headers"](sub=subject))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["versions"] == {"draft": 1, "published": 1, "retired": 0}
        assert set(body) == {
            "scope",
            "schema_version",
            "tenant_id",
            "agent_id",
            "read_started_at",
            "read_finished_at",
            "snapshot_guaranteed",
            "authorization_policy",
            "versions",
        }
        assert response.headers["cache-control"] == "no-store" and response.headers["vary"] == "Authorization"
        assert "private" not in response.text and "legacy" not in response.text
    assert owner["calls"] == []


def test_owner_reads_preassignment_history_with_half_open_boundary(c, owner):
    response = c.get(TRACES, headers=owner["headers"](), params=owner["params"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trace_calls"] == body["generations"] == 1
    assert body["tokens"]["total"] == 7 and body["cost_usd"]["total"] == "0.002"
    assert body["interval"] == "half_open" and body["source_trust"] == "metadata_consistency_only"
    assert body["snapshot_guaranteed"] is False and body["run_metrics_available"] is False
    assert not any(request.url.path.endswith(("/edge", "/foreign")) for request in owner["calls"])
    assert all(text not in response.text for text in ("private-input", "oidc-", "foreign", "Bearer", "history"))


@pytest.mark.parametrize(
    "change,status",
    [
        ({"sub": "unbound"}, 403),
        ({"iss": "https://other.test"}, 401),
        ({"aud": "ai-runtime"}, 401),
        ({"tenant": "other"}, 403),
        ({"exp": 1}, 401),
        ({"identity_type": "service"}, 403),
        ({"act": {"sub": "agent"}}, 403),
        ({"may_act": {}}, 403),
        ({"azp": "other-client"}, 403),
        ({"realm_access": {"roles": ["USER"]}}, 403),
        ({"sub": ""}, 401),
        ({"nbf": 9999999999}, 401),
        ({"realm_access": []}, 403),
        ({"realm_access": {"roles": [True]}}, 403),
        ({"client_id": "console-test"}, 403),
        ({"preferred_username": "service-account-console-test"}, 403),
    ],
)
def test_owner_rejects_untrusted_identity_before_source(c, owner, change, status):
    response = c.get(TRACES, headers=owner["headers"](**change), params=owner["params"])
    assert response.status_code == status
    assert owner["calls"] == [] and "trace_calls" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_owner_no_header_fallback_and_no_identity_overrides(c, owner):
    assert c.get(SUMMARY).status_code == 401
    assert c.get(SUMMARY, headers={"X-Console-Role": "PLATFORM_ADMIN"}).status_code == 400
    for key in ("user", "owner", "tenant", "role"):
        assert c.get(SUMMARY, headers=owner["headers"](), params={key: "forged"}).status_code == 400
    assert c.get(TRACES, headers=owner["headers"]() | {"X-Console-User": "legacy-alice"}, params=owner["params"]).status_code == 400
    assert c.get(TRACES, headers=owner["headers"](), params=list(owner["params"].items()) + [("since", "other")]).status_code == 422
    assert owner["calls"] == []


@pytest.mark.parametrize("roles", [["owner-admin"], ["tenant-admin"], ["platform-admin"]])
def test_owner_unassigned_creator_and_administrators_do_not_bypass_assignment(c, owner, roles):
    headers = owner["headers"](realm_access={"roles": roles})
    responses = [c.get(f"/owner/agents/{agent}/summary", headers=headers) for agent in ("unassigned", "missing")]
    assert all(response.status_code == 404 for response in responses)
    assert responses[0].json() == responses[1].json()
    asyncio.run(change_assignment(owner["tenant"], False))
    assert c.get(TRACES, headers=headers, params=owner["params"]).status_code == 404
    assert owner["calls"] == []


@pytest.mark.parametrize("mode,status", [("revoke", 404), ("expire", 401), ("binding-change", 403), ("aba", 200)])
def test_owner_deterministic_inflight_authorization_recheck(c, owner, mode, status):
    owner["state"]["mode"] = mode
    response = c.get(TRACES, headers=owner["headers"](), params=owner["params"])
    assert response.status_code == status, response.text
    assert owner["calls"]
    if status != 200:
        assert "tokens" not in response.text and "trace_calls" not in response.text


def test_owner_revocation_after_final_check_allows_only_admitted_response(c, owner, monkeypatch):
    original = owner_auth.assigned
    checks = 0

    async def barrier(principal, agent_id):
        nonlocal checks
        await original(principal, agent_id)
        checks += 1
        if checks == 2:
            await change_assignment(principal.tenant, False)

    monkeypatch.setattr(owner_auth, "assigned", barrier)
    assert c.get(TRACES, headers=owner["headers"](), params=owner["params"]).status_code == 200
    before = len(owner["calls"])
    assert c.get(TRACES, headers=owner["headers"](), params=owner["params"]).status_code == 404
    assert len(owner["calls"]) == before


@pytest.mark.parametrize("mode", ["outage", "drift"])
def test_owner_source_failure_never_returns_partial_values(c, owner, mode):
    owner["state"]["mode"] = mode
    response = c.get(TRACES, headers=owner["headers"](), params=owner["params"])
    assert response.status_code == 503 and "tokens" not in response.text and "secret" not in response.text


def test_owner_missing_values_and_no_sample_preserve_unknown(c, owner):
    owner["state"]["mode"] = "missing"
    body = c.get(TRACES, headers=owner["headers"](), params=owner["params"]).json()
    assert body["tokens"]["total"] is body["cost_usd"]["total"] is None
    owner["state"]["rows"] = []
    body = c.get(TRACES, headers=owner["headers"](), params=owner["params"]).json()
    assert body["trace_calls"] == 0 and body["tokens"]["known_trace_subtotal"] == 0
    assert body["tokens"]["total"] is body["cost_usd"]["total"] is None


def test_owner_disabled_prod_configuration_and_ambiguous_mapping_fail_closed(c, owner, monkeypatch):
    monkeypatch.delenv("CONSOLE_OWNER_LOCAL_READ")
    assert c.get(SUMMARY, headers=owner["headers"]()).status_code == 503
    monkeypatch.setenv("CONSOLE_OWNER_LOCAL_READ", "1")
    monkeypatch.setenv("CONSOLE_ENV", "prod")
    assert c.get(SUMMARY, headers=owner["headers"]()).status_code == 503
    monkeypatch.setenv("CONSOLE_ENV", "dev")
    monkeypatch.setattr(owner_auth, "_profile", replace(owner["profile"], bindings=owner["profile"].bindings * 2))
    assert c.get(SUMMARY, headers=owner["headers"]()).status_code == 503
    assert owner["calls"] == []


def test_owner_old_admin_and_feedback_permissions_remain_closed(c, owner):
    headers = {"X-Console-Role": "ADMIN", "X-Console-Tenant": owner["tenant"], "X-Console-User": "legacy-alice"}
    for path in ("/metrics/operations", "/metrics/traces", "/metrics/feedback", "/metrics/feedback-v2"):
        response = c.get(path, headers=headers, params={"agent_id": "owned"} | owner["params"])
        assert response.status_code == 403
    assert owner["calls"] == []


def test_owner_invalid_window_and_transport_do_not_query_real_service(c, owner, monkeypatch):
    for params in ({"since": "2026-01-01"}, {"until": "2099-01-01T00:00:00Z"}, {"since": "2020-01-01T00:00:00Z"}):
        assert c.get(TRACES, headers=owner["headers"](), params=owner["params"] | params).status_code == 422
    monkeypatch.setattr(owner_metrics, "_transport", None)
    assert c.get(TRACES, headers=owner["headers"](), params=owner["params"]).status_code == 503
    assert owner["calls"] == []


def test_owner_reads_do_not_append_console_audits(c, owner):
    async def count():
        async with SessionLocal() as session:
            return len((await session.execute(select(Audit.id).where(Audit.tenant_id == owner["tenant"]))).all())

    before = asyncio.run(count())
    assert c.get(SUMMARY, headers=owner["headers"]()).status_code == 200
    assert c.get(TRACES, headers=owner["headers"](), params=owner["params"]).status_code == 200
    assert asyncio.run(count()) == before


def test_owner_pagination_duplicate_and_source_limit_are_fail_closed(c, owner):
    from copy import deepcopy

    original = owner["state"]["rows"][0]
    owner["state"]["rows"] = [deepcopy(original) | {"id": str(index)} for index in range(101)]
    response = c.get(TRACES, headers=owner["headers"](), params=owner["params"])
    assert response.status_code == 200 and response.json()["trace_calls"] == 101
    owner["state"]["rows"].append(deepcopy(owner["state"]["rows"][0]))
    assert c.get(TRACES, headers=owner["headers"](), params=owner["params"]).status_code == 503
    owner["state"]["rows"] = [deepcopy(original) | {"id": str(index)} for index in range(2001)]
    response = c.get(TRACES, headers=owner["headers"](), params=owner["params"])
    assert response.status_code == 503 and "trace_calls" not in response.text


def test_owner_invalid_signature_and_missing_required_claims(c, owner):
    for changes in ({"tenant": None}, {"iat": True}, {"identity_type": None}):
        assert c.get(SUMMARY, headers=owner["headers"](**changes)).status_code == 401
    token = owner["headers"]()["Authorization"].split(" ")[1]
    signature = token.rsplit(".", 1)[1]
    changed = ("a" if signature[0] != "a" else "b") + signature[1:]
    assert c.get(SUMMARY, headers={"Authorization": "Bearer " + token.rsplit(".", 1)[0] + "." + changed}).status_code == 401
    assert c.get(SUMMARY, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert c.get(SUMMARY, headers=owner["headers"](aud=["console-owner-test", "ai-runtime"])).status_code == 401


def test_owner_authorization_store_failure_does_not_return_data(c, owner, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    async def unavailable(principal, agent_id):
        raise SQLAlchemyError("private storage diagnostic")

    monkeypatch.setattr(owner_auth, "assigned", unavailable)
    response = c.get(SUMMARY, headers=owner["headers"]())
    assert response.status_code == 503 and "private" not in response.text and "versions" not in response.text
    assert owner["calls"] == []


def test_owner_authorization_recheck_failure_discards_collected_data(c, owner, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    original = owner_auth.assigned
    checks = 0

    async def unavailable(principal, agent_id):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise SQLAlchemyError("private storage diagnostic")
        await original(principal, agent_id)

    monkeypatch.setattr(owner_auth, "assigned", unavailable)
    response = c.get(TRACES, headers=owner["headers"](), params=owner["params"])
    assert owner["calls"] and checks == 2
    assert response.status_code == 503 and "tokens" not in response.text and "private" not in response.text


def test_owner_malformed_key_profile_fails_closed(c, owner, monkeypatch):
    for value in ("[]", "invalid json"):
        monkeypatch.setattr(owner_auth, "_profile", replace(owner["profile"], public_jwk=value))
        assert c.get(SUMMARY, headers=owner["headers"]()).status_code == 503
    assert owner["calls"] == []


def test_owner_same_legacy_name_different_tenant_needs_its_own_assignment(c, owner, monkeypatch):
    tenant = owner["tenant"] + "-other"
    binding = replace(owner["profile"].bindings[0], tenant=tenant)
    monkeypatch.setattr(owner_auth, "_profile", replace(owner["profile"], bindings=owner["profile"].bindings + (binding,)))
    assert c.get(SUMMARY, headers=owner["headers"](tenant=tenant)).status_code == 404

    async def grant():
        async with SessionLocal() as session:
            session.add(UserAgent(tenant_id=tenant, agent_id="owned", user="legacy-alice"))
            await session.commit()

    asyncio.run(grant())
    response = c.get(SUMMARY, headers=owner["headers"](tenant=tenant))
    assert response.status_code == 200 and response.json()["tenant_id"] == tenant
    assert response.json()["versions"] == {"draft": 0, "published": 0, "retired": 1}


def test_owner_metrics_response_whitelists_collector_fields(c, owner, monkeypatch):
    original = owner_metrics.trace_metrics.collect

    async def extended(*args, **kwargs):
        metrics = await original(*args, **kwargs)
        metrics["private"] = "do-not-expose"
        metrics["tokens"]["private"] = "do-not-expose"
        metrics["cost_usd"]["private"] = "do-not-expose"
        return metrics

    monkeypatch.setattr(owner_metrics.trace_metrics, "collect", extended)
    response = c.get(TRACES, headers=owner["headers"](), params=owner["params"])
    assert response.status_code == 200 and "private" not in response.text and "do-not-expose" not in response.text


def test_owner_half_open_window_does_not_change_legacy_admin_inclusive_window(c, owner, monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "synthetic")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "synthetic")
    monkeypatch.setattr(owner_metrics.trace_metrics, "_transport", owner_metrics._transport)
    headers = {"X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": owner["tenant"]}
    legacy = c.get("/metrics/traces", headers=headers, params={"agent_id": "owned"} | owner["params"])
    assert legacy.status_code == 200 and legacy.json()["trace_calls"] == 2
    current = c.get(TRACES, headers=owner["headers"](), params=owner["params"])
    assert current.status_code == 200 and current.json()["trace_calls"] == 1


def loaded_owner(owner):
    from console_api.owner_profile import IsolatedProfileStore

    profile = owner["profile"]
    config = {
        "profile_id": "isolated",
        "issuer": profile.issuer,
        "audience": profile.audience,
        "client": profile.client,
        "role": "owner-admin",
    }
    manifest = {
        "schema_version": 1,
        "revision": 1,
        "profile_id": "isolated",
        "change_ref": "test-review",
        "bindings": [
            {"issuer": binding.issuer, "tenant": binding.tenant, "subject": binding.subject, "legacy_user": binding.user}
            for binding in profile.bindings
        ],
    }
    return IsolatedProfileStore(), config, manifest


def access_headers(owner, **changes):
    claims = jwt.decode(owner["headers"]()["Authorization"][7:], options={"verify_signature": False})
    claims.update({"token_use": "access"})
    claims["exp"] = claims["iat"] + 600
    claims.update(changes)
    return {
        "Authorization": "Bearer "
        + jwt.encode(claims, owner["private"], algorithm="RS256", headers={"kid": "owner-isolated", "typ": "at+jwt"})
    }


def test_loaded_owner_profile_signed_access_and_id_token_separation(c, owner, monkeypatch):
    store, config, manifest = loaded_owner(owner)
    loaded = store.replace(json.dumps(config), json.dumps(manifest), owner["profile"].public_jwk)
    monkeypatch.setattr(owner_auth, "_profile", loaded)
    assert c.get(SUMMARY, headers=access_headers(owner)).status_code == 200
    for headers in (
        owner["headers"](),
        access_headers(owner, **{"token_use": "id"}),
        access_headers(owner, token_use=None),
        access_headers(owner, exp=int(owner_auth.now().timestamp()) + 601),
    ):
        assert c.get(SUMMARY, headers=headers).status_code == 401
    assert owner["calls"] == []


@pytest.mark.parametrize("fault", ["identity", "legacy", "issuer", "extra", "boolean", "empty", "profile", "overflow"])
def test_loaded_owner_manifest_rejects_invalid_configuration(c, owner, fault):
    from copy import deepcopy

    store, config, manifest = loaded_owner(owner)
    row = deepcopy(manifest["bindings"][0])
    if fault == "identity":
        row["legacy_user"] = "another"
        manifest["bindings"].append(row)
    elif fault == "legacy":
        row["subject"] = "another"
        manifest["bindings"].append(row)
    elif fault == "issuer":
        row["issuer"] = "https://foreign.test"
        manifest["bindings"] = [row]
    elif fault == "extra":
        manifest["trusted"] = True
    elif fault == "boolean":
        manifest["revision"] = True
    elif fault == "empty":
        manifest["bindings"][0]["subject"] = " "
    elif fault == "profile":
        manifest["profile_id"] = "other"
    else:
        manifest["bindings"] = [row] * 2001
    with pytest.raises(ValueError):
        store.replace(json.dumps(config), json.dumps(manifest), owner["profile"].public_jwk)
    assert store.profile is None


def test_loaded_owner_revision_failure_and_empty_revocation(c, owner, monkeypatch):
    store, config, manifest = loaded_owner(owner)
    key = owner["profile"].public_jwk
    first = store.install_isolated(json.dumps(config), json.dumps(manifest), key)
    with pytest.raises(ValueError):
        store.install_isolated(json.dumps(config), json.dumps(manifest), key)
    assert store.profile is None and store.revision == 1
    assert c.get(SUMMARY, headers=access_headers(owner)).status_code == 503
    manifest.update(revision=2, bindings=[])
    second = store.replace(json.dumps(config), json.dumps(manifest), key)
    assert first is not second
    monkeypatch.setattr(owner_auth, "_profile", second)
    assert c.get(SUMMARY, headers=access_headers(owner)).status_code == 403


def test_loaded_owner_duplicate_json_fields_and_private_key_rejected(c, owner):
    store, config, manifest = loaded_owner(owner)
    with pytest.raises(ValueError):
        store.replace(
            json.dumps(config), json.dumps(manifest).replace('"revision": 1', '"revision": 1, "revision": 2'), owner["profile"].public_jwk
        )
    key = json.loads(owner["profile"].public_jwk) | {"d": "not-a-key"}
    with pytest.raises(ValueError):
        store.replace(json.dumps(config), json.dumps(manifest), json.dumps(key))
    assert store.profile is None


def test_loaded_owner_binding_generation_changes_discard_inflight(c, owner, monkeypatch):
    store, config, manifest = loaded_owner(owner)
    key = owner["profile"].public_jwk
    monkeypatch.setattr(owner_auth, "_profile", store.replace(json.dumps(config), json.dumps(manifest), key))
    original = owner_metrics.trace_metrics.collect

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        manifest["revision"] = 2
        monkeypatch.setattr(owner_auth, "_profile", store.replace(json.dumps(config), json.dumps(manifest), key))
        return result

    monkeypatch.setattr(owner_metrics.trace_metrics, "collect", changed)
    response = c.get(TRACES, headers=access_headers(owner), params=owner["params"])
    assert owner["calls"] and response.status_code == 403 and "tokens" not in response.text


@pytest.mark.parametrize(
    "field,value",
    [("issuer", "http://owner.test"), ("issuer", "https://user@owner.test"), ("audience", " "), ("client", True), ("role", "owner\nadmin")],
)
def test_loaded_owner_profile_rejects_invalid_fields(c, owner, field, value):
    store, config, manifest = loaded_owner(owner)
    config[field] = value
    with pytest.raises(ValueError):
        store.install_isolated(json.dumps(config), json.dumps(manifest), owner["profile"].public_jwk)
    assert owner_auth._profile is None


def test_loaded_owner_id_header_and_environment_gate(c, owner, monkeypatch):
    store, config, manifest = loaded_owner(owner)
    store.install_isolated(json.dumps(config), json.dumps(manifest), owner["profile"].public_jwk)
    claims = jwt.decode(access_headers(owner)["Authorization"][7:], options={"verify_signature": False})
    token = jwt.encode(claims, owner["private"], algorithm="RS256", headers={"kid": "owner-isolated", "typ": "JWT"})
    assert c.get(SUMMARY, headers={"Authorization": "Bearer " + token}).status_code == 401
    monkeypatch.setenv("CONSOLE_ENV", "prod")
    with pytest.raises(ValueError):
        store.install_isolated(json.dumps(config), json.dumps(manifest), owner["profile"].public_jwk)
    assert c.get(SUMMARY, headers=access_headers(owner)).status_code == 503


def test_owner_jwks_signed_route_and_profile_switch(c, owner, monkeypatch):
    from console_api.owner_jwks import IsolatedJwks

    store, config, manifest = loaded_owner(owner)
    profile = store.replace(json.dumps(config), json.dumps(manifest), owner["profile"].public_jwk)
    calls = []

    async def source(request):
        calls.append(request)
        assert request.method == "GET"
        if len(calls) == 2:
            monkeypatch.setattr(owner_auth, "_profile", replace(profile))
        return httpx.Response(200, json={"keys": [json.loads(profile.public_jwk)]})

    clock = [0]
    cache = IsolatedJwks("https://identity.test/certs", httpx.MockTransport(source), lambda: clock[0])
    profile = replace(profile, jwks=cache)
    monkeypatch.setattr(owner_auth, "_profile", profile)
    assert c.get(SUMMARY, headers=access_headers(owner)).status_code == 200
    assert len(calls) == 1
    clock[0] = 300
    response = c.get(SUMMARY, headers=access_headers(owner))
    assert response.status_code == 403 and "versions" not in response.text


def test_owner_jwks_rejects_header_injection_without_fetch(c, owner, monkeypatch):
    from console_api.owner_jwks import IsolatedJwks

    calls = []
    cache = IsolatedJwks("https://identity.test/certs", httpx.MockTransport(lambda request: calls.append(request)))
    monkeypatch.setattr(owner_auth, "_profile", replace(owner["profile"], access_contract=True, jwks=cache))
    claims = jwt.decode(access_headers(owner)["Authorization"][7:], options={"verify_signature": False})
    token = jwt.encode(claims, owner["private"], algorithm="RS256", headers={"kid": "owner-isolated", "jku": "https://evil.test"})
    assert c.get(SUMMARY, headers={"Authorization": "Bearer " + token}).status_code == 401
    assert c.get(SUMMARY).status_code == 401
    assert calls == []


def test_owner_jwks_rotated_key_verifies_and_removed_key_fails(c, owner, monkeypatch):
    from console_api.owner_jwks import IsolatedJwks

    store, config, manifest = loaded_owner(owner)
    profile = store.replace(json.dumps(config), json.dumps(manifest), owner["profile"].public_jwk)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())) | {"kid": "rotated"}
    state = {"time": 0, "keys": [json.loads(profile.public_jwk)]}
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"keys": state["keys"]})

    cache = IsolatedJwks("https://identity.test/certs", httpx.MockTransport(handler), lambda: state["time"])
    monkeypatch.setattr(owner_auth, "_profile", replace(profile, jwks=cache))
    assert c.get(SUMMARY, headers=access_headers(owner)).status_code == 200
    claims = jwt.decode(access_headers(owner)["Authorization"][7:], options={"verify_signature": False})
    token = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "rotated", "typ": "at+jwt"})
    state.update(time=30, keys=[public])
    assert c.get(SUMMARY, headers={"Authorization": "Bearer " + token}).status_code == 200
    assert c.get(SUMMARY, headers=access_headers(owner)).status_code == 503
    assert len(calls) == 2


def test_owner_jwks_expiry_at_final_check_discards_metrics(c, owner, monkeypatch):
    from console_api.owner_jwks import IsolatedJwks

    store, config, manifest = loaded_owner(owner)
    profile = store.replace(json.dumps(config), json.dumps(manifest), owner["profile"].public_jwk)
    clock = [0]
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) > 1:
            raise httpx.ReadTimeout("synthetic", request=request)
        return httpx.Response(200, json={"keys": [json.loads(profile.public_jwk)]})

    cache = IsolatedJwks("https://identity.test/certs", httpx.MockTransport(handler), lambda: clock[0])
    monkeypatch.setattr(owner_auth, "_profile", replace(profile, jwks=cache))
    original = owner_metrics.trace_metrics.collect

    async def expired(*args, **kwargs):
        result = await original(*args, **kwargs)
        clock[0] = 300
        return result

    monkeypatch.setattr(owner_metrics.trace_metrics, "collect", expired)
    response = c.get(TRACES, headers=access_headers(owner), params=owner["params"])
    assert response.status_code == 503 and "tokens" not in response.text
    assert len(calls) == 2 and owner["calls"]
