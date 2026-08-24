"""What the migrations actually declare, read at test time.

Tests that assert about database shape have exactly two options: retype the
schema here, or read it. A retyped schema is a second copy, and a second copy
drifts silently and in the direction of passing — the row keeps matching the
copy long after it stops matching the table.

So this parses `migrations/*.sql`. It is deliberately small: `create table`
bodies only, no ALTERs, no views. If a table is later reshaped by an ALTER in a
newer migration, `columns_of` will describe the original and be WRONG — which
is why `has_alter` exists and why the callers assert on it. A parser that
quietly describes an out-of-date table is the same failure it was written to
prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

#: Words that open a table-level clause rather than name a column.
_NOT_COLUMNS = {"constraint", "primary", "unique", "check", "foreign", "exclude", "like"}


def _body(table: str) -> str:
    """The text between the parentheses of `create table ... public.<table>`.

    Found by searching every migration rather than by naming a file: the first
    draft of the caller hardcoded a filename that did not exist, which returned
    nothing, which made the check that used it vacuous while it still passed.
    """
    pattern = re.compile(
        rf"create\s+table[^(]*\bpublic\.{re.escape(table)}\s*\((.*?)\n\s*\);", re.S | re.I
    )
    for path in sorted(MIGRATIONS.glob("*.sql")):
        found = pattern.search(path.read_text(encoding="utf-8"))
        if found:
            return found.group(1)
    return ""


def columns_of(table: str) -> dict[str, str]:
    """`{column: declared_type}`. Empty if no migration creates the table."""
    out: dict[str, str] = {}
    for line in _body(table).splitlines():
        found = re.match(r"\s{2,}(\w+)\s+(\w+)", line)
        if found and found.group(1).lower() not in _NOT_COLUMNS:
            out[found.group(1)] = found.group(2).lower()
    return out


def uuid_columns_of(table: str) -> list[str]:
    return sorted(name for name, kind in columns_of(table).items() if kind == "uuid")


def not_null_of(table: str) -> list[str]:
    """Columns declared `not null` AND without a default.

    A `not null default now()` column may be omitted from an insert; a bare
    `not null` may not. Conflating the two would make this reject rows Postgres
    accepts, and a guard that cries wolf gets deleted.
    """
    out = []
    for line in _body(table).splitlines():
        found = re.match(r"\s{2,}(\w+)\s+\w", line)
        if not found or found.group(1).lower() in _NOT_COLUMNS:
            continue
        low = line.lower()
        if "not null" in low and "default" not in low:
            out.append(found.group(1))
    return sorted(out)


def has_alter(table: str) -> bool:
    """True if any migration ADDs, DROPs, or ALTERs a column on this table.

    The parser above reads the original `create table` only. When this is true,
    what it returns may no longer match the live table, and the caller must
    say so rather than trusting it.
    """
    pattern = re.compile(
        rf"alter\s+table[^;]*\bpublic\.{re.escape(table)}\b[^;]*"
        r"\b(add|drop|alter)\s+column\b",
        re.S | re.I,
    )
    return any(pattern.search(p.read_text(encoding="utf-8")) for p in MIGRATIONS.glob("*.sql"))
