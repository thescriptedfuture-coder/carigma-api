"""How many of us are running, and what it means if the answer is more than one.

`services/ratelimit` holds token buckets in process memory. That is correct on
one instance and cheap. On two, **every ceiling silently doubles** — no error,
no log line, just twice the intended rate against a JSearch quota that live V1
users are also drawing from.

## The decision: keep the in-process limiter, make its constraint visible

Not Redis. One instance is right at this scale, and adding infrastructure for a
problem we do not have is how a solo founder acquires an operational burden
before acquiring users.

What matters is that scaling cannot happen **by accident**. A declared
`MAX_INSTANCES` setting would catch an operator editing environment variables;
it would not catch the real accident, which is someone raising the instance
count in a dashboard that touches no environment variable at all.

So the process observes its peers instead of trusting a declaration.

## It warns, it does not refuse

A rolling deploy runs old and new together for a minute. Refusing to boot would
turn every routine deploy into an outage — a worse failure than the one being
prevented, and the kind of safety mechanism that gets disabled the first time
it fires at 2am.

CRITICAL to the log, and a flag admin can see. The alarm fires, the deploy
finishes, the overlap clears itself.

## Nothing here can take the API down

Every failure path returns "we do not know" and lets the request through. A
heartbeat table that cannot be reached must never become the reason the product
is offline; the limiter it protects is itself only a ceiling.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "api_instances"

#: How recently a peer must have checked in to count as alive. Comfortably
#: longer than the heartbeat interval so one slow write is not a phantom
#: departure, and short enough that a finished rolling deploy clears within
#: a couple of minutes.
ALIVE_WINDOW = timedelta(minutes=3)

#: This process. Generated once at import: two processes from the same image
#: get different ids, which is exactly what is being counted.
INSTANCE_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class Census:
    """What we could observe, and whether it is a problem.

    `count` is `None` when the table could not be read — "we could not look",
    never "there is one of us". Reporting a confident 1 from a failed read is
    exactly the shape that made the retention bug and the empty weekly review
    survive.
    """

    count: int | None
    at: datetime

    @property
    def known(self) -> bool:
        return self.count is not None

    @property
    def is_overscaled(self) -> bool:
        """True only when we have SEEN more than one. Unknown is not a breach."""
        return self.count is not None and self.count > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "instances": self.count,
            "known": self.known,
            "overscaled": self.is_overscaled,
            "checked_at": self.at.isoformat(),
            "note": (
                "Rate limits are per-process. More than one instance means every "
                "ceiling is multiplied by the instance count."
                if self.is_overscaled
                else None
            ),
        }


def heartbeat(client: Any, *, version: str = "", now: datetime | None = None) -> None:
    """Record that this process is alive. Never raises."""
    at = now or datetime.now(UTC)
    try:
        client.table(TABLE).upsert(
            {
                "instance_id": INSTANCE_ID,
                "last_seen": at.isoformat(),
                "version": version,
            },
            on_conflict="instance_id",
        ).execute()
    except Exception:
        # A heartbeat that fails is a monitoring gap, not an outage.
        logger.warning("instance heartbeat failed", exc_info=True)


def census(client: Any, *, now: datetime | None = None) -> Census:
    """How many instances have checked in recently. Never raises."""
    at = now or datetime.now(UTC)
    cutoff = (at - ALIVE_WINDOW).isoformat()
    try:
        res = client.table(TABLE).select("instance_id").gte("last_seen", cutoff).execute()
        rows = res.data if res and isinstance(res.data, list) else []
    except Exception:
        logger.warning("instance census failed", exc_info=True)
        return Census(None, at)

    alive = {str(r.get("instance_id")) for r in rows if isinstance(r, dict)}
    return Census(len(alive), at)


def check(client: Any, *, version: str = "", now: datetime | None = None) -> Census:
    """Heartbeat, then count, then say so loudly if there is more than one.

    Called at startup and from the admin surface. The CRITICAL is the whole
    mechanism: it is the difference between scaling being a decision and
    scaling being something that happened.
    """
    heartbeat(client, version=version, now=now)
    seen = census(client, now=now)

    if seen.is_overscaled:
        logger.critical(
            "MORE THAN ONE API INSTANCE IS RUNNING (%s seen). Rate limits are held "
            "in process memory, so every ceiling is now multiplied by %s — including "
            "the share of the JSearch quota that live V1 users depend on. This is "
            "expected for a minute during a rolling deploy and a problem if it "
            "persists. To scale deliberately, move the buckets to a shared store "
            "first; see services/ratelimit.",
            seen.count,
            seen.count,
        )
    elif not seen.known:
        logger.warning("could not determine the instance count — scaling is unverified")

    return seen


__all__ = ["ALIVE_WINDOW", "INSTANCE_ID", "TABLE", "Census", "census", "check", "heartbeat"]
