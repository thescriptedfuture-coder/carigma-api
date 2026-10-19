"""The run protocol's read side — `GET /agents/runs/{id}`.

`services/runs` was built for a client that starts a run and asks about it:
`replay_or_none` already said "the client polls the run protocol", and
`execute`'s done event already pointed at this path. Nothing served it, so
every agent answered by holding one request open for the length of the run —
and on 19 Oct three Profile Analyst runs succeeded, were charged, and reached
no page.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services.repository import user_client
from carigma_api.services.run_store import SupabaseRunStore
from carigma_api.services.runs import RunStoreUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])

_NO_SUCH_RUN = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail={"error": "no_such_run", "message": "We couldn't find that run."},
)


def _store(request: Request, settings: Settings) -> SupabaseRunStore:
    """`agent_runs` through the caller's own client, so RLS answers first."""
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return SupabaseRunStore(user_client(settings, token))


@router.get("/agents/runs/{run_id}")
def get_run(
    run_id: str,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """A run as it stands: status, steps, result, credits. Free.

    **Another user's run is a 404, not a 403.** A 403 would confirm the id
    exists. RLS already returns no row for it; the check below is the second
    lock, so the answer does not depend on a policy alone.

    A read that fails is a 503, never a 404: "could not look" told as "no
    such run" would make a polling page stop watching a run still going.

    `credits.charged` is the charge this run made, read from its row, and
    `balance_after` is `null` — the balance belongs to the ledger, and a copy
    kept on the run would be stale by the next spend.
    """
    try:
        UUID(run_id)
    except ValueError:
        # An id that cannot exist is "no such run", not a validation error to
        # show anyone.
        raise _NO_SUCH_RUN from None

    try:
        run = _store(request, settings).get(run_id)
    except RunStoreUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "read_failed",
                "message": "We couldn't check on that run just now.",
            },
        ) from exc

    if run is None or run.user_id != user.id:
        if run is not None:
            logger.warning("run %s requested by %s, owned by another user", run_id, user.id)
        raise _NO_SUCH_RUN
    return run.as_dict()
