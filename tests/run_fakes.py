"""A run store that behaves like the table, for tests that are not about storage.

## What this replaces

    class _NullRunStore:
        def save(self, run: object) -> None: ...

Two copies of that stood in for `SupabaseRunStore` across the route tests. It
looks like a reasonable simplification and is not one: **a `save` that does
nothing cannot reject anything**, so every route test passed against a table
that had refused every insert since the day it was created. The store was not
simplified, it was absent, and the absent behaviour was the only behaviour that
mattered.

`_NullRunStore.get` returning `None` unconditionally hid the other half: no
test ever read a run back, so `GET /agents/runs/{id}` and idempotency replay
had never once run against real storage.

## What this does instead

`save` builds the row `SupabaseRunStore` would send, using the same `_to_row`,
and checks it against the columns `migrations/` declares. It records the run,
so `get` and `find_by_idempotency_key` answer truthfully.

It cannot catch everything a database would — a foreign key, an RLS policy, a
check constraint. That is the honest limit, and it is why the parity sheet
exists. What it does catch is the class that actually happened: **a value of
the wrong type, or a column that is not there.**

## The general rule

**When a fake stands in for something that can REFUSE, the fake must be able to
refuse too — or the refusal has to be checked somewhere the fake is not
involved.** Anything else tests the fake.
"""

from __future__ import annotations

from uuid import UUID

from carigma_api.services.run_store import _TABLE, _to_row
from carigma_api.services.runs import AgentRun
from tests.schema import SOURCES_FOUND, columns_of, columns_typed, not_null_of


class SchemaViolation(AssertionError):
    """Raised where Postgres would answer 400 — loudly, unlike production.

    `SupabaseRunStore` logs and returns, deliberately: a run that computed a
    real answer should not fail because the audit write did. That is right in
    production and wrong in a test, where a swallowed write is the whole bug.
    """


def check_row(row: dict[str, object]) -> None:
    assert SOURCES_FOUND, "no migration files found — this check would pass on any row"
    declared = columns_of(_TABLE)
    assert declared, (
        f"no `create table public.{_TABLE}` found in any migration — this check "
        "would pass on any row at all"
    )

    unknown = sorted(set(row) - declared)
    if unknown:
        raise SchemaViolation(f"{_TABLE} has no column(s) {unknown}; PostgREST answers 400")

    for column in columns_typed(_TABLE, "uuid"):
        value = row.get(column)
        if value is None:
            continue
        try:
            UUID(str(value))
        except ValueError as exc:
            raise SchemaViolation(
                f"{_TABLE}.{column} is uuid and the row carries {value!r} — "
                "`invalid input syntax for type uuid`, 400"
            ) from exc

    for column in not_null_of(_TABLE):
        if row.get(column) is None:
            raise SchemaViolation(f"{_TABLE}.{column} is `not null` with no default, and is unset")


class RecordingRunStore:
    """Stores runs in a dict, having first checked they are storable."""

    def __init__(self) -> None:
        self.runs: dict[str, AgentRun] = {}
        self.rows: list[dict[str, object]] = []
        self._idempotency: dict[tuple[str, str], str] = {}

    def save(self, run: AgentRun) -> None:
        key = next((k for (u, k), r in self._idempotency.items() if r == run.id), None)
        row = _to_row(run, key)
        check_row(row)
        self.rows.append(row)
        self.runs[run.id] = run

    def get(self, run_id: str) -> AgentRun | None:
        return self.runs.get(run_id)

    def remember_idempotency(self, user_id: str, key: str, run_id: str) -> None:
        self._idempotency[(user_id, key)] = run_id

    def find_by_idempotency_key(self, user_id: str, key: str) -> AgentRun | None:
        run_id = self._idempotency.get((user_id, key))
        return self.runs.get(run_id) if run_id else None
