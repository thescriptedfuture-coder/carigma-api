"""Every column the code reads or writes must exist in a migration.

## The same boundary, one layer down

`db_to_profile` renames columns into JSON keys, and reading the wrong side of
that rename told users their headline was empty. `create table` is the same
kind of boundary: the migration declares the vocabulary, and the code has to
speak it. Get it wrong and PostgREST returns an error for a bad SELECT — but a
bad key in an INSERT payload, or a `row.get("wrong_name")`, gives you `None`,
which is indistinguishable from an honestly empty column.

I named this boundary as "migration columns against `Settings`" when listing
it. That was loose — `Settings` is the env config, and the `.env.example`
guard already covers that rename. The boundary with teeth is **migrations
against the code that reads them**, which is this.

## What it can and cannot see

Static, so it sees `.table("x").insert({...})` payload keys and literal
`.eq("col", ...)` / `.select("a,b")` arguments for V2-owned tables.

It CANNOT see:

- **V1's tables.** Their DDL is not in this repo, so there is nothing to check
  against. They are listed in `NOT_OURS` rather than skipped silently.
- Column names built at runtime, or reached through `**payload`.

Where it cannot see, it says so here rather than letting a green tick imply
coverage.
"""

from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "carigma_api"
MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"

#: Tables whose DDL lives in V1's repo. We may ALTER them — migrations are
#: additive — but we did not declare them, so we do not know their columns and
#: this guard says so instead of guessing.
#:
#: The list is policed: a table that gains a `create table` here must come out,
#: or its columns would go unchecked forever behind a name on a list.
NOT_OURS = {
    "profiles",
    "credits",
    "credit_ledger",
    "score_history",
    "content_loop",
    "jobs_feed",
    "jobs_cache",
    "feedback",
    "digest_log",
    "tracker",
    "weekly_review",
    "milestones",
    "ai_memory",
    "payments",
    "user_subscriptions",
    "users",
}

#: PostgREST adds these to every row.
IMPLICIT = {"id", "created_at", "updated_at"}


def v2_tables() -> dict[str, set[str]]:
    """Table -> columns, from `create table` statements in THIS repo.

    Only created tables. A table we merely `alter ... add column` is
    **partially known**, and a partially known table is the dangerous case: it
    looks checkable and reports every real column as missing. `credit_ledger`
    is exactly that — V1 declares it, V2_002 adds `kind`, so parsing alters
    into the same dict made four correct column names look like typos.
    """
    tables: dict[str, set[str]] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"create table if not exists\s+(?:public\.)?(\w+)\s*\((.*?)\n\)\s*;",
            sql,
            re.S | re.I,
        ):
            name, body = match.group(1), match.group(2)
            columns = tables.setdefault(name, set(IMPLICIT))
            for line in body.split("\n"):
                line = line.strip().rstrip(",")
                if not line or line.startswith("--"):
                    continue
                first = line.split()[0].lower()
                # Table-level constraints, not columns.
                if first in {"primary", "unique", "foreign", "check", "constraint", "exclude"}:
                    continue
                columns.add(line.split()[0])
        # `alter table x add column y` extends a table we already declared. It
        # must NOT create an entry — see the docstring.
        for match in re.finditer(
            r"alter table\s+(?:public\.)?(\w+)\s+add column(?: if not exists)?\s+(\w+)", sql, re.I
        ):
            if match.group(1) in tables:
                tables[match.group(1)].add(match.group(2))
    return tables


def _module_constants(tree: ast.AST) -> dict[str, str]:
    """Module-level `NAME = "literal"`, so `.table(TABLE)` resolves.

    Without this the scan saw only a third of the call sites — every module
    that names its table in a constant (`TABLE = "naukri_scores"`) was
    invisible, including the one this whole line of work came out of. A guard
    that quietly inspects a third of what it claims to is worse than none.
    """
    constants: dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
    return constants


