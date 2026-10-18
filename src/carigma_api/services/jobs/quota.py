"""The shared JSearch quota: a hard daily cap, counted where the call happens.

## Three versions, and the one lesson under all of them

`config.py` has said since the beginning that the JSearch quota is "handled by
the jobs cache + daily cap". It was not, three times over:

1. **S5** built the count and the cap. Nothing enforced them.
2. **S6** enforced the cap and never recorded a call, so the count read zero.
3. **0.063** recorded every call — through the USER's client, into
   `jobs_cache`, which V2_007 had made service-role only. The database refused
   every write (42501, swallowed by a writer that "never raises") and showed
   the user zero rows on every read. Zero rows is not an error, so the count
   read 0 through three real scans and the cap refused nothing.

V1 wrote the lesson into `resumeiq/db.py` in August, and it was ported without
being read: "An anonymous count under RLS succeeds and returns 0 — no error to
catch." **A read the database is not letting you make does not fail. It
returns nothing, and nothing counts as zero.**

## Where the count lives now

`jobs_cache` stays closed. It holds payloads other users are served, and a
table any signed-in user can write is one any signed-in user can fill with a
fake posting and a phishing apply link.

So V2 does not touch it. Instead (V2_018):

- every V2 provider call leaves a row in `jobs_provider_calls`, written as the
  caller, owner-scoped, carrying no payload — nothing in it to poison;
- the count is `jsearch_calls_today()`, a SECURITY DEFINER function that adds
  V1's fetches today to V2's calls today and returns one integer.

## What V2 gave up

The shared payload cache. A user's own repeat search is already stopped by the
six-hour gate, and serving one user's results to another needs a writer the
database can trust — the server holding the service key, which this project
keeps out of user requests. At this scale two users rarely search the same
role and city inside 48 hours. Revisit when they do; the decision is Ravi's,
because it is a decision about where the service key may go.

## Cannot count is not counted zero

`calls_today` returns `int | None`, and `None` REFUSES. `0` grants permission
to call a paid API that belongs to somebody else.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from carigma_api.services.jobs.adapter import ProviderUnavailable
from carigma_api.services.jobs.models import NormalizedJob

logger = logging.getLogger(__name__)

#: One row per V2 provider call. Owner-scoped; no payload.
LEDGER = "jobs_provider_calls"

#: SECURITY DEFINER: V1's fetches today plus V2's calls today, as one integer.
COUNTER = "jsearch_calls_today"

#: The provider whose quota is shared with live V1 users. Adzuna has its own
#: free tier; its calls are recorded too, but they are not what the cap counts.
CAPPED_SOURCE = "jsearch"


def query_key(*, role: str, city: str, source: str) -> str:
    """One stable, readable key per (role, city, source).

    Hashed in part because a role title can carry anything — `Business
    Analyst — ERP & Public Finance / MoD` is a real one. A readable prefix is
    kept so a person reading the ledger can tell what was searched.
    """
    normalised = f"{source}|{role.strip().lower()}|{city.strip().lower()}"
    digest = hashlib.sha256(normalised.encode()).hexdigest()[:16]
    return f"{source}:{role.strip().lower()[:40]}:{digest}"


def calls_today(db: Any) -> int | None:
    """JSearch calls since midnight UTC, V1 and V2 together — or `None`.

    **`None` is not zero.** Anything that is not a plain integer is `None`: a
    missing function, a refused call, a shape we did not expect. A `bool` is
    refused explicitly, because in Python `True` is an `int` and would count
    as one call.
    """
    try:
        res = db.rpc(COUNTER, {}).execute()
    except Exception:
        logger.exception("could not count today's %s calls", CAPPED_SOURCE)
        return None
    data = res.data if res is not None else None
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if isinstance(data, bool) or not isinstance(data, int):
        logger.error("the %s counter returned %r, which is not a count", COUNTER, data)
        return None
    return data


def refusal(db: Any, *, cap: int, now: datetime | None = None) -> str | None:
    """Why this scan must not call the provider, or `None` to go ahead.

    A sentence rather than a bool, because the caller has to tell somebody.
    """
    used = calls_today(db)
    if used is None:
        return (
            "We couldn't check today's search quota, so we didn't spend any of it. "
            "Nothing was charged."
        )
    if used >= cap:
        # When it resets, said relatively: "midnight UTC" is 05:30 for most of
        # the people reading this, and the sentence should not need a sum.
        moment = now or datetime.now(UTC)
        midnight = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        hours = max(1, round((midnight - moment).total_seconds() / 3600))
        return (
            f"Today's search quota is used up ({used} of {cap}). "
            f"It resets in about {hours} hour{'s' if hours != 1 else ''}. Nothing was charged."
        )
    return None


class CallNotRecorded(ProviderUnavailable):
    """The ledger refused the row, so the provider was not called.

    A `ProviderUnavailable`, so the gateway treats it like any provider that
    could not be reached: noted, skipped, never a crash.
    """


class RecordedProvider:
    """A provider that cannot reach the network without leaving a row.

    **Recorded BEFORE the call, and no row means no call.** Counting after the
    call would count only the calls that returned — a timeout may still have
    spent quota — and a write that fails after the call spends quota the cap
    never sees. Over-counting a call that then failed is the safe direction;
    the quota belongs to somebody else.
    """

    def __init__(self, inner: Any, db: Any, *, user_id: str) -> None:
        self._inner = inner
        self._db = db
        self._user_id = user_id
        self.name: str = inner.name

    def available(self) -> bool:
        return bool(self._inner.available())

    def search(self, *, role: str, city: str, limit: int = 20) -> list[NormalizedJob]:
        try:
            self._db.table(LEDGER).insert(
                {
                    # Stamped from the same token RLS reads, like every other
                    # write the API makes as the user.
                    "user_id": self._user_id,
                    "source": self.name,
                    "query_key": query_key(role=role, city=city, source=self.name),
                }
            ).execute()
        except Exception as exc:
            logger.exception("could not record a %s call; not making it", self.name)
            raise CallNotRecorded(f"{self.name}: the call could not be recorded") from exc
        return list(self._inner.search(role=role, city=city, limit=limit))


__all__ = [
    "CAPPED_SOURCE",
    "COUNTER",
    "LEDGER",
    "CallNotRecorded",
    "RecordedProvider",
    "calls_today",
    "query_key",
    "refusal",
]
