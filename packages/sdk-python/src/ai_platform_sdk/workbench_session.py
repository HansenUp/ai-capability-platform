"""Explicit workbench writes. Unknown outcomes are frozen, never retried."""

import json
from typing import Any
from uuid import UUID

from .workbench import VerifiedIdentity, WorkbenchReader, WorkbenchReceipt

_MISSING = object()


class UnknownWorkbenchWrite(RuntimeError):
    """Keep the original attempt and recover evidence; do not resubmit."""


class WorkbenchSession(WorkbenchReader):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._identity: VerifiedIdentity | None = None
        self._pending: WorkbenchReceipt | None = None
        self._expected: dict[str, str] | None = None
        self._uncertain = False
        self._executed = False
        self._mutating = False

    @property
    def pending(self) -> WorkbenchReceipt | None:
        return self._pending

    async def _scope(self) -> WorkbenchReceipt | None:
        identity, active = await self.ledger()
        if self._identity is not None and identity != self._identity:
            raise ValueError("Workbench identity changed; keep the original lock")
        self._identity = identity
        return active

    def _match(self, row: WorkbenchReceipt) -> None:
        if self._identity is None or row.scope_id != self._identity.scope_id or self._expected is None:
            raise ValueError("Missing original workbench identity")
        for key, expected in self._expected.items():
            if getattr(row, key) != expected:
                raise ValueError("Receipt does not match original attempt")
        if self._pending is not None:
            before = self._pending.model_dump(exclude={"revision", "status", "evidence_seq", "rejection", "acknowledged"})
            after = row.model_dump(exclude={"revision", "status", "evidence_seq", "rejection", "acknowledged"})
            if before != after or row.revision < self._pending.revision:
                raise ValueError("Receipt binding changed or revision regressed")
            if self._pending.acknowledged and not row.acknowledged:
                raise ValueError("Acknowledgement evidence regressed")
            if self._pending.status != "PREPARED" and (
                row.status != self._pending.status
                or row.evidence_seq != self._pending.evidence_seq
                or row.rejection != self._pending.rejection
            ):
                raise ValueError("Terminal evidence changed")

    async def _post(self, path: str, body: dict[str, Any]) -> WorkbenchReceipt:
        try:
            response = await self._client.post(path, json=body)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Unexpected write response")
            row = WorkbenchReceipt.model_validate(response.json())
            self._match(row)
            return row
        except Exception:
            self._uncertain = True
            raise UnknownWorkbenchWrite("Write outcome or evidence unknown; recover the original attempt without resubmitting") from None
        except BaseException:
            self._uncertain = True
            raise

    async def _prepare(self, path: str, body: dict[str, Any], expected: dict[str, str]) -> WorkbenchReceipt:
        attempt_id = expected["attempt_id"]
        if str(UUID(attempt_id)) != attempt_id:
            raise ValueError("Canonical attempt UUID required")
        if self._mutating or self._uncertain or self._pending is not None or self._expected is not None:
            raise ValueError("Unresolved attempt blocks all new writes")
        self._mutating = True
        try:
            if await self._scope() is not None:
                raise ValueError("Subject has an active ledger; restore it before writing")
            self._expected = expected
            row = await self._post(path, body)
            if row.status != "PREPARED" or row.acknowledged:
                self._uncertain = True
                raise UnknownWorkbenchWrite("Unexpected prepare evidence; recover without resubmitting")
            self._pending = row
            return row
        finally:
            self._mutating = False

    async def prepare_create(self, agent: str, text: str, attempt_id: str) -> WorkbenchReceipt:
        """Register only. Caller supplies and retains a UUID before requesting."""
        if not agent.strip() or len(agent.strip()) > 64 or not text.strip():
            raise ValueError("Agent and original text required")
        return await self._prepare(
            "/v1/workbench/create-attempts",
            {"agent_id": agent.strip(), "text": text, "attempt_id": attempt_id},
            {"attempt_id": attempt_id, "action": "create", "create_agent": agent.strip()},
        )

    async def prepare_credential(self, run_id: str, attempt_id: str) -> WorkbenchReceipt:
        """Register refresh of the current user token, never a body-supplied credential."""
        return await self._prepare_run(run_id, "credential", attempt_id)

    async def prepare_approval(self, run_id: str, call_id: str, params_hash: str, approved: bool, attempt_id: str) -> WorkbenchReceipt:
        if type(approved) is not bool or not params_hash or len(params_hash) > 64:
            raise ValueError("Approval decision and original parameter fingerprint required")
        return await self._prepare_run(run_id, "approve" if approved else "reject", attempt_id, call_id, params_hash=params_hash)

    async def prepare_answer(self, run_id: str, call_id: str, value: Any, attempt_id: str) -> WorkbenchReceipt:
        json.dumps(value, allow_nan=False)  # Reject non-JSON before any request; server validates schema and HMAC.
        return await self._prepare_run(run_id, "answer", attempt_id, call_id, value=value)

    async def _prepare_run(
        self, run_id: str, action: str, attempt_id: str, call_id: str | None = None, params_hash: str | None = None, value: Any = _MISSING
    ) -> WorkbenchReceipt:
        if not run_id.strip() or len(run_id.strip()) > 40 or (action != "credential" and (not call_id or len(call_id) > 128)):
            raise ValueError("Run and original call binding required")
        body: dict[str, Any] = {"run_id": run_id.strip(), "action": action, "attempt_id": attempt_id}
        expected = {"run_id": run_id.strip(), "action": action, "attempt_id": attempt_id}
        if call_id is not None:
            body["call_id"] = call_id
            expected["call_id"] = call_id
        if params_hash is not None:
            body["params_hash"] = params_hash
            expected["params_hash"] = params_hash
        if value is not _MISSING:
            body["value"] = value
        return await self._prepare("/v1/workbench/attempts", body, expected)

    async def recover(self) -> WorkbenchReceipt:
        """Only GET. Empty lists/404 never clear a pending or unknown write."""
        if self._mutating or self._expected is None:
            raise ValueError("No original attempt to recover")
        active = await self._scope()
        assert self._identity is not None
        row = await self.receipt(self._expected["attempt_id"], self._identity)
        self._match(row)
        if active is not None and active.attempt_id != row.attempt_id:
            raise ValueError("Another active attempt conflicts with original evidence")
        if not row.acknowledged and (active is None or active != row):
            raise ValueError("Active ledger and original receipt conflict")
        self._pending = row
        self._uncertain = False
        return row

    async def execute(self, original_value: Any = _MISSING) -> WorkbenchReceipt:
        row = self._pending
        if self._mutating or self._uncertain or self._executed or row is None or row.status != "PREPARED" or row.acknowledged:
            raise ValueError("Execution blocked; recover evidence, never resend")
        if row.action == "create" and (not isinstance(original_value, str) or not original_value.strip()):
            raise ValueError("Original registered create message required")
        if row.action == "answer" and original_value is _MISSING:
            raise ValueError("Original registered JSON answer required")
        if row.action not in {"create", "answer"} and original_value is not _MISSING:
            raise ValueError("This action has no body-supplied value")
        body = {"fingerprint": row.fingerprint, "revision": row.revision}
        if row.action in {"create", "answer"}:
            json.dumps(original_value, allow_nan=False)
            body["value"] = original_value
        self._mutating = True
        self._executed = True
        try:
            result = await self._post(
                f"/v1/workbench/attempts/{row.attempt_id}/execute",
                body,
            )
            if result.status == "PREPARED" or result.acknowledged or result.revision <= row.revision:
                self._uncertain = True
                raise UnknownWorkbenchWrite("Execution lacks terminal evidence")
            self._pending = result
            return result
        finally:
            self._mutating = False

    async def acknowledge(self) -> WorkbenchReceipt:
        row = self._pending
        if self._mutating or self._uncertain or row is None or row.status == "PREPARED" or row.acknowledged:
            raise ValueError("Exact terminal receipt required before explicit acknowledgement")
        self._mutating = True
        try:
            result = await self._post(
                f"/v1/workbench/attempts/{row.attempt_id}/acknowledge", {"fingerprint": row.fingerprint, "revision": row.revision}
            )
            if not result.acknowledged or result.revision <= row.revision:
                self._uncertain = True
                raise UnknownWorkbenchWrite("Acknowledgement lacks matching evidence")
            self._pending = result
            self.confirm_closed()
            return result
        finally:
            self._mutating = False

    def confirm_closed(self) -> None:
        """Explicit local confirmation after an ACK response was lost; never sends a write."""
        if self._uncertain or self._pending is None or not self._pending.acknowledged:
            raise ValueError("Matching acknowledged evidence required")
        self._pending = None
        self._expected = None
        self._executed = False
