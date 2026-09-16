"""Health checks — the only unauthenticated routes in the service.

## Why this reports a commit

Twice now, a round of fixes has been deployed and the reported behaviour has
not changed, and the first question both times was **"which build is actually
running?"** Answering it took a session each time: fetching the web bundle and
grepping it for strings that only exist in a given commit.

That method has a hard limit, and it was reached. A change whose only
difference is CONTROL FLOW — `if (isError) redirect` replacing
`if (isError) render` — leaves no string in a minified bundle. There is nothing
to grep for, so the question becomes unanswerable exactly when it matters most.

So the build identifies itself. `RENDER_GIT_COMMIT` is set by Render on every
deploy; locally it is absent and this says so rather than guessing.

**It is a build identifier, not configuration.** A commit SHA is public in any
repository anyone can read, and it says nothing about keys, hosts or users —
the same reason the version was already here. Nothing else joins it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from carigma_api.config import Settings, get_settings

router = APIRouter(tags=["health"])


def _commit(settings: Settings) -> str:
    """The deployed commit, short. `unknown` when nothing set it.

    Read through `Settings`, not `os.getenv` — the first version reached for
    the environment directly and `test_env_example.py` caught it. Every setting
    goes through one place so that `.env.example` documents the whole surface,
    which is the rule that keeps a deploy from depending on a variable nobody
    wrote down.

    Deliberately no fallback to reading `.git`: the container has no
    repository, so it would only ever succeed on a laptop and would answer
    confidently about the wrong thing.
    """
    sha = settings.render_git_commit
    return sha[:12] if sha else "unknown"


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    #: The commit this process was built from, so "is the fix deployed" is one
    #: request rather than an afternoon.
    commit: str


@router.get("/health", response_model=HealthResponse)
async def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    """Liveness probe. Deliberately leaks no configuration detail."""
    return HealthResponse(
        status="ok",
        service="carigma-api",
        version="0.1.0",
        commit=_commit(settings),
    )
