"""Sunday evening: the week's review, then next week's plan. Roadmap S9.

`services.weekly` opens with "Sunday evening the product proposes next week;
the user signs it", and `scripts/send_emails.py` says the `weekly` job does it.
**Nothing did.** Measured on 2026-10-19, three pieces of the ritual were
missing, and each failed quietly:

- **No review was ever built.** `save_review` had no caller, so
  `GET /weekly/review` answered null every week and Progress said "Your first
  review builds Sunday, 18:00" — to a user four weeks in.
- **No week was ever proposed.** Every Monday started with "No week is signed
  yet" until the user pressed Plan this week, and the sweep had nothing of the
  product's to adopt.
- **The email reviewed the week before.** It ran on Sunday and subtracted a
  week from that Sunday's Monday, so on 18 Oct it read 5–11 Oct, found
  nothing, and skipped — with Ravi's first published post in the week it
  should have read.

## Who it runs for

**Users who have planned a week in V2** — anyone with a `weekly_review` row.
V1 and V2 share one database and nothing in `profiles` tells their users
apart, so "every profile" would write plans for twelve people using V1. The
sweep would then adopt each one unanswered, and on their first V2 visit they
would meet a standing-by card about weeks they were never shown. At cutover
this widens to every onboarded profile: `SUNDAY_AUDIENCE=everyone`
(`audience`, `CUTOVER_PARITY.md`).

## What it will not do

- **Propose to someone the lapse card is about to take over for.** Two
  unanswered weeks make Monday's Progress the standing-by card, whose door is
  re-entry — and re-entry refuses a week that already has a plan. A proposal
  there would leave the card's only button answering "this week already has a
  plan" while the plan stays hidden behind it.
- **Write a review onto a week that does not exist.** See `save_review`.
- **Write a partial review.** Any read that fails and the review is not
  stored: a stored review is the permanent record of the week, and one missing
  its pipeline because a table timed out would say less happened than did.
  The email, which is gone once read, keeps its own rule — a dead source costs
  a line.

Takes a SERVICE-ROLE client, like the sweep: it reads across users.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from carigma_api.services import emails as mail
from carigma_api.services.applications import EVENTS
from carigma_api.services.applications import TABLE as APPLICATIONS
from carigma_api.services.clock import IST
from carigma_api.services.posts import SkipReason, Slot, SlotState, streak_state
from carigma_api.services.posts_store import SupabasePlanStore
from carigma_api.services.repository import ProfileRepository
from carigma_api.services.review import Direction, ReviewFact, Streak, earned_review
from carigma_api.services.schedule import proposal_items, schedule
from carigma_api.services.sweep import is_sweepable
from carigma_api.services.weekly import (
    Cadence,
    WeeklyContract,
    auto_adopt,
    consecutive_lapses,
)
from carigma_api.services.weekly_store import TABLE, SupabaseContractStore

logger = logging.getLogger(__name__)


#: Two unanswered weeks make Monday's Progress the standing-by card.
#: `weekly.presentation_for` is the rule; this is the count it starts at.
STANDING_BY_AT = 2


def closing_week(today: date) -> date:
    """The Monday of the week that the most recent Sunday closed.

    On a Sunday that is THIS week. The email read `monday_of(today) - 1 week`,
    which on its own Sunday is the week before the one ending. The same Sunday
    `weekly_key` names, so a Monday retry reviews the same week.
    """
    return date.fromisoformat(mail.weekly_key(today)) - timedelta(days=6)


def _bounds(week_start: date) -> tuple[str, str]:
    """The week as UTC instants: Monday 00:00 IST up to next Monday 00:00 IST."""
    start = datetime.combine(week_start, time(0), IST).astimezone(UTC)
    return start.isoformat(), (start + timedelta(weeks=1)).isoformat()


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stamp(value: Any) -> str:
    """`SUN 22:06` — when, in the reader's time. Empty if it cannot be read."""
    moment = _moment(value)
    return f"{moment.astimezone(IST):%a %H:%M}".upper() if moment else ""


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ── The record ─────────────────────────────────────────────────────────────


