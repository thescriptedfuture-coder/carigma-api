"""Today's primary action: which one thing, and what happens to the rest.

## The ordering is DATA, not branches

Five priorities written as if/elif are five things a later edit can silently
reorder, and nothing would notice — the screen would just start pointing
somewhere else. `PRIORITIES` is a tuple, `rank` is explicit, and a test asserts
the exact sequence. Reordering it is then a visible diff on a list rather than
an accident in control flow.

## The order, and why each pair falls the way it does

    1  interview_soon   an interview inside 72 hours
    2  post_day         a slot due today
    3  score_fix        a fix waiting to be applied
    4  new_matches      jobs surfaced since the last visit
    5  standing         the fallback

**Interview beats post day.** A missed post publishes tomorrow; an unprepared
interview is gone. A dated external commitment outranks a self-imposed cadence
the user can move.

**Score fix beats new matches.** A fix is COMPLETABLE — one action, finished.
Matches are a queue you browse. Today's premise is a single primary action, so
the completable rung wins.

## What loses must keep its URGENCY, not just its presence

A suppressed rung is information the user had a moment ago, so everything that
does not win is returned in `deferred`. But presence is not enough: "2 new
matches" as a quiet stat is honest, because it is not time-bound. "Your
Wednesday post is ready" demoted to a plain stat card loses the fact that it is
TODAY — and a post day that silently slides is the cadence eroding with nobody
noticing.

So `time_bound` travels with the rung. A deferred time-bound rung must still
read as time-bound wherever it lands, and a test holds that the flag survives
demotion.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

#: An interview this close is the most time-bound thing the product knows about.
INTERVIEW_HORIZON = timedelta(hours=72)


@dataclass(frozen=True)
class Signals:
    """Everything the ladder is allowed to look at.

    A flat, explicit input so the decision is a pure function of stated facts
    rather than of whatever the route happened to have in scope.
    """

    now: datetime
    #: The soonest upcoming interview, if any. None means no interview — not
    #: "we could not check", which the route must handle before it gets here.
    next_interview_at: datetime | None = None
    interview_label: str = ""
    #: A slot due today that is neither published nor skipped.
    post_due_today: str | None = None
    #: A fix the user has been offered and not applied.
    pending_fix: str | None = None
    #: Jobs surfaced since `last_seen_jobs_at`.
    #:
    #: `None` means we do NOT KNOW — the profile has no `last_seen_jobs_at`, so
    #: there is no point to measure "since". Four of twelve live profiles are in
    #: that state. Treating it as "everything is new" would greet someone with
    #: "15 new matches" on their first ever load; treating it as zero would hide
    #: a full feed. Neither is true, so it is neither.
    new_match_count: int | None = None
    #: Everything currently active in the feed. Always knowable, and it is what
    #: the rung falls back to describing when novelty cannot be claimed.
    open_match_count: int = 0


@dataclass(frozen=True)
class Rung:
    key: str
    rank: int
    #: Whether losing this rung costs the user a WINDOW. Drives how a deferred
    #: rung is rendered, so a demoted post day still reads as due today.
    time_bound: bool
    applies: Callable[[Signals], bool]
    build: Callable[[Signals], dict[str, Any]]


def _interview_applies(s: Signals) -> bool:
    if s.next_interview_at is None:
        return False
    return timedelta(0) <= (s.next_interview_at - s.now) <= INTERVIEW_HORIZON


def _interview(s: Signals) -> dict[str, Any]:
    when = s.next_interview_at
    if when is None:
        # `_interview_applies` gates this, so reaching it means the table and
        # the builders disagree. Raising beats an assert, which `python -O`
        # removes — the check would vanish exactly where it matters.
        raise ValueError("the interview rung was built with no interview")
    hours = int((when - s.now).total_seconds() // 3600)
    return {
        "title": f"Interview {'today' if hours < 24 else 'in ' + str(hours // 24 + 1) + ' days'}",
        "body": f"{s.interview_label}. Prep while there is still time to use it.".strip(". "),
        "primary": {"label": "Open prep", "route": "/tracker"},
        "when": when.isoformat(),
    }


def _post_applies(s: Signals) -> bool:
    return bool(s.post_due_today)


def _post(s: Signals) -> dict[str, Any]:
    return {
        "title": f"Your {s.post_due_today} post is ready",
        "body": "Review it and mark it published when you have posted.",
        "primary": {"label": "Open the draft", "route": f"/posts/week/{s.post_due_today}"},
    }


def _fix_applies(s: Signals) -> bool:
    return bool(s.pending_fix)


def _fix(s: Signals) -> dict[str, Any]:
    return {
        "title": "One fix is waiting",
        "body": f"{s.pending_fix} — a single change you can finish now.",
        "primary": {"label": "Apply it", "route": "/signal/linkedin"},
    }


def _matches_apply(s: Signals) -> bool:
    """Something to say about jobs — whether or not we can call it new."""
    if s.new_match_count is not None:
        return s.new_match_count > 0
    return s.open_match_count > 0


def _matches(s: Signals) -> dict[str, Any]:
    """Two sentences, and only one of them claims novelty.

    With no `last_seen_jobs_at` there is no "since" to measure from, so the
    honest line is a count of what is waiting rather than a count of what is
    new. "15 new matches" on a first load would be a claim about a comparison
    we never made.
    """
    if s.new_match_count is not None:
        count = s.new_match_count
        plural = "match" if count == 1 else "matches"
        return {
            "title": f"{count} new {plural}",
            "body": "Surfaced since you last looked.",
            "primary": {"label": "Open Jobs", "route": "/jobs"},
        }

    count = s.open_match_count
    plural = "match" if count == 1 else "matches"
    return {
        "title": f"{count} {plural} waiting",
        "body": "Your feed, as it stands.",
        "primary": {"label": "Open Jobs", "route": "/jobs"},
    }


def _standing(s: Signals) -> dict[str, Any]:
    """The fallback. Nothing is pressing, and saying so is the honest answer.

    Not a manufactured task. A product that always has something for you to do
    is one that invents work.

    But the copy has to agree with the button underneath it. It used to read
    "Nothing needs you right now / Your plan is running. Come back when
    something lands." — above a button offering the week. Three things wrong:

    - **It sends people away** from an action it is simultaneously offering.
      A screen whose text says "come back later" is a screen people leave, and
      the rung exists precisely because there is always a next thing.
    - **"Your plan is running" can be false.** A brand-new account has no plan.
      Asserting one to somebody who has never set anything up is the
      fabrication rule broken in the smallest possible way.
    - It reads as an apology for the product having nothing to say, when a
      quiet day is a real and good answer.

    So: state the quiet honestly, do not claim anything about a plan, and let
    the body describe what the button actually gets you.
    """
    return {
        "title": "Nothing is due today",
        "body": "A quiet day is a real answer. It is a good moment to look at the week ahead.",
        "primary": {"label": "See the week", "route": "/posts"},
    }


#: THE TABLE. Order here is the product decision; `rank` makes it explicit so
#: a reordering shows up as a changed number rather than as moved code.
CONDITIONAL: tuple[Rung, ...] = (
    Rung("interview_soon", 1, True, _interview_applies, _interview),
    Rung("post_day", 2, True, _post_applies, _post),
    Rung("score_fix", 3, False, _fix_applies, _fix),
    Rung("new_matches", 4, False, _matches_apply, _matches),
)

#: Separate, because it is not a condition — it is what is left when none of
#: the others hold. Splitting it out makes two rules structural rather than
#: enforced: `choose` cannot return nothing, and the fallback cannot appear in
#: `deferred`, because it is not in the list that produces deferrals.
FALLBACK = Rung("standing", 5, False, lambda _s: True, _standing)

PRIORITIES: tuple[Rung, ...] = (*CONDITIONAL, FALLBACK)


@dataclass(frozen=True)
class Decision:
    primary: dict[str, Any]
    deferred: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"primary_action": self.primary, "deferred": self.deferred}


def choose(signals: Signals) -> Decision:
    """The one primary action, and everything it displaced.

    Every applicable rung is evaluated — the losers are not discarded, they are
    demoted with their `time_bound` intact.
    """
    ordered = sorted(CONDITIONAL, key=lambda r: r.rank)
    applicable = [rung for rung in ordered if rung.applies(signals)]

    # No empty case to guard. `FALLBACK` is not in the list that can be empty,
    # so "the ladder produced nothing" is unrepresentable rather than asserted
    # against — and an assert would have been stripped by `python -O` anyway.
    winner = applicable[0] if applicable else FALLBACK
    rest = applicable[1:]

    primary = {**winner.build(signals), "key": winner.key, "time_bound": winner.time_bound}
    deferred = [
        {
            **rung.build(signals),
            "key": rung.key,
            # The refinement that matters: a demoted post day is still due
            # TODAY. Dropping this makes it a quiet stat, and a post day that
            # silently slides is the cadence eroding unnoticed.
            "time_bound": rung.time_bound,
        }
        for rung in rest
    ]
    return Decision(primary=primary, deferred=deferred)


__all__ = [
    "CONDITIONAL",
    "FALLBACK",
    "INTERVIEW_HORIZON",
    "PRIORITIES",
    "Decision",
    "Rung",
    "Signals",
    "choose",
]
