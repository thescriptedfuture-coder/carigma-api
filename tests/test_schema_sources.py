"""The vendored V1 schema must be complete, and must still be a copy.

## Why the DDL is vendored at all

`tests/schema.py` used to read V1's `supabase_*.sql` from the parent
repository. That works on a laptop where every repo sits in one tree, and it
**failed the first time CI ran**, because `actions/checkout` fetches one
repository. Six guards went red on code that was correct.

Same class as the first container deploy discovering `supabase` and `anthropic`
undeclared: **a local working tree is a superset of any single repository**, and
anything that reads outside its own repo is relying on a layout nobody
declared. `carigma-web` had the identical bug in `src/lib/contract.test.ts`.

## The two risks of a copy, and what covers each

**It goes stale.** `test_the_vendored_copies_match_the_parent_repository` reads
the originals whenever the parent repo is present — every local run, never in
CI — and compares bytes. So drift is caught where the originals exist, which is
also the only place anyone could have caused it.

**It goes missing, and the guards quietly narrow.** That is the dangerous one,
because a schema reader that finds nothing reports the same green as one that
finds no problems. `test_every_table_we_write_to_resolves` names the tables and
fails loudly if any stops resolving — it is the assertion that made this
failure legible in the first place.
"""

from __future__ import annotations

import pytest

from tests.schema import PARENT_REPO, SOURCES_FOUND, V1_REFERENCE, types_of

#: Every table `src/` or `scripts/` writes to, from the AST sweep in
#: `test_write_shapes.py`. Named rather than derived: this is the list that
#: must RESOLVE, and deriving it from the same sweep that consumes it would
#: make an empty sweep prove itself.
TABLES_WE_WRITE = [
    "agent_runs",
    "admin_notes",
    "credit_ledger",
    "credit_requests",
    "credits",
    "digest_log",
    "email_log",
    "payments",
    "profiles",
    "reengagement_sequences",
    "score_history",
    "user_subscriptions",
]


def test_the_vendored_directory_is_populated() -> None:
    assert V1_REFERENCE.is_dir(), f"{V1_REFERENCE} is missing — V1's DDL is not vendored"
    assert len(list(V1_REFERENCE.glob("*.sql"))) >= 15
    assert SOURCES_FOUND >= 30, f"only {SOURCES_FOUND} migration files readable"


@pytest.mark.parametrize("table", TABLES_WE_WRITE)
def test_every_table_we_write_to_resolves(table: str) -> None:
    """The loud failure that replaces a silent narrowing.

    `types_of` returns `{}` for a table whose CREATE it cannot find, and
    callers `skip` on that. A skip is invisible in a green run, so without this
    the guards would shrink to nothing the next time the vendored files move.
    """
    columns = types_of(table)
    assert columns, (
        f"no `create table public.{table}` in migrations/ or migrations/v1_reference/. "
        "Every guard that checks a write to this table is now skipping."
    )
    assert "user_id" in columns or "id" in columns


def test_the_two_columns_that_caused_this_are_still_what_we_think() -> None:
    """The specific pair, pinned. Both bugs were a value the column refused."""
    assert types_of("agent_runs")["id"] == "uuid"
    assert types_of("digest_log")["sent_on"] == "date"


@pytest.mark.skipif(
    PARENT_REPO is None or not PARENT_REPO.is_dir(),
    reason="the parent repository is not checked out — this is CI, and the copy is all there is",
)
def test_the_vendored_copies_match_the_parent_repository() -> None:
    """A copy nobody compares is a second thing to be wrong.

    Byte-for-byte, both directions: a vendored file that has diverged fails,
    and a V1 migration that was never copied in fails too. The second is the
    likelier one — adding a migration upstream and forgetting this directory.
    """
    assert PARENT_REPO is not None
    originals = {p.name: p for p in PARENT_REPO.glob("supabase_*.sql")}
    if not originals:
        pytest.skip("parent repo present but holds no supabase_*.sql — nothing to compare")

    vendored = {p.name: p for p in V1_REFERENCE.glob("*.sql")}

    missing = sorted(originals.keys() - vendored.keys())
    assert missing == [], (
        f"V1 migrations that were never vendored: {missing}. "
        f"Copy them into {V1_REFERENCE.name}/ — CI can only see what is committed here."
    )

    extra = sorted(vendored.keys() - originals.keys())
    assert extra == [], f"vendored files with no original upstream: {extra}"

    changed = sorted(
        name for name, path in vendored.items() if path.read_bytes() != originals[name].read_bytes()
    )
    assert changed == [], (
        f"vendored copies that no longer match the parent repository: {changed}. "
        "These are read-only copies; re-copy rather than edit."
    )