def week_posts(db: Any, user_id: str, week_start: date) -> tuple[int, int]:
    """(published, planned) for a week, counted the way the review counts.

    The weekly email counted `content_loop` rows, which are the slots someone
    has drafted, so a signed week of five posts with two drafted and one
    published mailed "1/2 shipped" beside a review reading "1 of 5 planned
    posts published" (UAT, 25 Oct). One fact, one number: both read the
    schedule Posts showed. A week with no signed plan has no planned posts,
    so it counts the slots the user worked on, as the email always did.
    Raises if either source cannot be read.
    """
    history = SupabaseContractStore(db).history(user_id)
    contract = next((c for c in history if c.week_start == week_start), None)
    stored = SupabasePlanStore(db).load(user_id, week_start)
    slots = schedule(week_start, contract=contract, stored=stored).slots
    planned = [s for s in slots if not s.optional] or list(slots)
    return sum(1 for s in planned if s.state is SlotState.PUBLISHED), len(planned)


def _post_facts(slots: tuple[Slot, ...]) -> list[ReviewFact]:
    planned = [s for s in slots if not s.optional]
    published = [s for s in planned if s.state is SlotState.PUBLISHED]
    facts: list[ReviewFact] = []

    if planned:
        stamps = " · ".join(filter(None, (_stamp(s.published_at) for s in published)))
        unmarked = " ".join(s.day for s in planned if s.state is not SlotState.PUBLISHED)
        # The receipt is the evidence: when each was marked, and which days
        # were not. "Posts, this week" would be a label, not proof.
        receipt = " · ".join(
            part
            for part in (
                f"MARK-PUBLISHED {stamps}" if stamps else "",
                f"NOT MARKED PUBLISHED {unmarked}" if unmarked else "",
            )
            if part
        )
        facts.append(
            ReviewFact(
                Direction.DONE if len(published) == len(planned) else Direction.NEUTRAL,
                f"{len(published)} of {_plural(len(planned), 'planned post')} published",
                receipt,
            )
        )
    for slot in planned:
        if slot.state is SlotState.SKIPPED:
            reason = slot.skip_reason
            why = f" — {reason.label.lower()}" if reason and reason is not SkipReason.OTHER else ""
            facts.append(
                ReviewFact(
                    Direction.NEUTRAL,
                    f"Skipped {slot.slot_date:%A}'s post{why}",
                    f"SKIPPED {slot.day}",
                )
            )

    outside = [s for s in slots if s.optional and s.state is SlotState.PUBLISHED]
    if outside:
        facts.append(
            ReviewFact(
                Direction.DONE,
                f"{_plural(len(outside), 'post')} published outside your plan",
                "MARK-PUBLISHED "
                + " · ".join(filter(None, (_stamp(s.published_at) for s in outside))),
            )
        )
    return facts


#: Stage moves worth a line, in board order, with how each is said.
_MOVES: tuple[tuple[str, Direction, str, str], ...] = (
    ("stage_saved", Direction.NEUTRAL, "Saved", "role"),
    ("stage_applied", Direction.DONE, "Applied to", "role"),
    ("stage_interview", Direction.UP, "Moved to interview:", "application"),
    ("stage_offer", Direction.UP, "Offer stage:", "application"),
    ("stage_closed", Direction.NEUTRAL, "Closed", "application"),
)


def _pipeline_facts(db: Any, user_id: str, start: str, end: str) -> list[ReviewFact]:
    events = (
        db.table(EVENTS)
        .select("application_id,event_type,created_at")
        .eq("user_id", user_id)
        .gte("created_at", start)
        .lt("created_at", end)
        .execute()
        .data
        or []
    )
    # Distinct applications per move: an application moved to offer twice in a
    # minute is one offer, not two.
    by_move: dict[str, list[str]] = {}
    for event in events:
        kind, app = str(event.get("event_type") or ""), str(event.get("application_id") or "")
        if app and app not in by_move.setdefault(kind, []):
            by_move[kind].append(app)
    if not any(by_move.get(move) for move, *_ in _MOVES):
        return []

    ids = sorted({a for apps in by_move.values() for a in apps})
    rows = (
        db.table(APPLICATIONS).select("id,company").eq("user_id", user_id).in_("id", ids).execute()
    ).data or []
    company = {str(r.get("id")): str(r.get("company") or "").strip() for r in rows}

    facts = []
    for move, direction, verb, noun in _MOVES:
        apps = by_move.get(move, [])
        if not apps:
            continue
        names = [_short(company.get(a, "")) or "UNNAMED" for a in apps]
        more = f" +{len(names) - NAMED} MORE" if len(names) > NAMED else ""
        facts.append(
            ReviewFact(
                direction,
                f"{verb} {_plural(len(apps), noun)}",
                # Named, so it can be checked against the board. A company
                # we cannot name is still counted, and the receipt says so.
                "TRACKER · " + ", ".join(names[:NAMED]) + more,
            )
        )
    return facts


