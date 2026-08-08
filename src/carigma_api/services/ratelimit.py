"""Rate limiting — three distinct limiters, not one (§21 Q7).

**The credit gate stops cost; rate limits stop abuse.** Different jobs, and
collapsing them into one mechanism gets both wrong: a credit check cannot stop
an unauthenticated flood, and a request limiter cannot stop someone with a
large balance burning it in ten seconds by accident.

| # | Scope | Mechanism | Lives here? |
|---|---|---|---|
| 1 | Authenticated agent runs | token bucket per user per agent | yes |
| 2 | Unauthenticated public endpoints | per IP | yes |
| 3 | JSearch upstream quota | shared resource — cache + daily cap | **no** |

**Limiter 3 is deliberately absent from this module.** It is not per-user, and
the jobs cache plus `JSEARCH_DAILY_CAP` already own it. A second limiter over
the same resource would either duplicate the accounting or fight it — and the
failure mode of fighting is the worse one: two limiters each believing the
other let a request through.

`assert_not_shared_resource` exists to make that boundary enforceable rather
than remembered.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: Per user, per agent. A run is expensive and deliberate; nobody legitimately
#: starts six of the same agent inside a minute. Generous enough that a retry
#: after a failure is never blocked.
AGENT_BURST = 5
AGENT_REFILL_PER_SECOND = 5 / 60  # five per minute, smoothed

#: Per IP, for the unauthenticated score checker. Higher, because a shared
#: office NAT is one IP with many honest people behind it — and lower would
#: turn a coworking space into a support ticket.
PUBLIC_BURST = 20
PUBLIC_REFILL_PER_SECOND = 20 / 60


class SharedResourceMisuse(RuntimeError):
    """Raised if someone tries to rate-limit a shared upstream quota here."""


#: Resources owned by the cache + daily-cap design, not by this module.
SHARED_RESOURCES = frozenset({"jsearch", "adzuna"})


def assert_not_shared_resource(name: str) -> None:
    """Guard the §21 Q7 boundary at the point of use.

    A comment saying "don't rate-limit JSearch here" would be read once. This
    raises.
    """
    if name.lower() in SHARED_RESOURCES:
        raise SharedResourceMisuse(
            f"{name} is a SHARED upstream quota owned by the jobs cache and "
            f"JSEARCH_DAILY_CAP, not a per-user limit. A second limiter over the "
            f"same resource duplicates or fights the first."
        )


@dataclass
class Bucket:
    """A token bucket. Refills continuously rather than in steps, so a user is
    never told to wait a full window for a single token."""

    capacity: int
    refill_per_second: float
    tokens: float = field(default=0.0)
    updated_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not self.tokens:
            self.tokens = float(self.capacity)

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now

    def take(self, now: float | None = None) -> bool:
        self._refill(now if now is not None else time.monotonic())
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def retry_after_seconds(self, now: float | None = None) -> int:
        """Whole seconds until one token is available. Always ≥ 1 when empty,
        because telling someone to retry in 0s is telling them nothing."""
        self._refill(now if now is not None else time.monotonic())
        if self.tokens >= 1:
            return 0
        needed = 1 - self.tokens
        return max(1, int(needed / self.refill_per_second + 0.999))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_seconds: int = 0
    #: Which limiter spoke. Surfaced so a 429 can say something true about why.
    scope: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": "rate_limited",
            "scope": self.scope,
            "retry_after_seconds": self.retry_after_seconds,
            # Distinct from `quota_exhausted`, which means credits. Conflating
            # them would tell someone to buy credits they already have.
            "message": (
                f"Too many requests just now. Try again in "
                f"{self.retry_after_seconds}s — nothing was charged."
            ),
        }


class RateLimiter:
    """In-process buckets, keyed by scope.

    Process-local, like the run store: correct for one instance, and the seam
    to move to Redis is this class alone. Documented rather than hidden,
    because a limiter that silently stops working across instances is worse
    than no limiter — it produces false confidence.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, Bucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, key: str, capacity: int, refill: float) -> Bucket:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = Bucket(capacity=capacity, refill_per_second=refill)
                self._buckets[key] = bucket
            return bucket

    def check_agent_run(self, user_id: str, agent: str) -> Decision:
        """Limiter 1 — per user PER AGENT.

        Per-agent rather than per-user overall: running the Profile Analyst and
        Career Scout in the same minute is normal use, and a combined bucket
        would punish it.
        """
        assert_not_shared_resource(agent)
        bucket = self._bucket(f"agent:{user_id}:{agent}", AGENT_BURST, AGENT_REFILL_PER_SECOND)
        with self._lock:
            if bucket.take():
                return Decision(allowed=True, scope="agent_run")
            return Decision(
                allowed=False,
                retry_after_seconds=bucket.retry_after_seconds(),
                scope="agent_run",
            )

    def check_public(self, ip: str) -> Decision:
        """Limiter 2 — per IP, for endpoints open to the internet."""
        bucket = self._bucket(f"ip:{ip}", PUBLIC_BURST, PUBLIC_REFILL_PER_SECOND)
        with self._lock:
            if bucket.take():
                return Decision(allowed=True, scope="public_ip")
            return Decision(
                allowed=False,
                retry_after_seconds=bucket.retry_after_seconds(),
                scope="public_ip",
            )

    def reset(self) -> None:
        """Test seam."""
        with self._lock:
            self._buckets.clear()


#: One shared limiter for the process.
limiter = RateLimiter()


def client_ip(headers: dict[str, str], fallback: str = "unknown") -> str:
    """The caller's IP behind Render's proxy.

    Takes the FIRST entry of `X-Forwarded-For` — the others are proxies, and
    keying on a proxy would put every user in one bucket. Trusting this header
    is only safe because Render sets it; a self-hosted deployment behind an
    untrusted proxy must not.
    """
    forwarded = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return headers.get("x-real-ip") or fallback