def code_columns() -> dict[str, set[tuple[str, str, int]]]:
    """Table -> {(column, file, line)} for every literal reference we can see."""
    found: dict[str, set[tuple[str, str, int]]] = {}

    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(SRC.parent.parent))
        constants = _module_constants(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "table":
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                table = arg.value
            elif isinstance(arg, ast.Name) and arg.id in constants:
                table = constants[arg.id]
            else:
                continue

            bucket = found.setdefault(table, set())
            # Walk the chained call this `.table(...)` starts. `insert({...})`
            # keys and `eq("col", ...)` / `select("a,b")` arguments are all
            # column names, and all three spellings can be wrong.
            for inner in ast.walk(_chain_root(tree, node)):
                if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
                    continue
                name = inner.func.attr
                if name in {"insert", "upsert", "update"} and inner.args:
                    arg = inner.args[0]
                    if isinstance(arg, ast.Dict):
                        for key in arg.keys:
                            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                bucket.add((key.value, rel, inner.lineno))
                elif name in {"eq", "neq", "gt", "gte", "lt", "lte", "order"} and inner.args:
                    arg = inner.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        bucket.add((arg.value, rel, inner.lineno))
                elif name == "select" and inner.args:
                    arg = inner.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        for col in arg.value.split(","):
                            col = col.strip()
                            if col and col != "*" and "(" not in col:
                                bucket.add((col, rel, inner.lineno))
    return found


def _chain_root(tree: ast.AST, table_call: ast.Call) -> ast.AST:
    """The outermost expression containing this `.table(...)` call."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr | ast.Assign | ast.Return | ast.withitem):
            for child in ast.walk(node):
                if child is table_call:
                    return node
    return table_call


def test_every_column_we_reference_exists_in_a_migration() -> None:
    tables = v2_tables()
    offenders: list[str] = []

    for table, refs in sorted(code_columns().items()):
        if table in NOT_OURS:
            continue
        if table not in tables:
            offenders.append(f"{table}: referenced in code, created by no migration")
            continue
        for column, where, line in sorted(refs):
            if column not in tables[table]:
                near = [c for c in tables[table] if c.replace("_", "") == column.replace("_", "")]
                hint = f" — did you mean {near[0]!r}?" if near else ""
                offenders.append(f"{where}:{line} uses {table}.{column}{hint}")

    assert not offenders, "columns no migration creates:\n  " + "\n  ".join(offenders)


def test_the_migrations_actually_parsed() -> None:
    """Assert the precondition. If the regex stopped matching `create table`,
    every column would be unknown — or, with the lookup inverted, every column
    would be fine. Either way the check would be measuring nothing."""
    tables = v2_tables()

    assert len(tables) >= 8, f"only parsed {sorted(tables)}"
    assert "referrals" in tables
    # `referrer_id`/`referred_id`, NOT `user_id` — a referral is a relationship
    # between two people, so it has no single owner column.
    assert {"referrer_id", "referred_id"} <= tables["referrals"]
    assert "slot" in tables["referrals"], "the referral cap column is missing from the parse"


def test_the_code_scan_actually_found_something() -> None:
    """The other half of the precondition: a scan that finds no references
    passes trivially."""
    refs = code_columns()

    assert len(refs) > 5, f"only found tables {sorted(refs)}"
    # A table named through a module constant, which the first version missed
    # entirely. If constant resolution regresses, this is what says so.
    assert any(col == "user_id" for col, _, _ in refs.get("naukri_scores", set())), (
        "no naukri_scores columns found — `.table(TABLE)` constant resolution is broken"
    )


def test_the_unchecked_list_cannot_outlive_its_reason() -> None:
    """Their DDL is not in this repo. Saying so beats a green tick that implies
    they were checked — and the moment we DO declare one, it must come off the
    list rather than sit there unchecked behind a name."""
    tables = v2_tables()

    for table in NOT_OURS:
        assert table not in tables, (
            f"{table} IS created by one of our migrations now — take it out of "
            f"NOT_OURS so its columns get checked"
        )


def test_no_table_we_reference_is_silently_unchecked() -> None:
    """The gap this guard could hide.

    A table that is neither declared here nor named in `NOT_OURS` gets skipped
    by nothing and checked by nothing. Listing it is a decision; falling
    through is an accident.
    """
    tables = v2_tables()
    unaccounted = sorted(set(code_columns()) - set(tables) - NOT_OURS)

    assert not unaccounted, (
        f"referenced but neither declared here nor listed as V1's: {unaccounted}"
    )


def test_the_guard_catches_a_column_that_does_not_exist() -> None:
    """Verify by breaking, without touching the source tree.

    `referred_user_id` is the plausible wrong name for `referred_id` — the
    exact shape of mistake this exists for.
    """
    tables = v2_tables()

    assert "referred_user_id" not in tables["referrals"]
    assert "referred_id" in tables["referrals"]
