"""Read-only authority evidence. Unsupported active actions fail closed, never unlock."""

import re
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

Hex = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class VerifiedIdentity(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    protocol: Literal["workbench-v1"]
    auth_method: Literal["user"]
    scope_id: Hex
    tenant_id: Annotated[str, Field(min_length=1)]
    subject: Annotated[str, Field(min_length=1)]


class WorkbenchReceipt(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    scope_id: Hex
    attempt_id: str
    action: Literal["credential", "approve", "reject", "answer", "create"]
    run_id: Annotated[str, Field(min_length=1)]
    request_seq: Annotated[int, Field(ge=0)]
    fingerprint: Hex
    status: Literal["PREPARED", "COMMITTED", "REJECTED_NO_EFFECT"]
    revision: Annotated[int, Field(ge=1)]
    evidence_seq: int | None
    rejection: str | None
    acknowledged: bool
    call_id: str | None = None
    params_hash: str | None = None
    value_hmac: Hex | None = None
    create_agent: str | None = None
    create_version: str | None = None
    create_model: str | None = None
    create_snapshot: Hex | None = None
    create_reason: str | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> "WorkbenchReceipt":
        if str(UUID(self.attempt_id)) != self.attempt_id:
            raise ValueError("Invalid attempt UUID")
        if self.status == "PREPARED" and (self.acknowledged or self.evidence_seq is not None or self.rejection is not None):
            raise ValueError("Invalid prepared evidence")
        if self.status == "COMMITTED" and (
            self.evidence_seq is None or self.evidence_seq <= self.request_seq or self.rejection is not None
        ):
            raise ValueError("Invalid committed evidence")
        if self.status == "REJECTED_NO_EFFECT" and (
            self.evidence_seq is not None or self.rejection not in {"VALIDATION_REJECTED", "STALE_WRITER"}
        ):
            raise ValueError("Invalid rejection evidence")
        create = (self.create_agent, self.create_version, self.create_model, self.create_snapshot, self.create_reason)
        if self.action == "create":
            if not all(create) or self.value_hmac is None or self.call_id is not None or self.params_hash is not None:
                raise ValueError("Invalid create binding")
            if self.request_seq != 0 or (self.status == "COMMITTED" and self.evidence_seq != 1):
                raise ValueError("Invalid create sequence")
        else:
            if any(value is not None for value in create) or self.request_seq < 1:
                raise ValueError("Invalid action binding")
            if self.action == "credential":
                if any(value is not None for value in (self.call_id, self.params_hash, self.value_hmac)):
                    raise ValueError("Invalid credential binding")
            elif not self.call_id or len(self.call_id) > 128:
                raise ValueError("Invalid call binding")
            elif self.action == "answer":
                if self.value_hmac is None or self.params_hash is not None:
                    raise ValueError("Invalid answer binding")
            elif not self.params_hash or len(self.params_hash) > 64 or self.value_hmac is not None:
                raise ValueError("Invalid approval binding")
        return self


class WorkbenchReader:
    """Uses only GET, no retries or inferred unlocking. Identity remains server verified.

    An empty ledger does not resolve an earlier unknown write. Callers must keep
    their original identity/attempt evidence and compare an exact receipt.
    """

    def __init__(self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not token.strip():
            raise ValueError("User bearer token required")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}", "Cache-Control": "no-store"},
            transport=transport,
            follow_redirects=False,
        )

    async def _get(self, path: str) -> Any:
        response = await self._client.get(path)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Unexpected evidence response status")
        return response.json()

    async def ledger(self) -> tuple[VerifiedIdentity, WorkbenchReceipt | None]:
        identity = VerifiedIdentity.model_validate(await self._get("/v1/workbench/identity"))
        value = await self._get("/v1/workbench/attempts")
        if not isinstance(value, dict) or value.get("scope_id") != identity.scope_id or "active" not in value:
            raise ValueError("Ledger identity mismatch; keep writes blocked")
        receipt = None if value["active"] is None else WorkbenchReceipt.model_validate(value["active"])
        if receipt is not None and (receipt.scope_id != identity.scope_id or receipt.acknowledged):
            raise ValueError("Invalid active receipt; keep writes blocked")
        return identity, receipt

    async def receipt(self, attempt_id: str, identity: VerifiedIdentity) -> WorkbenchReceipt:
        if not re.fullmatch(r"[a-f0-9-]{36}", attempt_id) or str(UUID(attempt_id)) != attempt_id:
            raise ValueError("Invalid attempt UUID")
        value = WorkbenchReceipt.model_validate(await self._get(f"/v1/workbench/attempts/{attempt_id}"))
        if value.scope_id != identity.scope_id or value.attempt_id != attempt_id:
            raise ValueError("Receipt identity or attempt mismatch; keep writes blocked")
        return value

    async def aclose(self) -> None:
        await self._client.aclose()
