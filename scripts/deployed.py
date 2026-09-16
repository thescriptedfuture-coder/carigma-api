"""Is production running what `main` says? Both services, one command.

    ./run deployed

Exits 0 only when every service is built from `main` and runs `origin/main`'s
code. Run it FIRST in any round that tests the deployed build — before reading
a single finding.

## Why a script, and why it compares code rather than hashes

Rounds were lost twice diagnosing code that was not running, so both builds
learned to report their commit. The first real use of that compared the
reported commit with `main` by eye, saw two different hashes, and concluded
that production had never received the fixes.

The finding underneath was real — both Render services were building from
`p4-surfaces` while every merge went to `main`. The conclusion was not. Every
merge to `main` carried a tree IDENTICAL to the `p4-surfaces` commit it merged,
so the branch tip was `main`'s exact code under a different hash, and each time
the live bundle had been measured it was current with the branch. Production
was never behind `main`; if anything it was ahead of CI.

**A hash is not code.** A merge, a rebase or a cherry-pick changes the hash and
nothing else, so eyes comparing hashes report a difference that may not exist
— and "the fixes were never deployed" sends the next round chasing a pipeline
that was working. This compares TREES and names which case it found:

| verdict      | meaning                                              | passes |
|--------------|------------------------------------------------------|--------|
| ok           | production is origin/main                            | yes    |
| same code    | a different commit with exactly origin/main's tree   | yes    |
| behind       | an older main; lists what production lacks           | no     |
| not on main  | production runs commits main does not contain        | no     |
| wrong branch | built from a branch other than main, whatever the code | no   |
| unknown      | the build, or this checkout, cannot say              | no     |

`unknown` fails. A check that cannot see is not a check that passed.

## Why this is not a CI job

Render's "After CI checks pass" auto-deploy waits for EVERY GitHub check on a
commit before deploying it. A check that waits for the deploy would wait for a
deploy that is waiting for it, time out, fail — and a failed check means Render
never deploys. So this runs where nothing waits on it: here, at the start of a
round. The branch half does not depend on anyone remembering to run it: the
builds themselves refuse the wrong branch (`services/deploy.py`, and the web's
`buildIdentity`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from carigma_api.services.deploy import DEPLOY_BRANCH  # noqa: E402

TARGET = f"origin/{DEPLOY_BRANCH}"

#: How many missing or extra commits to print before summarising the rest.
SHOWN = 10


@dataclass(frozen=True)
class Service:
    name: str
    #: The public endpoint that reports `commit` and `branch`. Both are build
    #: identifiers, not configuration — see routes/health.py.
    url: str
    repo: Path


SERVICES = (
    Service("carigma-api", "https://carigma-api.onrender.com/health", ROOT),
    # The sibling checkout. This is an operator script on a laptop, never a
    # test: a test reading outside its repository is the bug CI caught twice.
    Service(
        "carigma-web", "https://carigma-web.onrender.com/build.json", ROOT.parent / "carigma-web"
    ),
)


@dataclass(frozen=True)
class Deployed:
    commit: str
    #: None when the build predates branch reporting — distinct from "unknown",
    #: which is a build that reports and could not say.
    branch: str | None


class Status(Enum):
    OK = "ok"
    SAME_CODE = "same code"
    BEHIND = "behind"
    NOT_ON_MAIN = "not on main"
    WRONG_BRANCH = "wrong branch"
    UNKNOWN = "unknown"


PASSING = frozenset({Status.OK, Status.SAME_CODE})


@dataclass(frozen=True)
class Verdict:
    status: Status
    summary: str
    detail: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status in PASSING


class Git:
    """The few questions this needs, asked of a real repository."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # S603/S607: a fixed argv with no shell, and `git` resolved from PATH
        # the same way the person running this resolves it.
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(self.repo), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )

    def fetch(self) -> str | None:
        """Refresh `origin`. The error text on failure, else None."""
        result = self._run("fetch", "--quiet", "origin")
        return None if result.returncode == 0 else (result.stderr.strip() or "git fetch failed")

    def resolve(self, ref: str) -> str | None:
        result = self._run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return result.stdout.strip() if result.returncode == 0 else None

    def tree(self, commit: str) -> str:
        return self._run("rev-parse", f"{commit}^{{tree}}").stdout.strip()

    def is_ancestor(self, older: str, newer: str) -> bool:
        result = self._run("merge-base", "--is-ancestor", older, newer)
        if result.returncode not in (0, 1):
            raise RuntimeError(f"git merge-base failed: {result.stderr.strip()}")
        return result.returncode == 0

    def commits(self, since: str, until: str) -> list[str]:
        """`since..until`, newest first, one line each.

        Without merges: a pull-request merge carries no change of its own, and
        listing five of them above the one commit that matters buries it.
        """
        out = self._run("log", "--no-merges", "--format=%h %s", f"{since}..{until}").stdout
        return [line for line in out.splitlines() if line]

    def files_differing(self, a: str, b: str) -> int:
        out = self._run("diff", "--name-only", a, b).stdout
        return len([line for line in out.splitlines() if line])


def parse_identity(payload: object) -> Deployed:
    """Read `/health` or `build.json`. Both carry `commit`; newer ones `branch`."""
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    commit = payload.get("commit")
    branch = payload.get("branch")
    return Deployed(
        commit=commit if isinstance(commit, str) and commit else "unknown",
        branch=branch if isinstance(branch, str) and branch else None,
    )


