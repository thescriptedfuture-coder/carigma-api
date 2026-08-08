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


@pytest.mark.parametrize("path", _sql_files(), ids=lambda p: p.name)
def test_migration_contains_no_destructive_statements(path: Path) -> None:
    sql = _strip_comments(path.read_text(encoding="utf-8")).lower()

    # `drop policy if exists` immediately followed by `create policy` is the
    # standard idempotent-policy idiom and only ever touches V2-owned policies.
    sql_without_policy_idiom = re.sub(r"drop\s+policy\s+if\s+exists[^;]*;", " ", sql)

    for pattern, label in FORBIDDEN:
        match = re.search(pattern, sql_without_policy_idiom)
        assert match is None, (
            f"{path.name} contains {label} — migrations must be strictly additive "
            f"while V1 is live. Found near: "
            f"{sql_without_policy_idiom[max(0, match.start() - 60) : match.start() + 60]!r}"
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