#: Names on a receipt before it says how many more. Measured on Ravi's week:
#: four companies, one of them 70 characters long, made a receipt longer than
#: the fact it was evidence for.
NAMED = 3


def _short(name: str, limit: int = 28) -> str:
    name = name.strip().upper()
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _score_fact(db: Any, user_id: str, start: str, end: str) -> list[ReviewFact]:
    rows = (
        db.table("score_history")
        .select("score,created_at")
        .eq("user_id", user_id)
        .lt("created_at", end)
        .order("created_at", desc=True)
        .limit(200)
        .execute()
        .data
        or []
    )
    inside = [r for r in rows if str(r.get("created_at") or "") >= start]
    if not inside:
        return []
    newest = inside[0]
    before = next((r for r in rows if str(r.get("created_at") or "") < start), None)
    moment = _moment(newest.get("created_at"))
    receipt = f"SCORE · MEASURED {moment.astimezone(IST):%a %d %b}".upper() if moment else "SCORE"
    score = int(newest["score"])
    if before is None:
        return [ReviewFact(Direction.NEUTRAL, f"Profile score measured: {score}", receipt)]
    previous = int(before["score"])
    if score == previous:
        return [ReviewFact(Direction.NEUTRAL, f"Profile score held at {score}", receipt)]
    # The direction is stated, never dressed up. A fall is a fall — and it is
    # built by the same constructor as a rise, so it reads as weather.
    direction = Direction.UP if score > previous else Direction.DOWN
    return [ReviewFact(direction, f"Profile score {previous} → {score}", receipt)]


def _match_fact(db: Any, user_id: str, start: str, end: str) -> list[ReviewFact]:
    rows = (
        db.table("jobs_feed")
        .select("id")
        .eq("user_id", user_id)
        .gte("first_seen", start)
        .lt("first_seen", end)
        .execute()
        .data
        or []
    )
    if not rows:
        return []
    return [
        ReviewFact(
            Direction.NEUTRAL,
            f"Career Scout found {_plural(len(rows), 'new match', 'new matches')}",
            "CAREER SCOUT · FIRST SEEN THIS WEEK",
        )
    ]


def build_review(
    db: Any, user_id: str, week_start: date, *, contract: WeeklyContract | None, now: datetime
) -> dict[str, Any]:
    """What the week EARNED. Raises if any source cannot be read."""
    start, end = _bounds(week_start)
    stored = SupabasePlanStore(db).load(user_id, week_start)
    # The same schedule Posts showed that week, so the review and the page
    # cannot disagree about what was planned.
    plan = schedule(week_start, contract=contract, stored=stored)
    facts = (
        *_post_facts(plan.slots),
        *_pipeline_facts(db, user_id, start, end),
        *_score_fact(db, user_id, start, end),
        *_match_fact(db, user_id, start, end),
    )
    # One implementation of the streak rule — the one Posts shows.
    return earned_review(facts=facts, streak=Streak(weeks=streak_state(plan).weeks), built_at=now)


# ── Next week ──────────────────────────────────────────────────────────────


def _why_not_propose(history: list[WeeklyContract], next_monday: date, now: datetime) -> str | None:
    if any(c.week_start == next_monday for c in history):
        return "already planned"
    # The lapses Monday will show: this week's unanswered plan, as the sweep
    # will have adopted it by then.
    after_sweep = [
        auto_adopt(c, now=now) if is_sweepable(c, today=next_monday) else c for c in history
    ]
    if consecutive_lapses(after_sweep, upto=next_monday) >= STANDING_BY_AT:
        return "standing by"
    latest = max(history, key=lambda c: c.week_start, default=None)
    if (
        latest is not None
        and latest.cadence is Cadence.FORTNIGHTLY
        and (next_monday - latest.week_start).days < 14
    ):
        return "fortnightly off-week"
    return None


# ── The pass ───────────────────────────────────────────────────────────────


