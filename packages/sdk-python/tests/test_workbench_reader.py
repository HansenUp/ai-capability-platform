import httpx
import pytest

from ai_platform_sdk.workbench import WorkbenchReader

SCOPE = "a" * 64
ATTEMPT = "12345678-1234-4234-8234-123456789abc"
IDENTITY = {"protocol": "workbench-v1", "auth_method": "user", "scope_id": SCOPE, "tenant_id": "synthetic", "subject": "human:alice"}
ROW = {
    "scope_id": SCOPE,
    "attempt_id": ATTEMPT,
    "action": "credential",
    "run_id": "run",
    "request_seq": 1,
    "fingerprint": "b" * 64,
    "status": "PREPARED",
    "revision": 1,
    "evidence_seq": None,
    "rejection": None,
    "acknowledged": False,
}


def reader(active, *, scope=SCOPE, identity=None, receipt=None):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer synthetic"
        if request.url.path.endswith("/identity"):
            value = IDENTITY if identity is None else identity
        elif request.url.path.endswith("/attempts"):
            value = {"scope_id": scope, "active": active}
        else:
            value = ROW if receipt is None else receipt
        return httpx.Response(200, json=value)

    return WorkbenchReader("http://synthetic", "synthetic", httpx.MockTransport(handler)), calls


async def test_ledger_and_exact_receipt_are_only_reads():
    client, calls = reader(ROW)
    try:
        identity, active = await client.ledger()
        assert active.attempt_id == ATTEMPT
        assert await client.receipt(ATTEMPT, identity) == active
        assert len(calls) == 3
    finally:
        await client.aclose()


async def test_empty_ledger_is_returned_as_evidence_not_an_unlock_operation():
    client, calls = reader(None)
    try:
        _, active = await client.ledger()
        assert active is None
        assert len(calls) == 2
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "row",
    [
        {**ROW, "scope_id": "c" * 64},
        {**ROW, "acknowledged": True},
        {**ROW, "action": "feedback"},
        {**ROW, "request_seq": True},
        {**ROW, "status": "COMMITTED", "evidence_seq": 1},
        {**ROW, "call_id": "unexpected"},
        {**ROW, "fingerprint": "invalid"},
    ],
)
async def test_bad_or_unsupported_active_evidence_fails_closed(row):
    client, _ = reader(row)
    try:
        with pytest.raises(ValueError):
            await client.ledger()
    finally:
        await client.aclose()


async def test_mismatched_ledger_scope_rejected():
    client, _ = reader(None, scope="f" * 64)
    try:
        with pytest.raises(ValueError, match="mismatch"):
            await client.ledger()
    finally:
        await client.aclose()


async def test_service_identity_rejected_before_ledger_request():
    client, calls = reader(None, identity={**IDENTITY, "auth_method": "service"})
    try:
        with pytest.raises(ValueError):
            await client.ledger()
        assert len(calls) == 1
    finally:
        await client.aclose()


async def test_foreign_receipt_rejected_without_retry():
    client, calls = reader(None, receipt={**ROW, "scope_id": "f" * 64})
    try:
        identity, _ = await client.ledger()
        with pytest.raises(ValueError, match="mismatch"):
            await client.receipt(ATTEMPT, identity)
        assert len(calls) == 3
    finally:
        await client.aclose()
