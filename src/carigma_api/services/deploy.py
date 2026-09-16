"""Where production comes from — declared here, checked by the build itself.

## The bug this exists for

Both Render services deployed from `p4-surfaces` while every merge went to
`main`. The branch is a dashboard setting: nothing in this repository recorded
it, nothing the build emitted reported it, and it disagreed with where the work
happened for weeks without saying so.

It happened not to withhold code — every merge to `main` carried a tree
identical to the `p4-surfaces` commit it merged, so production was never
behind. What it did do is put code in front of users BEFORE CI had judged it:
a push to the branch deployed, and the pull request went red afterwards.

So the repository now declares the branch, and a production process started
from any other one refuses — the same move as the JWKS and service-key checks
in `main.py`. A refused deploy leaves the previous build serving and makes
Render send its failure notice, which is the first time this setting has ever
been able to say anything.
"""

from __future__ import annotations

from carigma_api.config import Settings

#: The only branch production builds from. A second deployed branch (staging,
#: say) is a decision to make here, in a commit, not in a dashboard.
DEPLOY_BRANCH = "main"


def short_commit(settings: Settings) -> str:
    """The deployed commit, short. `unknown` when nothing set it.

    Deliberately no fallback to reading `.git`: the container has no
    repository, so it would only ever succeed on a laptop and would answer
    confidently about the wrong thing.
    """
    sha = settings.render_git_commit
    return sha[:12] if sha else "unknown"


def branch(settings: Settings) -> str:
    """The branch this build came from. `unknown` when nothing set it."""
    return settings.render_git_branch or "unknown"


def wrong_branch(settings: Settings) -> str | None:
    """Why this process must not serve production, or None if it may.

    Refuses only on POSITIVE evidence of the wrong branch:

    - Outside production nothing is refused. CI and laptops have no branch
      variable, and a staging service is not production.
    - A pull-request preview carries its PR's branch by definition, and Render
      copies the parent's `ENVIRONMENT` onto it. Refusing there would make every
      preview an outage for a reason that is not a defect.
    - An EMPTY branch means the process is not on Render at all. That is
      reported (`/health` says `unknown`, and `./run deployed` fails on it)
      rather than refused — Render always sets the variable, so an empty one is
      a different problem from a wrong one and should not look like it.
    """
    actual = settings.render_git_branch
    if not settings.is_production or settings.is_pull_request or not actual:
        return None
    if actual == DEPLOY_BRANCH:
        return None
    return (
        f"This build came from branch '{actual}', and production deploys only from "
        f"'{DEPLOY_BRANCH}' (carigma_api/services/deploy.py). The branch is a Render "
        f"dashboard setting: Settings > Build & Deploy > Branch. Refusing to start: "
        f"a branch other than '{DEPLOY_BRANCH}' reaches users before CI has judged it."
    )
