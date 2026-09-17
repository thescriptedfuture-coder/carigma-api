"""Is the work actually in the repository? All three repos, one command.

    ./run landed                  what is uncommitted, unpushed, not yet on main
    ./run landed 2b52996 acf663b  and does each of these commits exist, and where

Exits 0 only when every repository is clean, pushed, and on `origin/main`, and
every named commit exists on `origin/main`. Run it at the END of every round and
paste what it prints; `./run deployed` answers the next boundary out.

## Why

Three times in one round, "done" and "in the repository" came apart:

1. Render built from `p4-surfaces` while the work merged to `main`. (Deploy
   boundary: `./run deployed` catches it now.)
2. A round's commits sat on `p4-surfaces`, pushed, with `main` never merged.
   (Merge boundary.)
3. A report named commit `2b52996` — a probe, a guard and notes — as done. No
   such object existed in any of the three repositories, reachable or dangling,
   and no session on the machine had written it. (Commit boundary.)

Each was found by a person comparing hashes and file lists by hand, after the
fact. This makes the comparison mechanical, and makes a named commit something
to CHECK rather than to believe: a hash that does not exist is reported as not
existing, not assumed unpushed.

Not a CI job, for the same reason `deployed.py` is not: it describes a working
copy, and CI has only the commit it was given.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

API = Path(__file__).resolve().parents[1]

#: The three repositories a round can leave work in. The docs repo is V1's,
#: which tracks `v2/*.md` — CONTRIBUTING, the status anchor, the deploy guide.
REPOS: tuple[tuple[str, Path], ...] = (
    ("carigma-api", API),
    ("carigma-web", API.parent / "carigma-web"),
    ("carigma docs", API.parents[1]),
)

TARGET = "origin/main"
SHOWN = 8


class Git:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # S603/S607: fixed argv, no shell; `git` from PATH as the user runs it.
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(self.repo), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def lines(self, *args: str) -> list[str]:
        return [line for line in self.run(*args).stdout.splitlines() if line.strip()]

    def ok(self, *args: str) -> bool:
        return self.run(*args).returncode == 0


@dataclass
class Finding:
    headline: str
    lines: list[str] = field(default_factory=list)
    #: A note is reported and does not stop the work counting as landed.
    blocking: bool = True


@dataclass
class RepoState:
    name: str
    path: Path
    findings: list[Finding] = field(default_factory=list)
    branch: str = ""

    def problem(self, headline: str, lines: list[str] | None = None) -> None:
        self.findings.append(Finding(headline, _listed(lines or [])))

    def note(self, headline: str) -> None:
        self.findings.append(Finding(headline, blocking=False))

    @property
    def landed(self) -> bool:
        return not any(f.blocking for f in self.findings)


def _listed(lines: list[str]) -> list[str]:
    if len(lines) <= SHOWN:
        return lines
    return [*lines[:SHOWN], f"... and {len(lines) - SHOWN} more"]


def inspect(name: str, path: Path, *, fetch: bool = True) -> RepoState:
    state = RepoState(name=name, path=path)
    if not (path / ".git").exists():
        state.problem(f"no repository at {path}, so nothing about it can be said")
        return state
    git = Git(path)

    if fetch:
        result = git.run("fetch", "--quiet", "origin")
        if result.returncode != 0:
            # Comparing against stale remote refs would report the past.
            state.problem(f"could not fetch origin: {result.stderr.strip()[:120]}")
            return state

    state.branch = git.out("branch", "--show-current") or "(detached HEAD)"

    dirty = git.lines("status", "--porcelain")
    if dirty:
        state.problem(f"UNCOMMITTED: {len(dirty)} path(s)", dirty)

    stashes = git.lines("stash", "list")
    if stashes:
        state.problem(f"STASHED: {len(stashes)} stash(es) hold work no commit has", stashes)

    upstream = git.out("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not git.ok("rev-parse", "--verify", "--quiet", "@{u}"):
        state.problem(f"NO UPSTREAM: nothing on '{state.branch}' has been pushed anywhere")
    elif git.ok("rev-parse", "--verify", "--quiet", f"{TARGET}^{{commit}}"):
        ahead = git.lines("log", "--format=%h %s", "@{u}..HEAD")
        # Only changes that exist nowhere on the remote are work at risk. A
        # branch synced forward to main is "ahead" by merge commits that carry
        # nothing origin/main does not already have.
        # "neither origin/main nor origin/main" when a branch tracks main itself.
        where = TARGET if upstream == TARGET else f"{upstream} or {TARGET}"
        at_risk = git.lines("log", "--no-merges", "--format=%h %s", "@{u}..HEAD", "--not", TARGET)
        if at_risk:
            state.problem(f"UNPUSHED: {len(at_risk)} change(s) not on {where}", at_risk)
        elif ahead:
            state.note(f"ahead of {upstream} by {len(ahead)} commit(s), all already on {TARGET}")

    if not git.ok("rev-parse", "--verify", "--quiet", f"{TARGET}^{{commit}}"):
        state.problem(f"{TARGET} does not exist here")
        return state

    head_tree = git.out("rev-parse", "HEAD^{tree}")
    main_tree = git.out("rev-parse", f"{TARGET}^{{tree}}")
    if head_tree != main_tree:
        # By change, not by merge commit: a PR merge has no change of its own,
        # and listing merges buries the commit that is actually missing.
        missing = git.lines("log", "--no-merges", "--format=%h %s", f"{TARGET}..HEAD")
        if missing:
            state.problem(f"NOT ON MAIN: {len(missing)} change(s) {TARGET} lacks", missing)
        else:
            behind = git.lines("log", "--no-merges", "--format=%h %s", f"HEAD..{TARGET}")
            state.note(f"behind {TARGET} by {len(behind)} change(s); nothing is lost")
    return state


@dataclass(frozen=True)
class Claim:
    ref: str
    found_in: str | None
    detail: str

    @property
    def landed(self) -> bool:
        return self.found_in is not None and self.detail == f"on {TARGET}"


def locate(ref: str, repos: tuple[tuple[str, Path], ...]) -> Claim:
    """Where a named commit is — including nowhere."""
    for name, path in repos:
        if not (path / ".git").exists():
            continue
        git = Git(path)
        full = git.out("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if not full:
            continue
        if git.ok("merge-base", "--is-ancestor", full, TARGET):
            return Claim(ref, name, f"on {TARGET}")
        remotes = git.lines("branch", "-r", "--contains", full)
        if remotes:
            where = ", ".join(r.strip() for r in remotes)
            return Claim(ref, name, f"pushed to {where}, not on {TARGET}")
        return Claim(ref, name, "LOCAL ONLY: committed here and pushed nowhere")
    return Claim(
        ref,
        None,
        "NOT FOUND in any of the three repositories. It does not exist here; "
        "it was never committed on this machine, or the hash is wrong.",
    )


def report(states: list[RepoState], claims: list[Claim], out: TextIO) -> bool:
    """Print the ledger. True only when everything has landed.

    ASCII only, and held to a strict cp1252 stream by a test — `deployed.py`
    once crashed on a Windows console printing exactly its failing verdicts.
    """
    for state in states:
        verdict = "landed" if state.landed else "NOT LANDED"
        print(f"{state.name:<13} {verdict:<11} branch {state.branch or '-'}", file=out)
        for finding in state.findings:
            mark = "-" if finding.blocking else "note:"
            print(f"{'':<13} {mark} {finding.headline}", file=out)
            for line in finding.lines:
                print(f"{'':<13}     {line}", file=out)
    for claim in claims:
        where = claim.found_in or "-"
        print(f"commit {claim.ref:<12} {where:<13} {claim.detail}", file=out)

    landed = all(s.landed for s in states) and all(c.landed for c in claims)
    print(file=out)
    if landed:
        print(f"Everything is committed, pushed and on {TARGET}.", file=out)
    else:
        print("Not everything has landed. The lines above say what, and where.", file=out)
    return landed


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().splitlines()[0])
    parser.add_argument("commits", nargs="*", help="commit hashes a report says exist")
    parser.add_argument("--no-fetch", action="store_true", help="skip `git fetch` (offline)")
    args = parser.parse_args()

    states = [inspect(name, path, fetch=not args.no_fetch) for name, path in REPOS]
    claims = [locate(ref, REPOS) for ref in args.commits]
    return 0 if report(states, claims, sys.stdout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
