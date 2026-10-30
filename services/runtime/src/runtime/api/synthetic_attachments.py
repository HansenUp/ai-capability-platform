"""Synthetic-only scanner; never claims malware detection or invokes external services."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from runtime.api.auth import Principal, current_principal
from runtime.run.continuation import MAX_ATTACHMENT_BYTES
from runtime.state.db import ContinuationInput, SessionLocal

SCANNER = "synthetic-only-v1"


class AttachmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    text: str = Field(max_length=MAX_ATTACHMENT_BYTES, repr=False, exclude=True)


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attachments: list[AttachmentBody] = Field(max_length=3)


def inspect(items: list[AttachmentBody]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    documents, results = [], []
    for item in items:
        reason = None
        try:
            raw = item.text.encode("utf-8", errors="strict")
        except UnicodeError:
            raw = b""
            reason = "INVALID_UTF8"
        if not item.name.endswith(".txt") or any(c in item.name for c in ("/", "\\", "\x00")):
            reason = "TXT_FILENAME_REQUIRED"
        elif len(raw) > MAX_ATTACHMENT_BYTES:
            reason = "TOO_LARGE"
        elif "\x00" in item.text or "SYNTHETIC_SCAN_REJECT" in item.text:
            reason = "SYNTHETIC_REJECTED"
        elif "SYNTHETIC_SCAN_UNAVAILABLE" in item.text:
            reason = "SCANNER_UNAVAILABLE"
        digest = hashlib.sha256(raw).hexdigest()
        results.append(
            {"name": item.name, "bytes": len(raw), "sha256": digest, "status": "rejected" if reason else "passed", "reason": reason}
        )
        documents.append({"name": item.name, "sha256": digest, "text": item.text})
    return documents, results


def accepted(items: list[AttachmentBody]) -> list[dict[str, Any]]:
    documents, results = inspect(items)
    if any(item["status"] != "passed" for item in results):
        raise HTTPException(422, "synthetic attachment validation or scan failed; no attempt registered")
    return documents


async def preview(body: PreviewBody, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    from runtime.api.conversations import enabled
    from runtime.api.workbench import user_scope

    enabled()
    scope = user_scope(principal)
    _, items = inspect(body.attachments)
    # No database mutation, retained upload, model or external scanner call.
    return {"scope_id": scope, "scanner": SCANNER, "items": items}


async def receipt(attempt_id: UUID, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    from runtime.api.conversations import enabled, execution
    from runtime.api.workbench import owned, user_scope

    enabled()
    scope = user_scope(principal)
    async with SessionLocal() as session:
        row = await owned(session, str(attempt_id), scope)
        saved = await session.get(ContinuationInput, row.attempt_id)
        if saved is None or row.action != "create":
            raise HTTPException(404, "continuation not found")
        value, _ = await execution(session, row, saved.question, principal)
        snapshot = json.loads(value)
        documents = snapshot.get("synthetic_attachments", [])
        if documents and snapshot.get("attachment_scanner") != SCANNER:
            raise HTTPException(409, "attachment scanner evidence unavailable")
        return {
            "scope_id": scope,
            "attempt_id": row.attempt_id,
            "fingerprint": row.fingerprint,
            "scanner": SCANNER,
            "items": [
                {"name": item["name"], "sha256": item["sha256"], "bytes": len(item["text"].encode()), "status": "passed", "reason": None}
                for item in documents
            ],
        }
