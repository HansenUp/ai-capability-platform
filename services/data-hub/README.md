# S14 restricted-view kernel

This directory contains an isolated SQLite query kernel, not an HTTP service or
a production data connector. The test host constructs `Identity`, immutable
`ViewRule` entries and a `Registry`, then calls `query_view` with a database path
and a structured `Query`. Run the audited local regression from the repository
root:

```shell
.venv/bin/python docs/evidence/s14/2026-10-27-local/verify_isolated.py --policy
```

The caller is responsible for the provenance of identities, rule configuration,
database files and view definitions. In particular, the tenant column must
truthfully describe each source row: checking the view's column names cannot
prove the lineage of an arbitrary SQL view. The current kernel must not be used
as a public identity boundary or production authorization adapter. The existing
policy baseline has no `read_data` permit; this example does not add one.

Queries use a read-only SQLite connection and a single read transaction. Allowed
columns and equality predicates are checked against the injected registry, row
tenant matching is text-only and binary exact, and masked columns are replaced
by `[REDACTED]` in the SELECT expression. Filters on masked columns are denied.
The response reports a bounded result and truncation, with no ordering or cursor
guarantee. Errors do not include raw SQL or database messages.

`guarded.py` is a separate local experiment that requires explicitly injected
identity and policy providers and a view-definition hash pin. It has no default
provider, endpoint or connection to the actual policy service. The hash check and
query share one SQLite read transaction, but a hash does not prove source tenant
lineage or protect against an incorrectly labelled row. The experiment performs
only a pre-read policy check; it does not implement a policy deadline, a post-read
revocation check or any production identity/token profile. See the S14 identity
and lineage contract and acceptance report before considering integration.

`reviewed.py`, `local_identity.py` and `local_policy.py` add the C1–C4 isolated
profile with explicit injected providers, a single-table direct-view schema pin,
and pre-read/post-read checks. Its approvals and DataView permit are synthetic,
not production policy or signed governance. The providers must supply trusted,
monotonic, non-replayable generations for revocation to be observable; this
package does not implement that persistence. Schema pins do not prove that source
rows are truthfully labelled. The configured gate timeout does not enforce a
whole-query SQLite deadline or forcibly stop a non-cooperative provider.
