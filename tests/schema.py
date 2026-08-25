"""What the database actually declares, read at test time.

Tests that assert about database shape have two options: retype the schema, or
read it. A retyped schema is a second copy, and a second copy drifts silently
and in the direction of passing — the row keeps matching the copy long after it
stops matching the table.

## Two sources, because V2 owns only half the schema

`migrations/` holds the tables V2 created. **Most tables V2 writes to are V1's**
— `profiles`, `credits`, `credit_ledger`, `payments`, `digest_log`, `email_log`
— created by the `supabase_migration_*.sql` files in the parent repository and
only ALTERed by V2. A parser that reads `migrations/` alone can say nothing
about the majority of writes, which is where the two bugs of this class were:

- `agent_runs.id` is `uuid`; we sent `run_<hex>`. Every insert refused.
- `digest_log.sent_on` is `date`; we sent `"2026-W34"`. Every weekly claim
  refused, and refusal reads as "already sent".

So both sources are read, and `SOURCES_FOUND` is asserted non-empty by the
callers: a parser that quietly finds no DDL turns every guard built on it into
a guard that passes on anything.

## ALTER is applied, not treated as a reason to give up

An earlier version refused to answer for any table a later migration ALTERed,
which meant refusing for `profiles` and `credit_ledger` — the two most-written
tables in the system. Refusing is safe but useless. This applies
`add column` / `drop column` in source order instead.

Deliberately NOT handled: `alter column ... type`, computed columns, domains,
inherited tables. None appear in either repository; if one is added, this will
report the original type and be wrong. `types_of` is therefore only ever used
to REJECT values a column plainly cannot hold, never to assert a column can.
"""

from __future__ import annotations

import re
from functools import cache, lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve()
V2_MIGRATIONS = _HERE.parents[1] / "migrations"
#: V1's schema lives two repositories up. Absent in a bare clone of the API,
#: which is why the callers assert on `SOURCES_FOUND`.
V1_MIGRATIONS = _HERE.parents[3] if len(_HERE.parents) > 3 else None

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
    """`(name, sql)` for every migration file, V1 before V2."""
    out: list[tuple[str, str]] = []
    if V1_MIGRATIONS and V1_MIGRATIONS.is_dir():
        for path in sorted(V1_MIGRATIONS.glob("supabase_*.sql")):
            out.append((path.name, _strip_comments(path.read_text(encoding="utf-8"))))
    if V2_MIGRATIONS.is_dir():
        for path in sorted(V2_MIGRATIONS.glob("*.sql")):
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
        break

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
