"""`scripts/landed.py` — is the work actually in the repository?

Every case runs against a real clone of a real bare `origin`, built in a temp
directory. The states are the three that came apart from "done" in one round:
uncommitted work, commits pushed to a branch but never merged, and a commit a
report named that did not exist anywhere.
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "landed", Path(__file__).resolve().parents[1] / "scripts" / "landed.py"
)
assert _SPEC and _SPEC.loader
landed = importlib.util.module_from_spec(_SPEC)
sys.modules["landed"] = landed
_SPEC.loader.exec_module(landed)


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


def _commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(message, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone on `p4-surfaces`, pushed, whose tree is exactly origin/main's."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, "app.txt", "scaffold")
    _git(work, "push", "-q", "-u", "origin", "main")
    _git(work, "checkout", "-q", "-b", "p4-surfaces")
    _git(work, "push", "-q", "-u", "origin", "p4-surfaces")
    _git(work, "fetch", "-q", "origin")
    return work


def _state(work: Path) -> landed.RepoState:
    # No fetch: the fixture's refs are already current, and the fetch path is
    # the same git call the real run makes.
    return landed.inspect("carigma-api", work, fetch=False)


def _headlines(state: landed.RepoState) -> list[str]:
    return [f.headline for f in state.findings if f.blocking]


def test_clean_pushed_and_on_main_has_landed(repo: Path) -> None:
    state = _state(repo)

    assert state.landed, _headlines(state)


def test_an_uncommitted_file_has_not_landed_and_is_named(repo: Path) -> None:
    (repo / "probe.py").write_text("written, never committed", encoding="utf-8")

    state = _state(repo)

    assert not state.landed
    finding = next(f for f in state.findings if f.headline.startswith("UNCOMMITTED"))
    assert any("probe.py" in line for line in finding.lines)


def test_a_commit_never_pushed_has_not_landed_and_is_named(repo: Path) -> None:
    _commit(repo, "guard.py", "rls: the guard")

    state = _state(repo)

    finding = next(f for f in state.findings if f.headline.startswith("UNPUSHED"))
    assert any("rls: the guard" in line for line in finding.lines)


def test_pushed_to_the_branch_but_not_merged_has_not_landed(repo: Path) -> None:
    """The unmerged-main incident: everything pushed, nothing on main."""
    _commit(repo, "fix.py", "onboarding: the fix")
    _git(repo, "push", "-q")

    state = _state(repo)

    assert not any(h.startswith("UNPUSHED") for h in _headlines(state))
    finding = next(f for f in state.findings if f.headline.startswith("NOT ON MAIN"))
    assert any("onboarding: the fix" in line for line in finding.lines)


def test_a_branch_synced_forward_to_main_is_a_note_not_missing_work(repo: Path) -> None:
    """Ravi's fast-forward: the local branch is ahead of its upstream by merges
    that main already has. Reporting those as unpushed work would be noise that
    trains people to skim past the one line that matters."""
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "merged.py", "merged elsewhere")
    _git(repo, "push", "-q")
    _git(repo, "checkout", "-q", "p4-surfaces")
    _git(repo, "merge", "-q", "--ff-only", "main")
    _git(repo, "fetch", "-q", "origin")

    state = _state(repo)

    assert state.landed, _headlines(state)
    assert any(not f.blocking and "already on origin/main" in f.headline for f in state.findings)


def test_a_branch_with_no_upstream_has_not_landed(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "never-pushed")

    state = _state(repo)

    assert any(h.startswith("NO UPSTREAM") for h in _headlines(state))


def test_a_stash_has_not_landed(repo: Path) -> None:
    (repo / "app.txt").write_text("edited then stashed", encoding="utf-8")
    _git(repo, "stash", "-q")

    state = _state(repo)

    assert any(h.startswith("STASHED") for h in _headlines(state))


# ── Named commits: checked, not believed ────────────────────────────────────


def test_a_commit_that_does_not_exist_is_reported_as_not_existing(repo: Path) -> None:
    claim = landed.locate("2b52996", (("carigma-api", repo),))

    assert claim.found_in is None
    assert "NOT FOUND" in claim.detail
    assert not claim.landed


def test_a_local_only_commit_is_distinguished_from_a_missing_one(repo: Path) -> None:
    sha = _commit(repo, "local.py", "local only")

    claim = landed.locate(sha[:7], (("carigma-api", repo),))

    assert claim.found_in == "carigma-api"
    assert claim.detail.startswith("LOCAL ONLY")


def test_a_commit_pushed_but_not_merged_says_where_it_is(repo: Path) -> None:
    sha = _commit(repo, "pushed.py", "pushed")
    _git(repo, "push", "-q")

    claim = landed.locate(sha[:7], (("carigma-api", repo),))

    assert "origin/p4-surfaces" in claim.detail
    assert not claim.landed


def test_a_commit_on_main_has_landed(repo: Path) -> None:
    sha = _git(repo, "rev-parse", "origin/main")

    assert landed.locate(sha[:7], (("carigma-api", repo),)).landed


# ── The report ───────────────────────────────────────────────────────────────


def test_the_report_prints_every_state_on_a_windows_console(repo: Path) -> None:
    """Strict cp1252, because `deployed.py` once crashed on a Windows console on
    exactly the verdicts it existed to deliver."""
    (repo / "dirty.py").write_text("x", encoding="utf-8")
    _commit(repo, "unpushed.py", "unpushed — with a dash in the subject")
    state = _state(repo)
    claims = [
        landed.locate("2b52996", (("carigma-api", repo),)),
        landed.locate("HEAD", (("carigma-api", repo),)),
    ]

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    passed = landed.report([state], claims, console)
    console.flush()

    assert passed is False
    assert b"NOT LANDED" in raw.getvalue()
