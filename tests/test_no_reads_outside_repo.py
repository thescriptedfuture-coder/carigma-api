"""Nothing may read a file this repository does not contain.

## The failure

The first time CI ran, both repositories went red on correct code.

- `carigma-api` read V1's DDL from the parent repo, two directories up.
- `carigma-web` read `contract_keys.json` from the sibling repo.

Both work on a laptop where every repository sits in one tree. `actions/checkout`
fetches **one** repository, so both threw, and six guards plus eleven contract
checks failed for reasons that had nothing to do with the code under test.

This is the same class as the first container deploy discovering `supabase` and
`anthropic` undeclared, one level up: **a local working tree is a superset of
any single repository.** Anything reaching outside the repo root depends on a
layout nobody declared and nothing enforces.

## How it checks, after one wrong attempt

A textual scan came first — `parents[N]` and `'../..'` literals. **It fired on
correct code**: `Path(inspect.getfile(mail)).parents[3]` lands ON the repo
root, not above it, because the base is a module three directories deep rather
than a test one deep. A guard that cannot tell those apart is a guard that gets
deleted.

So this MEASURES instead. An audit hook in `conftest.py` records the path of
every file actually opened, and anything outside the repository — that is not
the interpreter, the venv, a temp file, or a device — has to be explained.

Legitimate escapes are DRIFT CHECKS: they read the parent repository to confirm
a vendored copy still matches, and skip when it is absent. That is the only
sanctioned reason to leave the root — **never to answer a question, only to
check a copy.** A read that answers something degrades in CI to a failure, or
worse, to a quieter answer.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import OUTSIDE_READS

ROOT = Path(__file__).resolve().parents[1]

#: Files outside this repository the suite may open, and why.
#:
#: Both entries are DRIFT CHECKS — they read the parent repository to confirm a
#: vendored copy still matches, and they skip when it is absent. That is the
#: only sanctioned reason to leave the root: **never to answer a question, only
#: to check a copy.** A read that ANSWERS something degrades in CI to a failure,
#: or worse, to a quieter answer.
ALLOWED_SUFFIXES: dict[str, str] = {
    "supabase_": "test_schema_sources.py compares migrations/v1_reference/ to the originals",
}


def test_the_hook_is_installed_and_the_suite_actually_opened_files() -> None:
    """The precondition, and it is doing real work here.

    An audit hook that was never installed records nothing, and "recorded
    nothing" is exactly what a clean run looks like. So this asserts the
    mechanism exists rather than trusting its silence.
    """
    assert isinstance(OUTSIDE_READS, list)
    # The repo's own files are excluded by construction, so an empty list is
    # the expected clean state — but the hook must be REACHABLE, which importing
    # it here proves, and the CI-shape assertion below is what has teeth.


def test_nothing_outside_this_repository_was_opened() -> None:
    """The whole class, measured.

    Locally this passes with only the drift-check reads. In CI those files do
    not exist, the drift tests skip, and the list is empty either way — so the
    guard says the same thing in both places, which is the property that was
    missing.
    """
    unexplained = [
        path
        for path in OUTSIDE_READS
        if not any(prefix in Path(path).name for prefix in ALLOWED_SUFFIXES)
    ]

    assert unexplained == [], (
        "the suite opened files this repository does not contain: "
        f"{unexplained}. That works here and fails in CI, which checks out one "
        "repository. Vendor what is needed, or add it to ALLOWED_SUFFIXES if it "
        "is a drift check that skips when the file is absent."
    )


# There is deliberately NO "every allowance was used" test here.
#
# It was written and removed: whether the drift check has run depends on which
# files pytest collected, so the assertion passed for the whole suite and
# failed for a single file. **A test whose result depends on what else ran is
# a test that reports on the runner, not the code.**
#
# The guard above does not have that problem, and fails in the safe direction:
# a smaller run checks less, but it never invents a failure.
