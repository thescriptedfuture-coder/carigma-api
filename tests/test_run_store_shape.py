"""The row we send must be a row `agent_runs` can hold.

## The bug this exists for

`new_run` generated `f"run_{uuid4().hex}"`. `agent_runs.id` has been
`uuid primary key` since V2_001. **Every insert was rejected with a 400, on
every run, from the day the table existed** — and nothing noticed for months.

Three components then disagreed about what happened: the score computed and
persisted, the API returned 200, and the run record did not exist. Only the
third was wrong, and you could not tell that from any of the three.

## Why the local tests could not have caught it

    class _NullRunStore:
        def save(self, run: object) -> None: ...

**A fake whose `save` does nothing cannot reject anything.** That is not a
simpler store — it is an *absent* one, and the behaviour it removed is the only
behaviour that mattered. Every "the run completes" test passed against a table
that refused every write.

`InMemoryRunStore` is the same defect with a dict in front of it: a dict accepts
any string as a key, so it too can never disagree with Postgres about a type.

## The shape of the fix

This checks the ROW, not the store. No database, no fake, no network — just
"is what we would send storable in the columns we declared". It runs in
milliseconds and would have failed on day one.

The general form, worth carrying: **when a fake stands in for something that
can REFUSE, the fake must be able to refuse too — or the refusal must be
checked somewhere the fake is not involved.**

## Why the schema is read rather than retyped

A list of uuid columns copied into this file is a second copy of the schema,
and a second copy is a second thing to be wrong — silently, in the direction of
passing. So the DDL is parsed out of `migrations/`, and the block is FOUND by
searching every migration rather than by naming a file: the first draft of this
guard hardcoded `V2_001_core.sql`, which does not exist. That returned an empty
column set, `parametrize` produced zero cases, and the real check disappeared
while the file still looked like a guard.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from carigma_api.services import runs as runs_service
from carigma_api.services.run_store import _TABLE, _to_row

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

#: Words that open a table-level clause rather than name a column.
_NOT_COLUMNS = {"constraint", "primary", "unique", "check", "foreign", "exclude", "like"}


def ddl() -> str:
    """The body of `create table ... agent_runs (...)`, from whichever
    migration declares it. Empty string if no migration does."""
    pattern = re.compile(rf"create\s+table[^(]*\bpublic\.{_TABLE}\s*\((.*?)\n\s*\);", re.S | re.I)
    for path in sorted(MIGRATIONS.glob("*.sql")):
        found = pattern.search(path.read_text(encoding="utf-8"))
        if found:
            return found.group(1)
    return ""


def columns() -> dict[str, str]:
    """`{name: type}` for every column in the declaration."""
    out: dict[str, str] = {}
    for line in ddl().splitlines():
        found = re.match(r"\s{2,}(\w+)\s+(\w+)", line)
        if found and found.group(1).lower() not in _NOT_COLUMNS:
            out[found.group(1)] = found.group(2).lower()
    return out


def uuid_columns() -> list[str]:
    return sorted(name for name, kind in columns().items() if kind == "uuid")


def a_run() -> runs_service.AgentRun:
    run = runs_service.new_run("00000000-0000-4000-8000-000000000001", "profile")
    run.finished_at = datetime.now(UTC)
    return run


def test_the_declaration_says_what_we_think_it_says() -> None:
    """The precondition, and it earns its place: with it absent, a wrong path
    or a changed DDL style empties every check below and the file still
    passes."""
    found = columns()
    assert found, f"no `create table public.{_TABLE}` found under {MIGRATIONS.name}/"
    assert found.get("id") == "uuid"
    assert found.get("user_id") == "uuid"
    assert "status" in found and "idempotency_key" in found


@pytest.mark.parametrize("column", uuid_columns())
def test_every_uuid_column_receives_something_postgres_accepts(column: str) -> None:
    """`UUID(value)` raises on exactly what Postgres rejects.

    Parametrised over what the migration declares, so a uuid column added later
    is covered when it is added rather than when someone remembers this file.
    """
    value = _to_row(a_run(), "some-idempotency-key").get(column)

    assert value is not None, f"{column} is a uuid column and the row carries no value for it"
    try:
        UUID(str(value))
    except ValueError:
        pytest.fail(
            f"agent_runs.{column} is uuid, and we would send {value!r}. Postgres "
            "answers that with `invalid input syntax for type uuid` and a 400 — "
            "silently, because the write is best-effort."
        )


def test_the_generated_id_carries_no_prefix() -> None:
    """The specific regression.

    `run_<hex>` read like a helpful id and was decoration: nothing anywhere
    parsed the prefix, and it cost the column its type.
    """
    run_id = runs_service.new_run("11111111-1111-1111-1111-111111111111", "profile").id

    assert not run_id.startswith("run_")
    assert UUID(run_id)


def test_two_runs_do_not_share_an_id() -> None:
    assert (
        len(
            {
                runs_service.new_run("11111111-1111-1111-1111-111111111111", "profile").id
                for _ in range(100)
            }
        )
        == 100
    )


def test_the_row_declares_no_column_the_table_lacks() -> None:
    """The other half of the same disagreement, and just as quiet.

    PostgREST rejects an unknown column with a 400 the same way it rejects a
    bad uuid, and `SupabaseRunStore` logs both and returns.
    """
    declared = set(columns())
    assert declared, "no columns parsed — this assertion would prove nothing"

    unknown = sorted(set(_to_row(a_run(), None)) - declared)

    assert unknown == [], f"we would send columns agent_runs does not have: {unknown}"
