"""Migration safety tests.

V1 (Streamlit) and V2 share ONE Supabase project, and V1 stays live until
cutover. A destructive migration would take the live product down. Review alone
is not a good enough guard for that, so the constraint is mechanical: this test
reads every V2 migration and fails on anything that could break V1.

Adding a new migration with a `DROP COLUMN` in it fails CI. That is the point.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Inside THIS repo, not the parent workspace. The migrations previously lived
# at `v2/migrations/`, which the root .gitignore excludes via `v2/*` — so the
# schema V2 depends on existed only on one laptop and had never been committed.
# Found while committing V2_002. They belong beside the code that depends on
# them and beside this test.
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def _sql_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("V2_*.sql"))


def _strip_comments(sql: str) -> str:
    """Remove -- line comments and /* */ blocks.

    Without this, the file's own prose explaining "contains NO DROP/RENAME"
    would trip every check below.
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


# Patterns that would change or remove something V1 might depend on.
FORBIDDEN = [
    (r"\bdrop\s+table\b", "DROP TABLE"),
    (r"\bdrop\s+column\b", "DROP COLUMN"),
    (r"\bdrop\s+schema\b", "DROP SCHEMA"),
    (r"\bdrop\s+database\b", "DROP DATABASE"),
    (r"\btruncate\b", "TRUNCATE"),
    (r"\bdelete\s+from\b", "DELETE FROM"),
    (r"\balter\s+column\b", "ALTER COLUMN"),
    (r"\brename\s+to\b", "RENAME TO"),
    (r"\brename\s+column\b", "RENAME COLUMN"),
    (r"\bset\s+not\s+null\b", "SET NOT NULL"),
    (r"\bdrop\s+constraint\b", "DROP CONSTRAINT"),
    (r"\bdrop\s+index\b", "DROP INDEX"),
    # Dropping a PERMISSIVE policy fails closed — it denies more, never less.
    # Dropping a RESTRICTIVE one does the opposite: restrictive policies are
    # AND-ed, so removing one LOOSENS access. The two are indistinguishable in
    # a `drop policy` statement, so the statement itself is forbidden and the
    # drop-then-create idiom below is the sanctioned exception.
    (r"\bdrop\s+policy\b", "DROP POLICY"),
]

# V1's tables. Adding a NOT NULL column to any of these, or otherwise tightening
# them, breaks V1 inserts that don't know the column exists.
V1_TABLES = {
    "profiles",
    "results",
    "usage",
    "score_history",
    "credits",
    "credit_ledger",
    "content_plan",
    "content_feedback",
    "post_history",
    "milestones",
    "jobs_feed",
    "jobs_cache",
    "job_applications",
    "application_events",
    "interview_preps",
    "feedback",
    "email_log",
    "digest_log",
    "payments",
    "subscriptions",
    "user_subscriptions",
}


def test_migrations_directory_exists() -> None:
    assert MIGRATIONS_DIR.is_dir(), f"missing {MIGRATIONS_DIR}"
    assert _sql_files(), "no V2_*.sql migrations found"


#: Declares that a dropped constraint was CREATED BY V2 and therefore is not
#: V1's to protect. The claim is VERIFIED below against the earlier migrations,
#: not taken on trust — an opt-out nobody checks is a comment.
V2_OWNED_CONSTRAINT = "carigma:v2-owned-constraint:"


def _v2_created_constraints(before: Path) -> set[str]:
    """Constraint names an EARLIER V2 migration adds.

    Only earlier ones: a migration cannot justify dropping something it creates
    later in the same run, and ordering is the whole point of the numbering.
    """
    names: set[str] = set()
    for path in _sql_files():
        if path.name >= before.name:
            continue
        sql = _strip_comments(path.read_text(encoding="utf-8")).lower()
        names.update(re.findall(r"add\s+constraint\s+([a-z0-9_]+)", sql))
    return names


