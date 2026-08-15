"""What the user has left.

    GET /credits    free

Free, obviously — charging to look at your balance would be absurd. It is
stated because every other read in this codebase now says so explicitly, and
an unstated rule is one somebody can be talked out of.

## Cannot read is not zero

`read_balance` returns `int | None`, and `None` is passed straight through as
`balance: null` with `known: false`. Reporting `0` for a failed read would tell
a user with credits that they have none, and the surface would then refuse them
work they have already paid for.

The same conflation exists one level down and is NOT this endpoint's to fix:
`SupabaseCreditStore.read_balance` returns `None` both when the read fails and
when the user has no `credits` row at all. Eleven of the live users have a row;
whether a brand-new account gets one at signup is V1's business. Either way the
honest answer here is the same — we do not know your balance — so the endpoint
is correct today and the ambiguity is recorded rather than papered over.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services.repository import SupabaseCreditStore, user_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["credits"])


def _store(request: Request, settings: Settings) -> SupabaseCreditStore:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return SupabaseCreditStore(user_client(settings, token))


@router.get("/credits")
def get_balance(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    balance = _store(request, settings).read_balance(user.id)
    return {
        "balance": balance,
        # Carried explicitly so no surface has to infer it from `balance ===
        # null`. Two places inferring the same thing eventually spell it
        # differently, and one of the spellings will be `?? 0`.
        "known": balance is not None,
    }


__all__ = ["router"]
