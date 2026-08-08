"""Admin — an operational tool, not a reporting dashboard.

Top-ups are **manual** during beta: when someone exhausts their welcome grant,
the founder adds credits by hand. That single fact decides what this module is
for. A dashboard that reports numbers is useless here; what is needed is a
surface that says *who needs something from you right now*.

## The thing that matters most

**A blocked user who cannot act and does not complain churns silently.** They
hit zero, the buttons stop working, and they leave without ever sending a
message. That is the failure this module exists to prevent, and it is why
low-credit users are pushed at the founder rather than waiting to be searched
for. A list you have to remember to check is a list that fails.

Urgency ordering is deliberate: **already at zero beats nearly out.** Someone
at zero is blocked *now*; someone at 8 credits is merely close. Sorting by
balance ascending does this naturally, but the tiering is explicit so the
count on the overview can say "3 blocked, 5 nearly out" rather than one
undifferentiated number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

# The cheapest useful action is `copy_caption` at 2 credits; the cheapest AGENT
# run is `run_naukri`/`run_profile` at 10. So "nearly out" means "cannot run an
# agent", which is the point at which the product stops being itself.
BLOCKED_AT = 0
NEARLY_OUT_BELOW = 15


class CreditUrgency(StrEnum):
    BLOCKED = "blocked"
    NEARLY_OUT = "nearly_out"
    FINE = "fine"

    @property
    def rank(self) -> int:
        """Sort key. Lower is more urgent."""
        return {"blocked": 0, "nearly_out": 1, "fine": 2}[self.value]


def urgency_of(balance: int) -> CreditUrgency:
    if balance <= BLOCKED_AT:
        return CreditUrgency.BLOCKED
    if balance < NEARLY_OUT_BELOW:
        return CreditUrgency.NEARLY_OUT
    return CreditUrgency.FINE


@dataclass(frozen=True)
class LowCreditUser:
    user_id: str
    email: str
    balance: int
    last_active: datetime | None = None
    #: Whether they have ASKED for more. An unresolved ask plus a zero balance
    #: is the most urgent combination there is — they told us and we haven't
    #: answered.
    has_open_request: bool = False

    @property
    def urgency(self) -> CreditUrgency:
        return urgency_of(self.balance)

    @property
    def sort_key(self) -> tuple[int, int, int]:
        # An open request outranks everything at the same tier: they are
        # blocked AND they reached out, so silence from us is now the problem.
        return (self.urgency.rank, 0 if self.has_open_request else 1, self.balance)

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "email": self.email,
            "balance": self.balance,
            "urgency": str(self.urgency),
            "has_open_request": self.has_open_request,
            "last_active": self.last_active.isoformat() if self.last_active else None,
            "line": self.line(),
        }

    def line(self) -> str:
        """One sentence the founder can act on without opening the detail view."""
        if self.urgency is CreditUrgency.BLOCKED:
            base = "Blocked — 0 credits"
        elif self.urgency is CreditUrgency.NEARLY_OUT:
            base = f"{self.balance} left — can't run an agent"
        else:
            base = f"{self.balance} credits"
        return f"{base} · asked for more" if self.has_open_request else base


def rank_low_credit(users: list[LowCreditUser]) -> list[LowCreditUser]:
    """Most urgent first: blocked before nearly-out, askers before silent."""
    return sorted(users, key=lambda u: u.sort_key)


def low_credit_summary(users: list[LowCreditUser]) -> dict[str, Any]:
    """The count that greets the founder on opening admin.

    Split rather than totalled, because "3 blocked" is an instruction and
    "8 low" is a statistic.
    """
    ranked = rank_low_credit([u for u in users if u.urgency is not CreditUrgency.FINE])
    blocked = [u for u in ranked if u.urgency is CreditUrgency.BLOCKED]
    nearly = [u for u in ranked if u.urgency is CreditUrgency.NEARLY_OUT]
    asked = [u for u in ranked if u.has_open_request]

    return {
        "blocked": len(blocked),
        "nearly_out": len(nearly),
        "asked_for_more": len(asked),
        "needs_action": len(ranked),
        "users": [u.as_dict() for u in ranked],
        "headline": _headline(len(blocked), len(nearly), len(asked)),
    }


def _headline(blocked: int, nearly: int, asked: int) -> str:
    if blocked == 0 and nearly == 0:
        # An honest nothing. Not "All good!" — a statement of fact.
        return "Nobody is out of credits."
    parts = []
    if blocked:
        parts.append(f"{blocked} blocked at zero")
    if nearly:
        parts.append(f"{nearly} nearly out")
    line = " · ".join(parts)
    return f"{line} · {asked} asked for more" if asked else line


# ── Manual credit adjustment ───────────────────────────────────────────────


class AdjustmentRefused(ValueError):
    """The adjustment was not valid and nothing was written."""


@dataclass(frozen=True)
class Adjustment:
    """A founder-made credit change.

    `reason` is REQUIRED and non-trivial. An adjustment with no reason is
    unauditable six weeks later, when the only question that matters is "why
    does this person have 400 credits?" — and the answer has to be in the
    ledger, not in someone's memory.
    """

    user_id: str
    delta: int
    reason: str
    author: str

    def __post_init__(self) -> None:
        if self.delta == 0:
            raise AdjustmentRefused("A zero adjustment does nothing — nothing was written.")
        if len(self.reason.strip()) < 3:
            raise AdjustmentRefused(
                "Give a reason. The ledger has to explain itself later, when "
                "nobody remembers this conversation."
            )

    def as_ledger_row(self, *, balance_after: int) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "delta": self.delta,
            "balance_after": balance_after,
            # Distinct from purchase and signup ON PURPOSE. During a manual
            # top-up beta, "credits issued" is a meaningless number unless
            # given-away and sold can be told apart.
            "kind": "admin_adjustment",
            "reason": f"{self.reason.strip()} — by {self.author}",
        }


def apply_adjustment(balance: int, adjustment: Adjustment, *, allow_negative: bool = False) -> int:
    """Compute the new balance.

    A removal that would take someone below zero clamps at zero rather than
    creating a debt, unless explicitly allowed. A negative balance has no
    meaning in this product — there is nothing to collect.
    """
    new = balance + adjustment.delta
    if new < 0 and not allow_negative:
        return 0
    return new


# ── Overview ───────────────────────────────────────────────────────────────


@dataclass
class Overview:
    total_users: int = 0
    active_7d: int = 0
    active_30d: int = 0
    runs_7d: int = 0
    runs_failed_7d: int = 0
    credits_issued: int = 0
    credits_consumed: int = 0
    recent: list[dict[str, Any]] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        """Fraction of runs that failed. Zero runs is 0.0, not a division
        error and not "100% healthy" — there is simply nothing to rate."""
        return round(self.runs_failed_7d / self.runs_7d, 3) if self.runs_7d else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_users": self.total_users,
            "active_7d": self.active_7d,
            "active_30d": self.active_30d,
            "runs_7d": self.runs_7d,
            "runs_failed_7d": self.runs_failed_7d,
            "error_rate": self.error_rate,
            "credits_issued": self.credits_issued,
            "credits_consumed": self.credits_consumed,
            # Deliberately NOT a single "credits outstanding" figure. Issued
            # and consumed answer different questions and netting them hides
            # both.
            "recent": self.recent,
        }


# ── Analytics ──────────────────────────────────────────────────────────────


def activation_rate(signups: int, reached_first_score: int) -> dict[str, Any]:
    """Activation = reached their first score. That is the moment the product
    has demonstrated anything at all."""
    if signups == 0:
        return {"signups": 0, "activated": 0, "rate": None, "note": "No signups yet."}
    return {
        "signups": signups,
        "activated": reached_first_score,
        "rate": round(reached_first_score / signups, 3),
        "note": None,
    }


def retention_buckets(
    signup_dates: list[datetime],
    activity: dict[str, list[datetime]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Day 1 / 7 / 30 retention.

    A cohort is only counted once it has HAD the chance to retain: someone who
    signed up yesterday cannot yet be a day-7 datum, and including them would
    silently depress the number. `eligible` is reported alongside each rate so
    a small denominator is visible rather than hidden.
    """
    now = now or datetime.now(UTC)
    out: dict[str, Any] = {}
    for day in (1, 7, 30):
        eligible = [d for d in signup_dates if (now - d).days >= day]
        if not eligible:
            out[f"day_{day}"] = {"eligible": 0, "retained": 0, "rate": None}
            continue
        retained = 0
        for signup in eligible:
            window_end = signup + timedelta(days=day + 1)
            seen = activity.get(signup.isoformat(), [])
            if any(signup + timedelta(days=day) <= t < window_end for t in seen):
                retained += 1
        out[f"day_{day}"] = {
            "eligible": len(eligible),
            "retained": retained,
            "rate": round(retained / len(eligible), 3),
        }
    return out
