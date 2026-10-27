# Explicit workbench protocol

`ai_platform_sdk.workbench_session.WorkbenchSession` is a separate API from the
legacy `PlatformClient.create_run` / `approve` helpers. It uses the existing
Runtime workbench protocol and direct, server-verified user Bearer identity.
No background execution, write retries, automatic resume or feedback delivery
is added.

Bind one session instance to one identity. Obtain and retain a canonical attempt
UUID **before** requesting prepare. Call the following methods separately in
response to explicit operator actions:

1. `prepare_create(agent, text, attempt_id)`, `prepare_approval(...)`,
   `prepare_answer(...)` or `prepare_credential(...)` registers the action only.
2. Inspect the returned immutable receipt. `execute(original_value)` executes
   create/answer with the original value; `execute()` executes other actions.
   A credential refresh uses the current Bearer, never a body-supplied secret.
3. Inspect terminal evidence. `acknowledge()` explicitly confirms that exact
   receipt; it does not execute the business operation again.

Any unknown write freezes new writes in this session. `recover()` only reads
the original identity/attempt and compares its bindings and revision. An empty
ledger or 404 does not unlock. A PREPARED read after an unknown execute does not
permit a resend. After a lost ACK response, recover exact acknowledged evidence
and explicitly call `confirm_closed()`; do not send another ACK.

Keep the session and attempt context while resolving an unknown outcome. This
version does not serialize session state or provide cross-process restoration;
do not create a new instance to bypass an unresolved write. An existing server
active lock blocks prepare by another instance as well. Unsupported feedback
locks fail closed. Service/delegated identities and administrator unlock are not
introduced. Token renewal/session persistence require a separately defined host
integration; these methods do not store credentials or messages on disk.

`WorkbenchReader` exposes only identity/ledger/receipt GETs for diagnostics.
`PlatformClient.follow_run` reads one Run's persisted SSE, closes resources on
cancellation, and requires explicit manual reconnection from the last yielded
sequence. SSE done ends the stream, not the Run or any write lock.

Tests use MockTransport and actual ASGI Runtime with temporary SQLite, signed
test users and synthetic tools. They are not proof of a production identity
integration, real business effects, SDK publication or production readiness.
