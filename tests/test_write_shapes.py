"""Every row we send must fit the table we send it to.

## Two bugs, one shape

- `agent_runs.id` is `uuid`; `new_run` produced `run_<hex>`. Every insert
  refused, for months, silently — the store logs and returns by design.
- `digest_log.sent_on` is `date`; `weekly_key` produced `"2026-W34"`. Every
  weekly claim refused, and `claim_period` reads a refusal as "already sent",
  so no weekly review and no re-engagement digest has ever been sent.

Neither was a logic error. Both were **a value or a name the column could not
hold**, and in both cases the code between us and Postgres turned the rejection
into something that looked like an ordinary quiet outcome.

## What this checks

Every `.table("X").insert/upsert/update({...})` in `src/` and `scripts/`,
against the DDL of both repositories:

1. no key that `X` does not declare, and
2. for `uuid` / `date` columns given a literal string, a value of that type —
   checked with a parser STRICTER than Postgres, never looser.

It is static, so it cannot see rows built at runtime. Those are named in
`EXEMPT` with the guard that covers them instead, and the exemption list has to
prove every entry still exists: a stale exemption is a hole that reads as a
decision.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from uuid import UUID

import pytest

from tests.schema import SOURCES_FOUND, columns_of, live_primary_key, types_of, unique_keys_of

ROOT = Path(__file__).resolve().parents[1]
WRITE_METHODS = {"insert", "upsert", "update"}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Sites whose row is assembled at runtime, and what checks each one instead.
#: Keyed by (file, table, method) so it survives line movement.
EXEMPT: dict[tuple[str, str, str], str] = {
    ("repository.py", "profiles", "upsert"): "tests/test_profile_keys.py walks profile_to_db",
    ("admin.py", "credit_ledger", "insert"): "test_ledger_row_fits_the_table, below",
    ("payments.py", "payments", "insert"): "row is a parameter; test_payments_roundtrip covers it",
    ("send_emails.py", "email_log", "insert"): "derived from the literal row above it, minus keys",
    ("send_emails.py", "reengagement_sequences", "update"): "the dict is a parameter",
}


def _table_of(node: ast.expr) -> str | None:
    """Walk down a chained call looking for `.table("X")`."""
    cur: ast.expr = node
    while isinstance(cur, ast.Call):
        fn = cur.func
        if not isinstance(fn, ast.Attribute):
            break
        if fn.attr == "table" and cur.args and isinstance(cur.args[0], ast.Constant):
            value = cur.args[0].value
            return value if isinstance(value, str) else None
        cur = fn.value
    return None


def _sites() -> tuple[list[tuple[str, int, str, str, ast.Dict]], list[tuple[str, str, str]]]:
    """`(resolvable, unresolvable)` write sites."""
    resolvable: list[tuple[str, int, str, str, ast.Dict]] = []
    unresolvable: list[tuple[str, str, str]] = []

    files = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        for scope in scopes:
            local_dicts: dict[str, ast.Dict] = {
                a.targets[0].id: a.value
                for a in ast.walk(scope)
                if isinstance(a, ast.Assign)
                and len(a.targets) == 1
                and isinstance(a.targets[0], ast.Name)
                and isinstance(a.value, ast.Dict)
            }
            for call in [n for n in ast.walk(scope) if isinstance(n, ast.Call)]:
                fn = call.func
                if not (isinstance(fn, ast.Attribute) and fn.attr in WRITE_METHODS and call.args):
                    continue
                table = _table_of(fn.value)
                if not table:
                    continue
                arg: ast.expr = call.args[0]
                if isinstance(arg, ast.Name) and arg.id in local_dicts:
                    arg = local_dicts[arg.id]
                if isinstance(arg, ast.Dict):
                    resolvable.append((path.name, call.lineno, table, fn.attr, arg))
                else:
                    unresolvable.append((path.name, table, fn.attr))
    return resolvable, unresolvable


def _upserts() -> list[tuple[str, int, str, str | None, set[str]]]:
    """Every `.table("X").upsert(row, on_conflict=...)`: where, what, and the
    target it names. Payload keys come along, because an upsert that resolves
    on a key its row does not carry can only ever INSERT."""
    out: list[tuple[str, int, str, str | None, set[str]]] = []
    files = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {
            node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "upsert"
            ):
                continue
            table = _table_of(node)
            if table is None:
                inner = node.func.value
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "table"
                    and inner.args
                    and isinstance(inner.args[0], ast.Name)
                ):
                    table = constants.get(inner.args[0].id)
            if table is None:
                continue
            conflict: str | None = None
            for keyword in node.keywords:
                if keyword.arg == "on_conflict" and isinstance(keyword.value, ast.Constant):
                    conflict = str(keyword.value.value)
            keys: set[str] = set()
            if node.args and isinstance(node.args[0], ast.Dict):
                keys = {
                    k.value
                    for k in node.args[0].keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
            out.append((path.name, node.lineno, table, conflict, keys))
    return out


UPSERTS = _upserts()
UPSERT_IDS = [f"{f}:{n}:{t}" for f, n, t, _c, _k in UPSERTS]


def test_the_upsert_sweep_found_them() -> None:
    assert len(UPSERTS) >= 8, f"only found {UPSERT_IDS}"


@pytest.mark.parametrize("site", UPSERTS, ids=UPSERT_IDS)
def test_every_upsert_names_its_conflict_target(
    site: tuple[str, int, str, str | None, set[str]],
) -> None:
    """An upsert with no `on_conflict` resolves on the PRIMARY KEY — whichever
    key the database happens to call primary, which is not always the one the
    DDL in this repository declares.

    `profiles` is why. V1's DDL says `user_id uuid primary key`; the live table's
    primary key is `id`, with `user_id` merely unique. `upsert(payload)` with no
    `id` in the payload could never match, fell through to INSERT, and raised
    `duplicate key value violates unique constraint "profiles_user_id_key"` —
    a 503 on every completion of onboarding, every platform change, every
    analyst save, for as long as the row already existed.

    Naming the target is the whole fix, and it is checkable here.
    """
    name, line, table, conflict, _keys = site

    assert conflict, (
        f"{name}:{line} upserts into `{table}` without on_conflict, so it resolves on "
        f"whatever the live primary key is ({live_primary_key(table) or 'unknown'}). "
        "Name the columns this row is unique by."
    )


@pytest.mark.parametrize("site", UPSERTS, ids=UPSERT_IDS)
def test_every_conflict_target_is_a_key_that_exists(
    site: tuple[str, int, str, str | None, set[str]],
) -> None:
    """ON CONFLICT needs a unique index. A target without one does not degrade
    to an insert — it raises, every time."""
    name, line, table, conflict, keys = site
    if not conflict:
        pytest.skip("covered by the test above")
    declared = unique_keys_of(table)
    if not declared:
        pytest.skip(f"no DDL or snapshot key for {table}")

    target = tuple(c.strip() for c in conflict.split(","))
    assert set(target) in [set(k) for k in declared], (
        f"{name}:{line} upserts into `{table}` on {target}, which nothing declares unique. "
        f"Declared: {declared}"
    )
    # And the row has to carry the columns it claims to be unique by, or the
    # conflict can never be detected.
    if keys:
        missing = sorted(set(target) - keys)
        assert missing == [], f"{name}:{line} conflicts on {target} but the row omits {missing}"


RESOLVABLE, UNRESOLVABLE = _sites()
IDS = [f"{f}:{n}:{t}.{m}" for f, n, t, m, _ in RESOLVABLE]


def test_the_sweep_found_writes_and_a_schema_to_check_them_against() -> None:
    """The precondition. Both halves can fail to nothing, and a sweep that
    finds no writes reports the same green as one that finds no problems."""
    assert SOURCES_FOUND > 0, "no migration files found"
    assert RESOLVABLE, "no database writes found — the AST walk is looking in the wrong place"
    assert columns_of("agent_runs"), "the schema reader returned nothing for a known table"


@pytest.mark.parametrize("site", RESOLVABLE, ids=IDS)
def test_no_write_names_a_column_the_table_lacks(
    site: tuple[str, int, str, str, ast.Dict],
) -> None:
    name, line, table, _method, node = site
    declared = columns_of(table)
    if not declared:
        pytest.skip(f"no DDL found for {table} in either repository")

    keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    unknown = sorted(keys - declared)

    assert unknown == [], (
        f"{name}:{line} writes {unknown} to `{table}`, which does not declare them. "
        "PostgREST answers that with a 400."
    )


@pytest.mark.parametrize("site", RESOLVABLE, ids=IDS)
def test_no_literal_value_is_the_wrong_type_for_its_column(
    site: tuple[str, int, str, str, ast.Dict],
) -> None:
    """Only literals — a runtime value cannot be read from the AST.

    Deliberately stricter than Postgres for dates: `date.fromisoformat` accepts
    ISO WEEK strings in Python 3.11 and Postgres does not, so using Python's
    parser here would have passed on the `weekly_key` bug.
    """
    name, line, table, _method, node = site
    kinds = types_of(table)
    if not kinds:
        pytest.skip(f"no DDL found for {table}")

    for key, value in zip(node.keys, node.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        kind, literal = kinds.get(key.value), value.value
        if kind == "uuid":
            try:
                UUID(literal)
            except ValueError:
                pytest.fail(f"{name}:{line} sends {literal!r} to uuid column {table}.{key.value}")
        elif kind == "date":
            assert ISO_DATE.match(literal), (
                f"{name}:{line} sends {literal!r} to date column {table}.{key.value}; "
                "Postgres accepts only YYYY-MM-DD here"
            )


def test_every_exemption_still_exists() -> None:
    """An exemption for a site that has moved or gone is a hole nobody chose.

    Checked in both directions: a NEW unresolvable write must be added here
    with the guard that covers it, rather than quietly escaping the sweep.
    """
    found = set(UNRESOLVABLE)
    stale = sorted(k for k in EXEMPT if k not in found)
    unlisted = sorted(found - set(EXEMPT))

    assert stale == [], f"exemptions naming sites that no longer exist: {stale}"
    assert unlisted == [], (
        f"database writes this sweep cannot read, and which nothing else claims: {unlisted}. "
        "Add each to EXEMPT with the guard that covers it."
    )


def test_ledger_row_fits_the_table() -> None:
    """The exemption for `admin.py -> credit_ledger`, discharged directly.

    It is a money table and the row is built by a method, so the static sweep
    cannot see it. Calling the method can.
    """
    from carigma_api.services.admin import Adjustment

    row = Adjustment(
        user_id="11111111-1111-1111-1111-111111111111",
        delta=50,
        reason="a reason long enough to be accepted",
        author="ops@carigma.in",
    ).as_ledger_row(balance_after=50)

    declared = columns_of("credit_ledger")
    assert declared, "no DDL for credit_ledger"
    assert sorted(set(row) - declared) == [], f"credit_ledger has no column(s) in {sorted(row)}"
