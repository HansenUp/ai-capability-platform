"""F2 isolated continuation policy. No route, persistence, model call or automatic execution.

Inputs must come from the authenticated Runtime and its authorized read path, never
from browser-supplied ownership claims. The caller rechecks the subject write ledger
when registering/executing the resulting text through the existing create protocol.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from runtime.api.auth import Principal

MAX_ATTACHMENT_BYTES = 1024 * 1024
MAX_ATTACHMENTS = 3
TERMINAL = frozenset({"completed", "failed", "cancelled"})


class ContinuationRejected(ValueError):
    pass


@dataclass(frozen=True)
class ConversationScope:
    tenant: str
    subject: str
    agent: str

    def authorize(self, principal: Principal, agent: str) -> None:
        if principal.auth_method != "user" or principal.actor is not None and not principal.actor.startswith("app:"):
            raise ContinuationRejected("signed direct user identity required")
        if (principal.tenant_id, principal.subject, agent) != (self.tenant, self.subject, self.agent):
            raise ContinuationRejected("conversation scope mismatch")


@dataclass(frozen=True)
class HistoricalRun:
    run_id: str
    tenant: str
    subject: str
    agent: str
    state: str
    # Only visible user/assistant text; no thinking, credentials or hidden tool output.
    messages: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SyntheticAttachment:
    name: str
    content: bytes


def compose(
    scope: ConversationScope,
    principal: Principal,
    agent: str,
    question: str,
    selected_run_ids: list[str],
    *,
    load_authorized_run: Callable[[str, Principal], HistoricalRun],
    attachments: list[SyntheticAttachment],
    scan: Callable[[bytes], bool],
    subject_locked: bool,
) -> str:
    """Build explicit new-Run input for isolated tests; never re-executes a prior Run.

    Fail closed on any unreadable selection or scan; do not silently omit history.
    The returned UTF-8 text is bound by the existing create-attempt HMAC.
    """
    scope.authorize(principal, agent)
    if subject_locked:
        raise ContinuationRejected("subject has an unresolved write")
    if not question.strip():
        raise ContinuationRejected("question required")
    if len(selected_run_ids) != len(set(selected_run_ids)):
        raise ContinuationRejected("duplicate historical run")
    if len(attachments) > MAX_ATTACHMENTS:
        raise ContinuationRejected("too many attachments")
    history = []
    for run_id in selected_run_ids:
        run = load_authorized_run(run_id, principal)
        if run.run_id != run_id or (run.tenant, run.subject, run.agent) != (scope.tenant, scope.subject, scope.agent):
            raise ContinuationRejected("historical run scope mismatch")
        if run.state not in TERMINAL:
            raise ContinuationRejected("historical run is not terminal")
        if any(role not in {"user", "assistant"} for role, _ in run.messages):
            raise ContinuationRejected("non-visible history role")
        history.append({"run_id": run_id, "state": run.state, "messages": list(run.messages)})
    documents = []
    for item in attachments:
        if not item.name or not item.name.endswith(".txt") or any(c in item.name for c in ("/", "\\", "\x00")):
            raise ContinuationRejected("synthetic txt filename required")
        if len(item.content) > MAX_ATTACHMENT_BYTES:
            raise ContinuationRejected("attachment too large")
        try:
            value = item.content.decode("utf-8", errors="strict")
        except UnicodeError:
            raise ContinuationRejected("UTF-8 required") from None
        if "\x00" in value or scan(item.content) is not True:
            raise ContinuationRejected("attachment scan rejected")
        documents.append({"name": item.name, "sha256": hashlib.sha256(item.content).hexdigest(), "text": value})
    return json.dumps(
        {"protocol": "isolated-continuation-v1", "question": question, "selected_history": history, "synthetic_attachments": documents},
        ensure_ascii=False,
        sort_keys=True,
    )
