import hashlib
import json
import sqlite3
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from capability_gateway import auth, config

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_query import q  # noqa: E402
from test_query import sample as sample

from data_hub.guarded import GuardedQuery, PolicyVerdict, SchemaPin  # noqa: E402
from data_hub.query import Identity, QueryDenied, Registry  # noqa: E402


@pytest.fixture
def signed_idp(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    public_key = ECAlgorithm.to_jwk(key.public_key(), as_dict=True) | {"kid": "synthetic", "alg": "ES256", "use": "sig"}
    issuer = "https://idp.invalid/realms/synthetic"
    calls = []

    def jwks(request):
        calls.append(request.url.path)
        assert request.method == "GET" and request.url.host == "idp.invalid"
        return httpx.Response(200, json={"keys": [public_key]})

    monkeypatch.setattr(config, "JWT_ISSUER", issuer)
    monkeypatch.setattr(config, "JWKS_URL", issuer + "/certs")
    monkeypatch.setattr(config, "transport", httpx.MockTransport(jwks))
    monkeypatch.setattr(auth, "_jwks", None)

    def token(*, _kid="synthetic", _typ="JWT", **changes):
        now = int(time.time())
        claims = {
            "iss": issuer,
            "iat": now,
            "exp": now + 300,
            "aud": "ai-runtime",
            "azp": "portal-web",
            "sub": "alice",
            "tenant": "tenant-a",
            "token_use": "access",
            "principal_type": "user",
            "realm_access": {"roles": ["viewer"]},
        }
        return jwt.encode(claims | changes, key, algorithm="ES256", headers={"kid": _kid, "typ": _typ})

    return token, calls


def pin_for(path, rule):
    with sqlite3.connect(path) as db:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = ?", (rule.sqlite_view,)).fetchone()[0]
    return SchemaPin(rule.tenant, rule.view_id, rule.version, rule.sqlite_view, hashlib.sha256(sql.encode()).hexdigest())


def identity_provider(token):
    async def read():
        # This strict profile exists only in this test. It is not an enabled production adapter.
        claims = await auth.verify_jwt(token, audience="ai-runtime", code="invalid_user_token")
        if claims.get("token_use") != "access" or claims.get("azp") != "portal-web" or claims.get("act") is not None:
            raise ValueError("synthetic direct-user profile rejected")
        roles = claims.get("realm_access", {}).get("roles")
        if not isinstance(roles, list) or any(not isinstance(role, str) or not role for role in roles):
            raise ValueError("invalid synthetic roles")
        principal = auth.principal_from_claims(claims, token)
        return Identity(principal.tenant, principal.sub, frozenset(principal.roles))

    return read


@pytest.mark.asyncio
async def test_verified_synthetic_identity_exact_policy_and_pinned_view(sample, signed_idp):
    path, registry, rule = sample
    token, calls = signed_idp
    requests = []

    async def policy(request):
        requests.append(request)
        return PolicyVerdict(request, "allow", "synthetic-v1")

    guard = GuardedQuery(path, registry, (pin_for(path, rule),), identity_provider(token()), policy)
    result = await guard.query(q(("item", "secret"), (("category", "open"),)))
    assert set(result.rows) == {("a1", "[REDACTED]"), ("a3", "[REDACTED]")}
    assert requests[0].action == "read_data" and requests[0].tenant == "tenant-a" and requests[0].subject == "alice"
    assert requests[0].columns == ("item", "secret") and requests[0].filter_columns == ("category",)
    assert calls == ["/realms/synthetic/certs"]
    assert "a-secret-1" not in json.dumps(result.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"aud": "wrong"},
        {"exp": 1},
        {"tenant": None},
        {"token_use": "id"},
        {"azp": "service-account"},
        {"act": {"sub": "agent"}},
        {"realm_access": {"roles": [3]}},
        {"tenant": "other"},
        {"sub": "other", "realm_access": {"roles": []}},
        {"_kid": "unknown"},
        {"iss": "https://other.invalid"},
        {"_tamper": True},
    ],
)
async def test_invalid_identity_never_reaches_policy_or_database(sample, signed_idp, monkeypatch, changes):
    path, registry, rule = sample
    token, _ = signed_idp
    pin = pin_for(path, rule)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("Invalid identity must not open a database"))

    async def policy(request):
        pytest.fail("Invalid identity must not reach policy")

    if changes == {"_tamper": True}:
        raw = token()
        head, body, signature = raw.split(".")
        signed = f"{head}.{body}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    else:
        signed = token(**changes)
    guard = GuardedQuery(path, registry, (pin,), identity_provider(signed), policy)
    with pytest.raises(QueryDenied, match="^Restricted query denied$"):
        await guard.query(q())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["deny", "invalid", "scope", "columns", "error", "timeout", "empty_version", "bad_version", "blank_version"]
)
async def test_policy_failure_closed_before_database(sample, signed_idp, monkeypatch, failure):
    path, registry, rule = sample
    token, _ = signed_idp
    pin = pin_for(path, rule)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("Policy failure must not open a database"))

    async def policy(request):
        if failure == "error":
            raise RuntimeError("synthetic private policy error")
        if failure == "timeout":
            raise TimeoutError("synthetic timeout")
        if failure == "invalid":
            return True
        if failure == "scope":
            return PolicyVerdict(replace(request, subject="other"), "allow", "synthetic-v1")
        if failure == "columns":
            return PolicyVerdict(replace(request, columns=("item",)), "allow", "synthetic-v1")
        if failure == "bad_version":
            return PolicyVerdict(request, "allow", 42)
        if failure == "blank_version":
            return PolicyVerdict(request, "allow", " ")
        return PolicyVerdict(request, "deny" if failure == "deny" else "allow", "" if failure == "empty_version" else "synthetic-v1")

    guard = GuardedQuery(path, registry, (pin,), identity_provider(token()), policy)
    with pytest.raises(QueryDenied, match="^Restricted query denied$"):
        await guard.query(q())


