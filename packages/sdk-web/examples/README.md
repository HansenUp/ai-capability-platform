# Isolated business-host example

`readonly-run.ts` demonstrates embedding the independent
`@ai-platform/sdk-web/run-client` entry without registering the legacy Lit UI.
The host supplies a client bound to one authenticated user and one Run, renders
snapshot/event callbacks, and calls `close()` before identity or navigation changes.
Keep tokens in the host's controlled memory; never place them in URLs or logs.

This example only reads. It does not create a Run, submit feedback, approve, answer,
resume, acknowledge a write, or automatically reconnect. A stream ending does not
prove Run completion. The host must render the Runtime snapshot state separately.
Use the existing workbench prepare/execute/receipt/explicit ACK protocol when adding
writes; do not wrap legacy mutations with retries.

`readonly-run.test.mjs` uses in-memory clients. The S13 browser fixture
`apps/console-web/tests/browser/sdk-host.html` uses signed test identities against
the isolated temporary Runtime and verifies that mounting adds zero writes. It is
a business-system substitute, not a deployed integration or a published SDK release.
