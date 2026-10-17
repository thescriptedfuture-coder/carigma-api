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
import re
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

#: Tables that belong to NOBODY, deliberately. Empty, and the reason is worth
#: keeping.
#:
#: Its one entry was `jobs_cache`, filed here in S5 as "not owner-scoped and
#: not service-only, because the write happens inside a user action". The
#: proof below checked that the table had no `user_id` — true — and nothing
#: checked that a user could reach it at all. V2_007 had made it service-role
#: only. Every read through the user's client returned zero rows, every write
#: was refused, and the JSearch cap read 0 through three real scans.
#:
#: A category is a claim about the DATABASE. This one was believed rather than
#: measured; `test_no_user_path_names_a_service_only_table` now reads what the
#: migrations actually decided.
#:
#: **The claim is proved below, not asserted**: a table listed here must have
#: no `user_id` column. The moment one appears, it has an owner and belongs in
#: `WRITES_AS_USER` with a cross-user probe — the same rule as every other
#: exemption in this repository.
SHARED_NON_PERSONAL: dict[str, str] = {}


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
    unclassified = (
        WRITTEN - set(probe.WRITES_AS_USER) - set(SERVICE_ONLY) - set(SHARED_NON_PERSONAL)
    )
    assert unclassified == set(), (
        f"{sorted(unclassified)} are written by the API but neither probed as the user "
        "(verify_rls_crossuser.WRITES_AS_USER) nor declared service-only here"
    )


def test_nothing_shared_has_an_owner() -> None:
    """The proof behind `SHARED_NON_PERSONAL`.

    A table with a `user_id` HAS an owner, and declaring it ownerless would
    exempt real per-user rows from every cross-user check in this repository.
    An exemption needs a proof, not a name.
    """
    with_owners = sorted(t for t in SHARED_NON_PERSONAL if "user_id" in columns_of(t))
    assert with_owners == [], (
        f"{with_owners} carry a `user_id`, so they have an owner. Move them to "
        "WRITES_AS_USER with a cross-user probe."
    )


def test_no_entry_describes_a_table_nothing_writes() -> None:
    stale = (set(probe.WRITES_AS_USER) | set(SERVICE_ONLY) | set(SHARED_NON_PERSONAL)) - WRITTEN
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


# ── Reads, too: a table the user cannot see answers with NOTHING ───────────
#
# Everything above is about writes. The cap bug was a READ: `select … from
# jobs_cache` through the user's client returned `[]` with no error, so "how
# many calls today" was answered 0 by a table the caller was not allowed to
# look in. RLS does not fail a read; it filters it to nothing. A count built on
# a read the database will not let you make is a count of zero, forever.

MIGRATIONS = ROOT / "migrations"

#: Marker a migration uses to declare "RLS on, no policy, service role only".
SERVICE_ONLY_MARKER = "carigma:rls-service-role-only"

#: Modules that name a service-only table, and why the client they use there is
#: the service role. A module NOT listed here may not name one at all.
SERVICE_ROLE_MODULES: dict[str, str] = {
    "routes/admin.py": "`_db` is `service_client`; admin reads across users by definition",
    "routes/market.py": "each handler builds `service_client(settings)` behind the admin check",
    "services/instances.py": "the startup heartbeat; main.py passes it the service client",
    "services/referral_store.py": (
        "`referral_for` and `activate` run only from the service-role upload path; "
        "users reach `referrals` through SECURITY DEFINER functions, never a table read"
    ),
}


def _service_only_tables() -> set[str]:
    """What the MIGRATIONS make service-role only, read from the files.

    Two forms: V2_007's `service_only text[] := array[...]`, and any table a
    migration creates while declaring the marker and giving it no policy.
    Derived rather than listed, because the list that failed was a belief
    written in this file that the database disagreed with.
    """
    found: set[str] = set()
    for path in sorted(MIGRATIONS.glob("V2_*.sql")):
        raw = path.read_text(encoding="utf-8")
        block = re.search(r"service_only\s+text\[\]\s*:=\s*array\[([^\]]*)\]", raw, re.I)
        if block:
            found |= set(re.findall(r"'([a-z0-9_]+)'", block.group(1)))
        if SERVICE_ONLY_MARKER not in raw:
            continue
        sql = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("--"))
        for table in re.findall(
            r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?(\w+)", sql, re.I
        ):
            has_policy = re.search(
                rf"create\s+policy[^;]*\son\s+(?:public\.)?{re.escape(table)}\b", sql, re.I
            )
            if not has_policy:
                found.add(table.lower())
    return found


SERVICE_ONLY_TABLES = _service_only_tables()


def _tables_named_by_module() -> dict[str, set[str]]:
    """Every `.table(X)` in `src/` — read or write — by module, with `X`
    resolved through literals, module constants and imported constants."""
    paths = sorted((ROOT / "src").rglob("*.py"))
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in paths}
    by_module = {_module_name(path): _module_constants(tree) for path, tree in trees.items()}

    out: dict[str, set[str]] = {}
    for path, tree in trees.items():
        constants = dict(by_module[_module_name(path)])
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module in by_module:
                for alias in node.names:
                    value = by_module[node.module].get(alias.name)
                    if value is not None:
                        constants[alias.asname or alias.name] = value
        named: set[str] = set()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "table"
                and node.args
            ):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                named.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in constants:
                named.add(constants[arg.id])
            elif isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                # `quota.CACHE`, `store.TABLE`: a constant reached through an
                # imported module.
                for module, values in by_module.items():
                    if module.endswith("." + arg.value.id) and arg.attr in values:
                        named.add(values[arg.attr])
        if named:
            rel = path.relative_to(ROOT / "src" / "carigma_api").as_posix()
            out[rel] = named
    return out


NAMED_BY_MODULE = _tables_named_by_module()


def test_the_migrations_declare_the_service_only_tables_this_is_about() -> None:
    """A sweep that found none would pass on anything."""
    assert {"jobs_cache", "email_log", "digest_log", "api_instances"} <= SERVICE_ONLY_TABLES


def test_no_user_path_names_a_service_only_table() -> None:
    """The cap bug, as a rule.

    `services/jobs/quota.py` named `jobs_cache` and was handed the user's
    client. Nothing here can see WHICH client a call gets — that is decided at
    runtime — so the rule is drawn at the module: naming a service-only table
    is allowed only in a module declared above as service-role, with the
    reason.
    """
    offenders = sorted(
        f"{module} names {sorted(tables & SERVICE_ONLY_TABLES)}"
        for module, tables in NAMED_BY_MODULE.items()
        if tables & SERVICE_ONLY_TABLES and module not in SERVICE_ROLE_MODULES
    )
    assert offenders == [], (
        "these modules reach tables the migrations made service-role only. Through "
        "a user's client every read returns ZERO ROWS with no error and every write "
        "is refused: " + "; ".join(offenders)
    )


def test_every_declared_service_role_module_still_names_one() -> None:
    """A stale exemption is a hole that reads as a decision."""
    stale = sorted(
        module
        for module in SERVICE_ROLE_MODULES
        if not (NAMED_BY_MODULE.get(module, set()) & SERVICE_ONLY_TABLES)
    )
    assert stale == [], f"declared service-role but names no service-only table: {stale}"