@pytest.mark.asyncio
async def test_current_baseline_read_data_denies_before_any_database_read(sample, signed_idp, monkeypatch):
    from policy_service.baseline import authorize

    path, registry, rule = sample
    token, _ = signed_idp
    pin = pin_for(path, rule)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("Current policy denial must not open database"))

    async def policy(request):
        allow, _, errors = authorize({"tenant": request.tenant}, "read_data", {"tenant": request.tenant}, {})
        assert not errors
        return PolicyVerdict(request, "allow" if allow else "deny", "existing-baseline")

    guard = GuardedQuery(path, registry, (pin,), identity_provider(token()), policy)
    with pytest.raises(QueryDenied, match="^Restricted query denied$"):
        await guard.query(q())


@pytest.mark.asyncio
async def test_definition_change_is_rejected_and_never_overrides_existing_policy(sample, signed_idp):
    path, registry, rule = sample
    token, _ = signed_idp
    pin = pin_for(path, rule)
    with sqlite3.connect(path) as db:
        db.execute("DROP VIEW report")
        db.execute("CREATE VIEW report AS SELECT tenant, item, secret, category, amount FROM synthetic WHERE 1=0")

    async def policy(request):
        return PolicyVerdict(request, "allow", "synthetic-v1")

    guard = GuardedQuery(path, registry, (pin,), identity_provider(token()), policy)
    with pytest.raises(QueryDenied, match="^Restricted query denied$"):
        await guard.query(q())


@pytest.mark.asyncio
async def test_definition_pin_and_data_read_share_one_sqlite_snapshot(sample, signed_idp, monkeypatch):
    path, registry, rule = sample
    token, _ = signed_idp
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    pin = pin_for(path, rule)
    original_connect = sqlite3.connect
    changed = False

    class InterceptConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal changed
            cursor = super().execute(sql, parameters)
            if sql.startswith("SELECT type, sql FROM sqlite_master") and not changed:
                changed = True
                with original_connect(path) as writer:
                    writer.execute("DROP VIEW report")
                    writer.execute("CREATE VIEW report AS SELECT tenant, item, secret, category, amount FROM synthetic WHERE 1=0")
            return cursor

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: original_connect(*args, factory=InterceptConnection, **kwargs))

    async def policy(request):
        return PolicyVerdict(request, "allow", "synthetic-v1")

    guard = GuardedQuery(path, registry, (pin,), identity_provider(token()), policy)
    result = await guard.query(q(("item",)))
    assert changed and {row[0] for row in result.rows} == {"a1", "a2", "a3"}
    with pytest.raises(QueryDenied):
        await guard.query(q(("item",)))


@pytest.mark.asyncio
async def test_pin_does_not_prove_underlying_tenant_lineage(sample, signed_idp):
    path, _, rule = sample
    token, _ = signed_idp
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE VIEW forged_view AS SELECT 'tenant-a' AS tenant, item, secret, category, amount FROM synthetic WHERE tenant='tenant-b'"
        )
    rule = replace(rule, sqlite_view="forged_view")
    registry = Registry((rule,))
    pin = pin_for(path, rule)

    async def policy(request):
        return PolicyVerdict(request, "allow", "synthetic-v1")

    guard = GuardedQuery(path, registry, (pin,), identity_provider(token()), policy)
    result = await guard.query(q(("item",)))
    # A trusted reviewer accepted the pin, but the view labels tenant-b's row as tenant-a.
    # Fingerprints and column checks cannot establish source lineage.
    assert ("b1",) in result.rows


def test_duplicate_or_invalid_pin_is_rejected(sample):
    path, registry, rule = sample
    pin = pin_for(path, rule)

    async def unused():
        raise AssertionError("must not be called")

    with pytest.raises(QueryDenied):
        GuardedQuery(path, registry, (pin, pin), unused, unused)
    with pytest.raises(QueryDenied):
        GuardedQuery(path, registry, (replace(pin, definition_sha256="not-a-digest"),), unused, unused)


@pytest.mark.asyncio
async def test_missing_pin_and_unavailable_jwks_fail_closed(sample, signed_idp, monkeypatch):
    path, registry, rule = sample
    token, _ = signed_idp
    pin = pin_for(path, rule)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("Must not open database"))

    async def no_policy(request):
        pytest.fail("Must not call policy")

    with pytest.raises(QueryDenied):
        await GuardedQuery(path, registry, (), identity_provider(token()), no_policy).query(q())

    def unavailable(request):
        raise httpx.ConnectError("synthetic JWKS unavailable", request=request)

    monkeypatch.setattr(config, "transport", httpx.MockTransport(unavailable))
    monkeypatch.setattr(auth, "_jwks", None)
    with pytest.raises(QueryDenied):
        await GuardedQuery(path, registry, (pin,), identity_provider(token()), no_policy).query(q())