@dataclass
class SundayReport:
    reviewed: int = 0
    proposed: int = 0
    failed: int = 0
    #: Why a review or a proposal did not happen, counted by reason.
    skipped: Counter[str] = field(default_factory=Counter)
    #: (user_id, what happened) — the log line, structured.
    lines: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reviewed": self.reviewed,
            "proposed": self.proposed,
            "failed": self.failed,
            "skipped": dict(self.skipped),
            "lines": [{"user_id": u, "what": w} for u, w in self.lines],
        }


def planners(db: Any) -> list[str]:
    """Everyone who has planned a week in V2 — see the module docstring."""
    rows = db.table(TABLE).select("user_id").execute().data or []
    return sorted({str(r["user_id"]) for r in rows if isinstance(r, dict) and r.get("user_id")})


def audience(db: Any, setting: str) -> list[str]:
    """Whom this Sunday plans for: `planners`, or at cutover, `everyone`.

    `everyone` is the planners AND every onboarded profile — a planner whose
    profile row lost its flag is still a planner. Anything but the exact word
    is `planners`, like EMAIL_AUDIENCE: a typo must not write plans for
    people still using V1.
    """
    planned = planners(db)
    if setting.strip().lower() != "everyone":
        return planned
    rows = db.table("profiles").select("user_id").eq("onboarded", True).execute().data or []
    onboarded = {str(r["user_id"]) for r in rows if isinstance(r, dict) and r.get("user_id")}
    return sorted(set(planned) | onboarded)


def sunday(
    db: Any,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    only: str | None = None,
    who: str = "planners",
) -> SundayReport:
    """Review the closing week and propose the next, for `audience(db, who)`.

    `only` restricts the pass to one user id — the cron's `--only`.
    """
    moment = now or datetime.now(UTC)
    week = closing_week(moment.date())
    next_monday = week + timedelta(weeks=1)
    store = SupabaseContractStore(db)
    report = SundayReport()

    for user_id in audience(db, who):
        if only and user_id != only:
            continue
        try:
            history = store.history(user_id)
        except Exception:
            logger.exception("sunday: could not read the weeks of %s", user_id)
            report.failed += 1
            continue

        # ── the record ──
        contract = next((c for c in history if c.week_start == week), None)
        if contract is None:
            report.skipped["review: no week to attach to"] += 1
        else:
            try:
                earned = build_review(db, user_id, week, contract=contract, now=moment)
                stored = dry_run or store.save_review(user_id, week, earned)
            except Exception:
                logger.exception("sunday: could not build the review of %s for %s", week, user_id)
                report.failed += 1
            else:
                if stored:
                    report.reviewed += 1
                    report.lines.append((user_id, f"reviewed {week}: {len(earned['facts'])} facts"))
                else:
                    report.skipped["review: no week to attach to"] += 1

        # ── next week ──
        why_not = _why_not_propose(history, next_monday, moment)
        if why_not:
            report.skipped[f"proposal: {why_not}"] += 1
            continue
        try:
            profile = ProfileRepository(db).load(user_id)
        except Exception:
            logger.exception("sunday: could not read the profile of %s", user_id)
            report.failed += 1
            continue
        if not profile:
            # A plan built on a guessed cadence would offer days they never chose.
            report.skipped["proposal: no profile"] += 1
            continue
        latest = max(history, key=lambda c: c.week_start, default=None)
        proposed = WeeklyContract(
            week_start=next_monday,
            items=proposal_items(profile, today=next_monday),
            cadence=latest.cadence if latest else Cadence.WEEKLY,
            proposed_at=moment,
        )
        if not dry_run:
            try:
                store.save(user_id, proposed)
            except Exception:
                logger.exception("sunday: could not save next week for %s", user_id)
                report.failed += 1
                continue
        report.proposed += 1
        posts = sum(1 for i in proposed.items if i.kind == "content")
        report.lines.append((user_id, f"proposed {next_monday}: {_plural(posts, 'post')}"))

    for user_id, what in report.lines:
        logger.info("sunday: %s %s%s", user_id, what, " (dry run)" if dry_run else "")
    logger.info(
        "sunday complete: reviewed=%d proposed=%d failed=%d skipped=%s%s",
        report.reviewed,
        report.proposed,
        report.failed,
        dict(report.skipped),
        " (dry run)" if dry_run else "",
    )
    return report


__all__ = [
    "IST",
    "SundayReport",
    "audience",
    "build_review",
    "closing_week",
    "planners",
    "sunday",
    "week_posts",
]
