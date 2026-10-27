import hashlib
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from test_guarded import signed_idp as signed_idp
from test_query import sample as sample
from test_reviewed import setup as setup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from data_hub.delegated_identity import DelegatedIdentity, DelegationProfile  # noqa: E402
from data_hub.query import QueryDenied  # noqa: E402


@pytest.fixture
def delegated():
    idp_key = ec.generate_private_key(ec.SECP256R1())
    gateway_key = ec.generate_private_key(ec.SECP256R1())
    profile = DelegationProfile("https://idp.invalid", "data-hub", "gateway", "gateway-issuer", "runtime", "agent", "v1", "run", "call")
    now = int(time.time())
    user = {
        "iss": profile.obo_issuer,
        "aud": profile.audience,
        "azp": profile.gateway_client,
        "sub": "alice",
        "tenant": "tenant-a",
        "token_use": "access",
        "principal_type": "user",
        "iat": now,
        "exp": now + 300,
        "realm_access": {"roles": ["viewer"]},
    }

    def issue(user_changes=None, act_changes=None, *, wrong_obo_key=False, wrong_act_key=False, typ="at+jwt"):
        obo = jwt.encode(user | (user_changes or {}), gateway_key if wrong_obo_key else idp_key, algorithm="ES256", headers={"typ": typ})
        claims = {
            "iss": profile.act_issuer,
            "aud": profile.audience,
            "sub": "alice",
            "iat": now,
            "exp": now + 60,
            "jti": "synthetic-id",
            "act": {"sub": "agent:agent@v1", "act": {"sub": "client:runtime"}},
            "run_id": "run",
            "call_id": "call",
            "obo_hash": hashlib.sha256(obo.encode()).hexdigest(),
        }
        assertion = jwt.encode(claims | (act_changes or {}), idp_key if wrong_act_key else gateway_key, algorithm="ES256")

        async def tokens():
            return obo, assertion

        async def verify_obo(value):
            return jwt.decode(
                value,
                idp_key.public_key(),
                algorithms=["ES256"],
                audience=profile.audience,
                issuer=profile.obo_issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )

        async def verify_act(value):
            return jwt.decode(
                value,
                gateway_key.public_key(),
                algorithms=["ES256"],
                audience=profile.audience,
                issuer=profile.act_issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )

        return DelegatedIdentity(profile, tokens, verify_obo, verify_act)

    return issue


@pytest.mark.asyncio
async def test_signed_obo_and_act_produce_verified_user(delegated):
    user = await delegated()()
    assert (user.subject, user.tenant, user.roles) == ("alice", "tenant-a", frozenset({"viewer"}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"wrong_obo_key": True},
        {"wrong_act_key": True},
        {"typ": "JWT"},
        {"user_changes": {"aud": "other"}},
        {"user_changes": {"tenant": ""}},
        {"user_changes": {"token_use": "id"}},
        {"user_changes": {"principal_type": "service"}},
        {"user_changes": {"azp": "other"}},
        {"user_changes": {"exp": 1}},
        {"act_changes": {"sub": "other"}},
        {"act_changes": {"obo_hash": "0" * 64}},
        {"act_changes": {"run_id": "other"}},
        {"act_changes": {"call_id": "other"}},
        {"act_changes": {"act": {"sub": "agent:other@v1"}}},
        {"act_changes": {"exp": 1}},
        {"act_changes": {"jti": ""}},
        {"act_changes": {"aud": "other"}},
    ],
)
async def test_delegation_mismatch_fails_closed(delegated, arguments):
    with pytest.raises(QueryDenied, match="^Restricted query denied$"):
        await delegated(**arguments)()


@pytest.mark.asyncio
async def test_delegation_flows_through_reviewed_scope_and_rechecks_identity(delegated, setup, monkeypatch):
    import data_hub.reviewed as reviewed
    from data_hub.local_tool import LocalDataQuery
    from data_hub.reviewed import ReviewedQuery, UserProfile

    path, _, _, _, build = setup
    active = {"identity": delegated()}

    async def identity():
        return await active["identity"]()

    old = build()
    profile = UserProfile("https://idp.invalid", "data-hub", frozenset({"gateway"}), allow_delegated=True)
    guard = ReviewedQuery(path, old._registry, profile, identity, old._binding, old._policy, gate_timeout_s=1)
    request = {"view_id": "orders", "version": "v1", "columns": ["item", "secret"], "filters": [], "limit": 10}
    direct_profile = UserProfile("https://idp.invalid", "data-hub", frozenset({"gateway"}))
    direct_guard = ReviewedQuery(path, old._registry, direct_profile, identity, old._binding, old._policy, gate_timeout_s=1)
    with pytest.raises(QueryDenied):
        await LocalDataQuery(direct_guard).invoke(request)
    result = await LocalDataQuery(guard).invoke(request)
    assert len(result["rows"]) == 3 and all(row[1] == "[REDACTED]" for row in result["rows"])
    original = reviewed.query_view

    def change_tenant(*args, **kwargs):
        result = original(*args, **kwargs)
        active["identity"] = delegated(user_changes={"tenant": "tenant-b"})
        return result

    monkeypatch.setattr(reviewed, "query_view", change_tenant)
    with pytest.raises(QueryDenied):
        await LocalDataQuery(guard).invoke(request)