def _listed(lines: list[str]) -> tuple[str, ...]:
    if len(lines) <= SHOWN:
        return tuple(lines)
    return (*lines[:SHOWN], f"... and {len(lines) - SHOWN} more")


def judge(git: Git, deployed: Deployed, target: str = TARGET) -> Verdict:
    """Compare what a service reports with `target`, by content."""
    head = git.resolve(target)
    if head is None:
        return Verdict(Status.UNKNOWN, f"{target} does not resolve in {git.repo}")

    if deployed.commit == "unknown":
        return Verdict(
            Status.UNKNOWN,
            "the build does not say which commit it is (RENDER_GIT_COMMIT was not set), "
            "so nothing about it can be verified",
        )
    commit = git.resolve(deployed.commit)
    if commit is None:
        return Verdict(
            Status.UNKNOWN,
            f"production reports {deployed.commit}, which this repository does not contain "
            "even after fetching - a force-push, or a service pointed at another repository",
        )
    if deployed.branch == "unknown":
        return Verdict(
            Status.UNKNOWN,
            "the build does not say which branch it came from (RENDER_GIT_BRANCH was not set)",
        )

    short = commit[:12]
    same_code = git.tree(commit) == git.tree(head)
    differing = 0 if same_code else git.files_differing(commit, head)
    code = (
        f"the same code as {target}" if same_code else f"{differing} file(s) differ from {target}"
    )
    notes: tuple[str, ...] = ()
    if deployed.branch is None:
        notes = ("this build predates branch reporting, so only its code was compared",)

    if deployed.branch is not None and deployed.branch != DEPLOY_BRANCH:
        return Verdict(
            Status.WRONG_BRANCH,
            f"production {short} was built from '{deployed.branch}', not '{DEPLOY_BRANCH}' - {code}",
            (
                "Render dashboard > the service > Settings > Build & Deploy > Branch.",
                "A branch other than main reaches users before CI has judged it.",
            ),
        )

    if commit == head:
        return Verdict(Status.OK, f"production {short} is {target}", notes)

    if same_code:
        return Verdict(
            Status.SAME_CODE,
            f"production {short} is a different commit with exactly {target}'s code",
            notes,
        )

    if git.is_ancestor(commit, head):
        missing = git.commits(commit, head)
        return Verdict(
            Status.BEHIND,
            f"production {short} lacks {len(missing)} commit(s) on {target}; {code}. "
            "A deploy may still be building - check again before diagnosing anything.",
            (*notes, *_listed(missing)),
        )

    extra = git.commits(head, commit)
    return Verdict(
        Status.NOT_ON_MAIN,
        f"production {short} runs {len(extra)} commit(s) that {target} does not contain; {code}",
        (*notes, *_listed(extra)),
    )


def fetch_identity(url: str, timeout: float) -> Deployed:
    # A query string defeats any cache between here and the build. A free
    # Render instance may be asleep, hence the generous timeout.
    request = urllib.request.Request(  # noqa: S310
        f"{url}?check={int(time.time())}",
        headers={"User-Agent": "carigma-deployed-check", "Cache-Control": "no-cache"},
    )
    # S310: fixed https URLs declared in SERVICES above, never user input.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return parse_identity(json.load(response))


def check(service: Service, timeout: float) -> Verdict:
    if not (service.repo / ".git").exists():
        return Verdict(
            Status.UNKNOWN,
            f"no checkout at {service.repo}, so the deploy cannot be compared with anything",
        )
    git = Git(service.repo)
    # Comparing against a stale origin/main answers a question about the past.
    fetch_error = git.fetch()
    if fetch_error:
        return Verdict(Status.UNKNOWN, f"could not fetch origin: {fetch_error}")
    try:
        deployed = fetch_identity(service.url, timeout)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return Verdict(Status.UNKNOWN, f"could not read {service.url}: {exc}")
    return judge(git, deployed)


def report(verdicts: list[tuple[Service, Verdict]], out: TextIO) -> bool:
    """Print every verdict. True when all of them pass.

    Output is ASCII, and a test holds it to a strict cp1252 stream. The first
    version printed the dashboard hint with arrows, and on a Windows console
    that raised `UnicodeEncodeError` — so the check crashed on exactly the
    verdicts it exists to deliver, and printed cleanly only when all was well.
    """
    width = max(len(status.value) for status in Status)
    for service, verdict in verdicts:
        print(f"{service.name:<12} {verdict.status.value:<{width}}  {verdict.summary}", file=out)
        for line in verdict.detail:
            print(f"{'':<12} {'':<{width}}    {line}", file=out)

    passed = all(verdict.passed for _, verdict in verdicts)
    print(file=out)
    if passed:
        print(f"Production matches {TARGET}.", file=out)
    else:
        print(
            f"Production does NOT match {TARGET}. Resolve that before diagnosing anything.",
            file=out,
        )
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().splitlines()[0])
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Seconds to wait for each service. A sleeping free instance takes ~50.",
    )
    args = parser.parse_args()

    verdicts = [(service, check(service, args.timeout)) for service in SERVICES]
    return 0 if report(verdicts, sys.stdout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