@pytest.mark.parametrize("path", _sql_files(), ids=lambda p: p.name)
def test_migration_contains_no_destructive_statements(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    sql = _strip_comments(raw).lower()

    # `drop policy if exists` immediately followed by `create policy` is the
    # standard idempotent-policy idiom and only ever touches V2-owned policies.
    cleaned = re.sub(r"drop\s+policy\s+if\s+exists[^;]*;", " ", sql)

    # A constraint V2 itself added is not V1's to protect, and widening one is
    # how a V2-owned vocabulary grows. But "V2 owns it" is a CLAIM, so the
    # marker has to name the constraint and the name has to actually appear in
    # an earlier migration's `add constraint`. A marker that merely asserts
    # good intentions would be the vacuous-guard pattern again.
    claimed = {
        name.strip().lower()
        for name in re.findall(rf"{re.escape(V2_OWNED_CONSTRAINT)}\s*([a-z0-9_]+)", raw, re.I)
    }
    if claimed:
        actually_v2 = _v2_created_constraints(path)
        unverified = sorted(claimed - actually_v2)
        assert not unverified, (
            f"{path.name} claims V2 owns {', '.join(unverified)}, but no earlier "
            f"migration adds a constraint by that name. Either the name is wrong "
            f"or the constraint is V1's — in which case it must not be dropped."
        )
        for name in claimed:
            cleaned = re.sub(
                rf"drop\s+constraint\s+(if\s+exists\s+)?{re.escape(name)}\s*;", " ", cleaned
            )

    for pattern, label in FORBIDDEN:
        match = re.search(pattern, cleaned)
        assert match is None, (
            f"{path.name} contains {label} — migrations must be strictly additive "
            f"while V1 is live. Found near: "
            f"{cleaned[max(0, match.start() - 60) : match.start() + 60]!r}"
        )


@pytest.mark.parametrize("path", _sql_files(), ids=lambda p: p.name)
def test_new_columns_on_v1_tables_are_nullable(path: Path) -> None:
    """A NOT NULL column added to a live V1 table breaks every V1 insert."""
    sql = _strip_comments(path.read_text(encoding="utf-8"))

    for stmt in re.finditer(
        r"alter\s+table\s+(?:public\.)?(\w+)\s+add\s+column\s+(?:if\s+not\s+exists\s+)?([^;]+);",
        sql,
        flags=re.IGNORECASE,
    ):
        table, definition = stmt.group(1).lower(), stmt.group(2)
        if table not in V1_TABLES:
            continue
        assert not re.search(r"\bnot\s+null\b", definition, re.IGNORECASE), (
            f"{path.name}: column added to live V1 table '{table}' is NOT NULL — "
            f"V1 inserts don't know this column and would fail. Definition: {definition!r}"
        )


@pytest.mark.parametrize("path", _sql_files(), ids=lambda p: p.name)
def test_migration_is_idempotent(path: Path) -> None:
    """Re-running a migration must be safe — the runbook says paste-and-run, and
    someone will inevitably run it twice."""
    sql = _strip_comments(path.read_text(encoding="utf-8"))

    creates = re.findall(r"create\s+table\s+(?!if\s+not\s+exists)", sql, re.IGNORECASE)
    assert not creates, f"{path.name}: CREATE TABLE without IF NOT EXISTS"

    indexes = re.findall(
        r"create\s+(?:unique\s+)?index\s+(?!if\s+not\s+exists)", sql, re.IGNORECASE
    )
    assert not indexes, f"{path.name}: CREATE INDEX without IF NOT EXISTS"

    adds = re.findall(r"add\s+column\s+(?!if\s+not\s+exists)", sql, re.IGNORECASE)
    assert not adds, f"{path.name}: ADD COLUMN without IF NOT EXISTS"


# ── Row Level Security ─────────────────────────────────────────────────────
# Added after Supabase's own linter caught what this suite did not: V2_002
# created `credit_requests` and `admin_notes` in `public` with no RLS, leaving
# the founder's private notes about users readable by any `authenticated` key.
#
# The existing checks look for DESTRUCTIVE operations, which is what they were
# built for. "Creates a public table without RLS" is the same CLASS of mistake
# — a schema defect that ships silently — so it belongs here too.

#: Written on the line above (or beside) a CREATE TABLE to opt out, for tables
#: that are genuinely world-readable. Deliberately verbose: opting out of RLS
#: should be an act of typing, not a shrug.
RLS_OPT_OUT = "carigma:rls-exempt"

#: Declares "RLS on, no policy, service-role only — this is intended".
#: An explicit token rather than looking for the words "service role" in the
#: file. The first version did the latter and a probe file passed simply
#: because its own comment happened to contain the phrase; a guard that a
#: passing mention satisfies is not a guard.
RLS_SERVICE_ONLY = "carigma:rls-service-role-only"


def _created_tables(sql_with_comments: str) -> list[tuple[str, bool]]:
    """Every `create table [if not exists] public.X`, with its opt-out flag.

    Line-by-line rather than one regex over the whole file, because comments
    have to be treated differently from statements: the opt-out marker LIVES
    in a comment, but a comment must never be mistaken for a statement.

    The first version scanned raw SQL with a single regex and matched V2_001's
    own prose — the line `-- CREATE TABLE IF NOT EXISTS — new tables only`
    yielded a table called "if". Caught on the first run of this test.
    """
    out: list[tuple[str, bool]] = []
    marker_seen_recently = False
    blank_run = 0

    for line in sql_with_comments.splitlines():
        stripped = line.strip()

        if not stripped:
            blank_run += 1
            # Two blank lines end a comment block's association with whatever
            # follows, so a marker can't leak down the file.
            if blank_run >= 2:
                marker_seen_recently = False
            continue
        blank_run = 0

        if stripped.startswith("--"):
            if RLS_OPT_OUT in stripped:
                marker_seen_recently = True
            continue

        match = re.match(
            r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?(\w+)",
            stripped,
            flags=re.IGNORECASE,
        )
        if match:
            out.append((match.group(1).lower(), marker_seen_recently))
        marker_seen_recently = False

    return out


@pytest.mark.parametrize("path", _sql_files(), ids=lambda p: p.name)
def test_every_new_public_table_enables_rls(path: Path) -> None:
    """A table in `public` without RLS is readable with the anon key.

    Supabase exposes `public` over PostgREST, so "forgot to enable RLS" is not
    a hardening oversight — it is the table being published. `admin_notes`
    holds the founder's private observations about users; `credit_requests`
    holds their verbatim words. Neither is anyone's to browse.
    """
    raw = path.read_text(encoding="utf-8")
    stripped = _strip_comments(raw).lower()

    for table, exempt in _created_tables(raw):
        if exempt:
            continue
        enabled = re.search(
            rf"alter\s+table\s+(?:public\.)?{re.escape(table)}\s+enable\s+row\s+level\s+security",
            stripped,
        )
        assert enabled, (
            f"{path.name}: table '{table}' is created in public with NO row level "
            f"security. Supabase serves `public` over PostgREST, so anon and "
            f"authenticated keys can read it. Add:\n"
            f"    alter table public.{table} enable row level security;\n"
            f"plus a policy if users should see their own rows — or nothing at "
            f"all if it is service-role only. If the table really is meant to be "
            f"world-readable, write `{RLS_OPT_OUT}` in a comment above it and say "
            f"why."
        )


@pytest.mark.parametrize("path", _sql_files(), ids=lambda p: p.name)
def test_rls_without_policies_is_deliberate_not_forgotten(path: Path) -> None:
    """RLS on with no policy denies everyone — correct for service-role-only
    tables, and a silent outage for anything else. The two are
    indistinguishable from the SQL alone, so the intent has to be DECLARED.

    A missing policy is the dangerous direction precisely because it does not
    error: PostgREST returns an empty set, so a feature built on it looks like
    it works and shows nothing.
    """
    raw = path.read_text(encoding="utf-8")
    stripped = _strip_comments(raw).lower()

    for table, exempt in _created_tables(raw):
        if exempt:
            continue
        if not re.search(
            rf"alter\s+table\s+(?:public\.)?{re.escape(table)}\s+enable\s+row\s+level\s+security",
            stripped,
        ):
            continue  # the previous test already fails on this
        has_policy = re.search(
            rf"create\s+policy[^;]*\son\s+(?:public\.)?{re.escape(table)}\b", stripped
        )
        if has_policy:
            continue
        assert RLS_SERVICE_ONLY in raw, (
            f"{path.name}: '{table}' has RLS enabled and NO policy, which denies "
            f"anon and authenticated everything — silently, as an empty set "
            f"rather than an error. Declare it with `{RLS_SERVICE_ONLY}` in a "
            f"comment if intended, or add a policy. That is right for a "
            f"service-role-only table and a silent empty-set bug for anything "
            f"else. Say which, in a comment, near the RLS block."
        )


def test_no_blended_career_score_table() -> None:
    """The two platform scores must never share an axis or be averaged.

    They are stored in separate tables precisely so this mistake is structurally
    hard to make; this test guards the intent explicitly.
    """
    for path in _sql_files():
        sql = _strip_comments(path.read_text(encoding="utf-8")).lower()
        assert "combined_score" not in sql
        assert "career_score" not in sql
        assert "overall_score" not in sql


def test_the_state_enum_and_the_check_constraints_agree() -> None:
    """`ContractState` and the SQL that stores it, compared.

    `weekly_contracts_state_check` permitted 'expired' and 'lapsed' that the
    application could never write, and `weekly_review_state_check` — written
    three commits before this test — **omitted 'auto_adopted'**, the one state
    the Sunday sweep produces. It would have rejected every auto-adopted week.

    The enum's docstring said "keep them in step". That was the entire
    mechanism, and a comment is not a mechanism. This is.
    """
    import pathlib
    import re

    from carigma_api.services.posts import SlotState
    from carigma_api.services.weekly import ContractState

    # Each constraint against ITS OWN enum. The first version compared every
    # `*state_check` to `ContractState` and tripped on `content_loop`, which
    # stores slot states — a guard that fires on correct code gets deleted.
    owners: dict[str, set[str]] = {
        "weekly_contracts_state_check": {str(s) for s in ContractState},
        "weekly_review_state_check": {str(s) for s in ContractState},
        "content_loop_state_check": {str(s) for s in SlotState},
    }
    migrations = pathlib.Path(__file__).resolve().parents[1] / "migrations"

    # The LAST definition of each constraint wins, because a later migration
    # drops and replaces an earlier one. Comparing against the first would
    # check a constraint that is no longer in the database.
    latest: dict[str, set[str]] = {}
    for path in sorted(migrations.glob("*.sql")):
        for match in re.finditer(
            r"add constraint (\w*state_check)\s*\n?\s*check \(state in \(([^)]*)\)\)",
            path.read_text(encoding="utf-8"),
            re.I,
        ):
            latest[match.group(1)] = {v.strip().strip("'") for v in match.group(2).split(",")}

    assert latest, "no state check constraints found — this guard is checking nothing"
    unowned = sorted(set(latest) - set(owners))
    assert not unowned, (
        f"{unowned} has no enum to check against — add it to `owners` rather "
        f"than letting a constraint go uncompared"
    )

    for name, allowed in sorted(latest.items()):
        expected = owners[name]
        assert allowed == expected, (
            f"{name} permits {sorted(allowed)} but its enum is "
            f"{sorted(expected)}; missing={sorted(expected - allowed)} "
            f"unwritable={sorted(allowed - expected)}"
        )
