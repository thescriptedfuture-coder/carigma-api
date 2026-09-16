"""`scripts/deployed.py` — is production running what `main` says?

Every case runs against a REAL git repository built in a temp directory, shaped
like the history that produced the bug:

    main:  A ─────────── M        M merges p4 with --no-ff, so its tree is B's
            \\           /
    p4:      B ─────────┘── C     C lands on the branch after the merge

A fake git would answer whatever the test expected of it. The question here is
what git itself says about trees and ancestry, and only git can refuse.

The case that matters most is `B` reported from `p4-surfaces`. That is what the
build said on its first real use, and read by eye as "production never got the
fixes". It had `main`'s exact code. The verdict has to say BOTH things — wrong
branch, same code — because each alone sends the next round the wrong way.
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# The check is a script, not a package module.
_SPEC = importlib.util.spec_from_file_location(
    "deployed", Path(__file__).resolve().parents[1] / "scripts" / "deployed.py"
)
assert _SPEC and _SPEC.loader
deployed = importlib.util.module_from_spec(_SPEC)
sys.modules["deployed"] = deployed
_SPEC.loader.exec_module(deployed)

Deployed = deployed.Deployed
Status = deployed.Status


@dataclass(frozen=True)
class History:
    repo: Path
    a: str
    b: str
    m: str
    c: str


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str, body: str, message: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def history(tmp_path_factory: pytest.TempPathFactory) -> History:
    repo = tmp_path_factory.mktemp("deployed-history")
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "app.txt", "v1\n", "A: the scaffold")

    _git(repo, "checkout", "-q", "-b", "p4-surfaces")
    b = _commit(repo, "app.txt", "v2\n", "B: the fix")

    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "p4-surfaces", "-m", "M: Merge pull request #1")
    m = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "p4-surfaces")
    c = _commit(repo, "extra.txt", "unmerged\n", "C: pushed, not yet merged")
    _git(repo, "checkout", "-q", "main")

    return History(repo=repo, a=a, b=b, m=m, c=c)


def _judge(history: History, commit: str, branch: str | None) -> deployed.Verdict:
    # `main` stands in for `origin/main`: the comparison is identical, and a
    # remote would only add a fetch this test is not about.
    return deployed.judge(deployed.Git(history.repo), Deployed(commit[:12], branch), target="main")


def test_the_fixture_has_the_shape_of_the_bug(history: History) -> None:
    """Precondition. If the merge were a fast-forward, M would BE B and the
    central case below would prove nothing."""
    assert history.m != history.b
    assert _git(history.repo, "rev-parse", f"{history.m}^{{tree}}") == _git(
        history.repo, "rev-parse", f"{history.b}^{{tree}}"
    )


# ── The case that was misread ───────────────────────────────────────────────


def test_the_branch_tip_is_the_wrong_branch_and_the_same_code(history: History) -> None:
    verdict = _judge(history, history.b, "p4-surfaces")

    assert verdict.status is Status.WRONG_BRANCH
    assert not verdict.passed
    assert "p4-surfaces" in verdict.summary
    assert "the same code as main" in verdict.summary, (
        "a wrong-branch verdict that does not say whether the CODE differs is "
        "exactly the reading that cost a round: different hash, assumed stale"
    )


def test_a_wrong_branch_with_different_code_says_the_code_differs(history: History) -> None:
    verdict = _judge(history, history.c, "p4-surfaces")

    assert verdict.status is Status.WRONG_BRANCH
    assert "differ from main" in verdict.summary


def test_the_same_code_under_a_different_hash_passes_and_says_so(history: History) -> None:
    verdict = _judge(history, history.b, "main")

    assert verdict.status is Status.SAME_CODE
    assert verdict.passed


# ── Ordinary states ──────────────────────────────────────────────────────────


def test_production_at_main_is_ok(history: History) -> None:
    verdict = _judge(history, history.m, "main")

    assert verdict.status is Status.OK
    assert verdict.passed


def test_an_older_main_is_behind_and_names_what_it_lacks(history: History) -> None:
    verdict = _judge(history, history.a, "main")

    assert verdict.status is Status.BEHIND
    assert not verdict.passed
    assert any("B: the fix" in line for line in verdict.detail)
    assert not any("Merge pull request" in line for line in verdict.detail), (
        "a merge carries no change of its own; listing it buries the commit that matters"
    )


def test_commits_main_does_not_contain_are_not_on_main(history: History) -> None:
    verdict = _judge(history, history.c, "main")

    assert verdict.status is Status.NOT_ON_MAIN
    assert not verdict.passed
    assert any("C: pushed, not yet merged" in line for line in verdict.detail)


# ── A check that cannot see has not passed ───────────────────────────────────


def test_a_build_that_cannot_name_its_commit_fails(history: History) -> None:
    verdict = _judge(history, "unknown", "main")

    assert verdict.status is Status.UNKNOWN
    assert not verdict.passed


def test_a_build_that_cannot_name_its_branch_fails(history: History) -> None:
    verdict = _judge(history, history.m, "unknown")

    assert verdict.status is Status.UNKNOWN
    assert not verdict.passed


def test_a_commit_the_repository_does_not_have_fails(history: History) -> None:
    verdict = _judge(history, "deadbeefdead", "main")

    assert verdict.status is Status.UNKNOWN
    assert not verdict.passed


def test_a_build_from_before_branch_reporting_is_judged_on_code_and_says_so(
    history: History,
) -> None:
    """The builds live when this was written report no branch. They are
    judged on code, and the verdict says the branch was not checked."""
    verdict = _judge(history, history.m, None)

    assert verdict.status is Status.OK
    assert any("predates branch reporting" in line for line in verdict.detail)


# ── Reading what the services send ───────────────────────────────────────────


def test_health_and_build_json_are_both_read() -> None:
    health = deployed.parse_identity(
        {
            "status": "ok",
            "service": "carigma-api",
            "version": "0.1.0",
            "commit": "1eb6b9f599d8",
            "branch": "main",
        }
    )
    build = deployed.parse_identity(
        {"commit": "7e51d3c77816", "branch": "main", "builtAt": "2026-09-16T14:43:32Z"}
    )

    assert health == Deployed("1eb6b9f599d8", "main")
    assert build == Deployed("7e51d3c77816", "main")


def test_a_missing_commit_reads_as_unknown_not_as_blank() -> None:
    assert deployed.parse_identity({"branch": "main"}).commit == "unknown"
    assert deployed.parse_identity({"commit": ""}).commit == "unknown"


def test_a_missing_branch_is_distinct_from_an_unknown_one() -> None:
    assert deployed.parse_identity({"commit": "abc"}).branch is None
    assert deployed.parse_identity({"commit": "abc", "branch": "unknown"}).branch == "unknown"


def test_a_non_object_response_is_refused() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        deployed.parse_identity(["not", "an", "object"])


# ── The report ───────────────────────────────────────────────────────────────


def test_every_verdict_prints_on_a_windows_console(history: History) -> None:
    """The first version printed arrows in the dashboard hint, and a cp1252
    console raised on them — so it crashed on the failing verdicts and printed
    cleanly only when everything passed. `strict` is the point: `replace` would
    turn the crash into mojibake and hide the regression."""
    verdicts = [
        _judge(history, history.b, "p4-surfaces"),
        _judge(history, history.c, "p4-surfaces"),
        _judge(history, history.b, "main"),
        _judge(history, history.m, "main"),
        _judge(history, history.a, "main"),
        _judge(history, history.c, "main"),
        _judge(history, "unknown", "main"),
        _judge(history, history.m, None),
    ]
    assert {v.status for v in verdicts} == set(Status), "every status must be exercised"

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    service = deployed.Service("carigma-api", "https://example.invalid/health", history.repo)

    passed = deployed.report([(service, v) for v in verdicts], console)
    console.flush()

    assert passed is False
    assert b"does NOT match" in raw.getvalue()


def test_the_report_passes_only_when_every_service_passes(history: History) -> None:
    service = deployed.Service("carigma-api", "https://example.invalid/health", history.repo)
    ok = _judge(history, history.m, "main")
    behind = _judge(history, history.a, "main")

    assert deployed.report([(service, ok), (service, ok)], io.StringIO()) is True
    assert deployed.report([(service, ok), (service, behind)], io.StringIO()) is False
