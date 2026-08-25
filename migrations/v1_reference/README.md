# V1's schema, vendored — read-only

## Why these are here

V2 created `agent_runs`, `admin_notes`, `credit_requests` and a handful of
others. **Most tables V2 writes to are V1's** — `profiles`, `credits`,
`credit_ledger`, `payments`, `digest_log`, `email_log` — created by these files
and only ALTERed by `../V2_*.sql`.

`tests/schema.py` reads the DDL so that guards can check a row against the
columns it will actually meet. It used to read these files from the parent
repository, two directories up. That works on a laptop where every repo sits in
one tree, and **fails in CI, which checks out one repository alone** — the same
class as the first container deploy discovering `supabase` and `anthropic`
undeclared. A local working tree is a superset of any single repository.

## The rule

**Nothing here is ever edited.** They are copies. `tests/test_schema_sources.py`
compares them byte-for-byte against the parent repository whenever it is
present — which is every local run, and never in CI. So the copy cannot drift
without a local gate saying so, and CI still gets a complete schema.

If V1 gains a migration, copy it in and let the drift test confirm it landed.

## Why not check out both repositories in CI

It couples two workflows and needs a token for a private repo, to obtain files
that never change while V1 is bug-fix-only. When V2 owns the schema outright,
these move into `migrations/` proper and this directory goes away.
