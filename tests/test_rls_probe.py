"""The live RLS probe must cover every table the API writes as the user.

## Why this exists

The question: could a correct RLS refusal be surfacing as a broken product?
supabase-py asks for the written row back on every write, and Postgres errors —
not hides — when that row fails the table's SELECT policy. Measured from the
code on 2026-09-17: nineteen write sites across fifteen tables go through the
user client, every table has an owner SELECT policy, and every write stamps
`user_id` from the same token RLS reads. No instance.

But that measurement read migration text, and only four of the fifteen tables
had ever been checked against the live database. `verify_rls_crossuser.py` now
writes as the user, with each table's real write shape, on all fifteen.

A probe list is a list someone has to remember to update. This makes the code
the list's source: a table written anywhere in `src/` must be in the probe's
`WRITES_AS_USER` or in `SERVICE_ONLY` below, with a reason. The same rule
`verify_rls.py` follows — "derived, not maintained".

## What it cannot see

WHICH client a write goes through is decided at runtime. A new user-client
write to a table already listed here as service-only would pass. That is the
gap a classification by table cannot close; the reasons below say why each
table is believed service-only, so the belief can be checked.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

from tests.schema import SOURCES_FOUND, columns_of, not_null_of

ROOT = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "verify_rls_crossuser", ROOT / "scripts" / "verify_rls_crossuser.py"
)
assert _SPEC and _SPEC.loader
probe = importlib.util.module_from_spec(_SPEC)
sys.modules["verify_rls_crossuser"] = probe
_SPEC.loader.exec_module(probe)

WRITE_METHODS = {"insert", "upsert", "update"}

#: Tables written in `src/` only through the service role, and why.
SERVICE_ONLY: dict[str, str] = {
    "api_instances": "the startup heartbeat in services/instances.py; there is no user",
    "admin_notes": "routes/admin.py, whose `_db` is the service client",
    "credit_requests": "routes/admin.py; the user-facing request also goes through `_db`",
    "market_digests": "curated centrally in routes/market.py; users only read published rows",
}


def _module_constants(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.targets[0].id] = node.value.value
    return out


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)


def _written_tables() -> tuple[set[str], list[str]]:
    """Tables named in `.table(X).insert/upsert/update` anywhere in `src/`.

    `X` may be a literal, a module-level string constant (`TABLE`, `SLOTS`), or
    one imported from another module in the package (`sweep.py` imports
    `weekly_store.TABLE`). Anything else is returned as unresolved, and the test
    fails on it rather than skipping it: an unread write is a write this guard
    did not check.
    """
    paths = sorted((ROOT / "src").rglob("*.py"))
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in paths}
    by_module = {_module_name(path): _module_constants(tree) for path, tree in trees.items()}

    found: set[str] = set()
    unresolved: list[str] = []
    for path, tree in trees.items():
        constants = dict(by_module[_module_name(path)])
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in by_module:
                for alias in node.names:
                    value = by_module[node.module].get(alias.name)
                    if value is not None:
                        constants[alias.asname or alias.name] = value
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITE_METHODS
            ):
                continue
            target = node.func.value
            if not (
                isinstance(target, ast.Call)
                and isinstance(target.func, ast.Attribute)
                and target.func.attr == "table"
                and target.args
            ):
                continue
            arg = target.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in constants:
                found.add(constants[arg.id])
            else:
                unresolved.append(f"{path.name}:{node.lineno}")
    return found, unresolved


WRITTEN, UNRESOLVED = _written_tables()


def test_the_sweep_found_the_writes_it_is_about() -> None:
    # Nineteen sites over fifteen user tables were counted by hand; a sweep
    # finding a handful means it is reading less than it claims.
    assert len(WRITTEN) >= 15, f"only found {sorted(WRITTEN)}"
    assert UNRESOLVED == [], f"writes whose table could not be read: {UNRESOLVED}"


def test_every_table_written_in_src_is_probed_or_declared_service_only() -> None:
    unclassified = WRITTEN - set(probe.WRITES_AS_USER) - set(SERVICE_ONLY)
    assert unclassified == set(), (
        f"{sorted(unclassified)} are written by the API but neither probed as the user "
        "(verify_rls_crossuser.WRITES_AS_USER) nor declared service-only here"
    )


def test_no_entry_describes_a_table_nothing_writes() -> None:
    stale = (set(probe.WRITES_AS_USER) | set(SERVICE_ONLY)) - WRITTEN
    assert stale == set(), f"listed but never written in src/: {sorted(stale)}"


def test_every_user_written_table_has_a_seed_row() -> None:
    missing = set(probe.WRITES_AS_USER) - set(probe.OWNER_SCOPED)
    assert missing == set(), f"no template row in OWNER_SCOPED for {sorted(missing)}"


def test_every_seed_row_would_land() -> None:
    """A row missing a NOT NULL column reports SEED FAILED instead of measuring
    anything — the probe's first run lost four tables that way."""
    assert SOURCES_FOUND, "no DDL found; this check would prove nothing"
    problems: list[str] = []
    for table, template in probe.OWNER_SCOPED.items():
        columns = columns_of(table)
        if not columns:
            problems.append(f"{table}: no DDL found")
            continue
        unknown = set(template) - columns
        if unknown:
            problems.append(f"{table}: template names {sorted(unknown)}, not columns")
        # `user_id` is added by the probe for every row.
        absent = set(not_null_of(table)) - set(template) - {"user_id"}
        if absent:
            problems.append(f"{table}: NOT NULL without default and not supplied: {sorted(absent)}")
    assert problems == [], "\n".join(problems)


def test_update_patches_name_real_columns() -> None:
    problems = [
        f"{table}: {sorted(set(arg) - columns_of(table))}"
        for table, (method, arg) in probe.WRITES_AS_USER.items()
        if method in ("update", "insert+update") and set(arg) - columns_of(table)
    ]
    assert problems == [], problems


def test_upsert_conflict_targets_name_real_columns() -> None:
    problems = []
    for table, (method, arg) in probe.WRITES_AS_USER.items():
        if method != "upsert":
            continue
        cols = {c.strip() for c in str(arg).split(",")}
        if cols - columns_of(table):
            problems.append(f"{table}: {sorted(cols - columns_of(table))}")
    assert problems == [], problems
