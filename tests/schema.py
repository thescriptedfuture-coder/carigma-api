"""What the database actually declares, read at test time.

Tests that assert about database shape have two options: retype the schema, or
read it. A retyped schema is a second copy, and a second copy drifts silently
and in the direction of passing — the row keeps matching the copy long after it
stops matching the table.

## Two sources, because V2 owns only half the schema

`migrations/*.sql` holds the tables V2 created. **Most tables V2 writes to are
V1's** — `profiles`, `credits`, `credit_ledger`, `payments`, `digest_log`,
`email_log` — and V2 only ALTERs them. A reader that saw V2's migrations alone
could say nothing about the majority of writes, which is where both bugs of
this class were:

- `agent_runs.id` is `uuid`; we sent `run_<hex>`. Every insert refused.
- `digest_log.sent_on` is `date`; we sent `"2026-W34"`. Every weekly claim
  refused, and refusal reads as "already sent".

So V1's DDL is **vendored** into `migrations/v1_reference/`. It used to be read
from the parent repository, which works on a laptop where every repo sits in
one tree and **fails in CI, which checks out one repository alone.** Same class
as the first container deploy finding `supabase` undeclared: a local working
tree is a superset of any single repository.

`tests/test_schema_sources.py` compares the vendored copies against the parent
repo byte-for-byte whenever it is present, so the copy cannot drift unnoticed.

## Finding nothing is not the same as finding no problems

`types_of` returns `{}` unless it found a CREATE. That matters more than it
looks: an earlier version applied V2's ALTERs regardless, so a table whose
CREATE was missing came back with **the two or three columns V2 added** rather
than empty — a partial schema that looks like a real answer. Callers that
`skip` on an empty result then did not skip, and the guard reported false
alarms against a table it could not actually see.

Deliberately NOT handled: `alter column ... type`, computed columns, domains,
inherited tables. None appear in either repository; if one is added, this will
report the original type and be wrong. `types_of` is therefore only ever used
to REJECT values a column plainly cannot hold, never to assert one can.
"""

from __future__ import annotations

import re
from functools import cache, lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve()
MIGRATIONS = _HERE.parents[1] / "migrations"
#: V1's DDL, vendored. Read-only — see the README in that directory.
V1_REFERENCE = MIGRATIONS / "v1_reference"
#: The parent repository, present locally and absent in CI. Used ONLY by the
#: drift check, never to answer a question about the schema: a source that
#: exists on one machine is a source that gives two different answers.
PARENT_REPO = _HERE.parents[3] if len(_HERE.parents) > 3 else None

#: Words that open a table-level clause rather than name a column.
_NOT_COLUMNS = {"constraint", "primary", "unique", "check", "foreign", "exclude", "like"}


def _strip_comments(sql: str) -> str:
    """`--` and `/* */` removed.

    Not cosmetic: `alter table ... add column foo text` inside a comment would
    otherwise register as a real column, and the guards built on this would go
    quietly more permissive.
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", "", sql)


@lru_cache(maxsize=1)
def _sources() -> list[tuple[str, str]]:
    """`(name, sql)` for every migration, V1's vendored DDL first.

    Order is explicit rather than alphabetical: V1 CREATEs the tables and V2
    ALTERs them, and `V2_001...` sorts before `v1_reference/...` in ASCII.
    """
    out: list[tuple[str, str]] = []
    for path in sorted(V1_REFERENCE.glob("*.sql")) + sorted(MIGRATIONS.glob("*.sql")):
        out.append((path.name, _strip_comments(path.read_text(encoding="utf-8"))))
    return out


SOURCES_FOUND: int = len(_sources())


@cache
def types_of(table: str) -> dict[str, str]:
    """`{column: declared_type}` after every CREATE and ALTER, in source order.

    Empty if nothing creates the table.
    """
    create = re.compile(
        rf"create\s+table[^(]*\bpublic\.{re.escape(table)}\s*\((.*?)\n\s*\);", re.S | re.I
    )
    add = re.compile(
        rf"alter\s+table\s+(?:if\s+exists\s+)?public\.{re.escape(table)}\s+"
        r"add\s+column\s+(?:if\s+not\s+exists\s+)?(\w+)\s+(\w+)",
        re.S | re.I,
    )
    drop = re.compile(
        rf"alter\s+table\s+(?:if\s+exists\s+)?public\.{re.escape(table)}\s+"
        r"drop\s+column\s+(?:if\s+exists\s+)?(\w+)",
        re.S | re.I,
    )

    out: dict[str, str] = {}
    created = False

    # PASS 1: the CREATE, wherever it lives. Separate from pass 2 because a
    # single interleaved loop had this bug — ALTERs from an alphabetically
    # earlier file left `out` non-empty, an `if found and not out` guard then
    # skipped the CREATE block, and `profiles` came back as seventeen columns
    # with no `user_id`. Vacuous in the permissive direction, and it reported a
    # confident answer while doing it.
    for _name, sql in _sources():
        found = create.search(sql)
        if not found:
            continue
        for line in found.group(1).splitlines():
            col = re.match(r"\s{2,}(\w+)\s+(\w+)", line)
            if col and col.group(1).lower() not in _NOT_COLUMNS:
                out[col.group(1)] = col.group(2).lower()
        created = True
        break

    # No CREATE anywhere means we do not know this table. Returning the
    # columns V2's ALTERs happen to add would be a partial schema wearing the
    # shape of a complete one — see the module docstring.
    if not created:
        return {}

    # PASS 2: ALTERs, in source order.
    for _name, sql in _sources():
        for name, kind in add.findall(sql):
            out.setdefault(name, kind.lower())
        for name in drop.findall(sql):
            out.pop(name, None)
    return out


def columns_of(table: str) -> set[str]:
    return set(types_of(table))


def columns_typed(table: str, *kinds: str) -> list[str]:
    """Columns declared as any of `kinds` — e.g. `columns_typed(t, "uuid")`."""
    wanted = {k.lower() for k in kinds}
    return sorted(name for name, kind in types_of(table).items() if kind in wanted)


def not_null_of(table: str) -> list[str]:
    """Columns declared `not null` AND without a default.

    A `not null default now()` column may be omitted from an insert; a bare
    `not null` may not. Conflating them would make a guard reject rows Postgres
    accepts, and a guard that cries wolf gets deleted.
    """
    create = re.compile(
        rf"create\s+table[^(]*\bpublic\.{re.escape(table)}\s*\((.*?)\n\s*\);", re.S | re.I
    )
    for _name, sql in _sources():
        found = create.search(sql)
        if not found:
            continue
        out = []
        for line in found.group(1).splitlines():
            col = re.match(r"\s{2,}(\w+)\s+\w", line)
            if not col or col.group(1).lower() in _NOT_COLUMNS:
                continue
            low = line.lower()
            if "not null" in low and "default" not in low:
                out.append(col.group(1))
        return sorted(out)
    return []
