"""The Sunday sweep: unanswered contracts continue, conservatively.

## The decision

A contract proposed on Sunday and never answered by the following Sunday is
**auto-adopted**, not expired. Missing a Sunday must not cost someone their
week — that punishes them for being busy, which is the instinct the lapsed
grammar exists to forbid. And expiring leaves nothing to escalate from: no
plan, no signal, worse than a conservative plan they can change in a click.

## What makes it honest

Two things, and neither is copy:

1. **What carries forward is conservative.** `auto_adopt` applies
   `conservative_items`, so an unanswered week continues at the smaller
   commitment rather than the one the user never agreed to.
2. **The system never claims agreement.** `auto_adopted` is `is_decided` true
   and `user_actually_agreed` false, so no surface can render it as approval.
   The sentence it supports is "the standing plan continued; you haven't
   chosen yet", which is true.

## Observability, because this is the first thing that ever writes the state

`auto_adopt()` existed as a well-tested pure function with **no caller** for
two phases. Nothing swept, so `consecutive_lapses` always returned 0 and the whole
escalation was unreachable. A sweep that runs silently would be the same
failure wearing a schedule: the only evidence it is working is a state
appearing that nothing else can produce.

So every adoption logs what it carried forward, and `SweepReport` counts
adopted against skipped. The admin surface reports **auto_adopted vs
approved**, which is the equivalent of the finished:returned ratio — it says
whether the Sunday ritual is working or whether people are letting it ride.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from carigma_api.services.weekly import ContractState, WeeklyContract, auto_adopt
from carigma_api.services.weekly_store import TABLE, from_row, to_row

logger = logging.getLogger(__name__)

#: A contract is unanswered once its week has ENDED. Sweeping on the Sunday it
#: was proposed would adopt a plan the user still had all week to answer.
GRACE = timedelta(days=7)


@dataclass
class SweepReport:
    adopted: int = 0
    already_decided: int = 0
    too_recent: int = 0
    failed: int = 0
    #: (user_id, week_start, what carried forward) — the log line, structured.
    carried: list[tuple[str, str, list[str]]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "adopted": self.adopted,
            "already_decided": self.already_decided,
            "too_recent": self.too_recent,
            "failed": self.failed,
            "carried": [{"user_id": u, "week_start": w, "kinds": k} for u, w, k in self.carried],
        }


def is_sweepable(contract: WeeklyContract, *, today: date) -> bool:
    """Never answered, and its week is over."""
    return not contract.state.is_decided and contract.week_start + GRACE <= today


def sweep(client: Any, *, now: datetime | None = None, dry_run: bool = False) -> SweepReport:
    """Adopt every unanswered contract whose week has closed.

    Takes a SERVICE-ROLE client: this reads across all users, which no user's
    own token can do. That is the same reason the re-engagement cron holds one.
    """
    moment = now or datetime.now(UTC)
    today = moment.date()
    report = SweepReport()

    res = client.table(TABLE).select("*").eq("state", str(ContractState.PROPOSED)).execute()
    rows = res.data if res and isinstance(res.data, list) else []

    for row in rows:
        if not isinstance(row, dict):
            continue
        user_id = str(row.get("user_id") or "")
        try:
            contract = from_row(row)
        except Exception:
            # A row we cannot read is not a row to adopt. Guessing at it would
            # put a plan in force on a week we do not understand.
            logger.exception("could not read a %s row for %s", TABLE, user_id)
            report.failed += 1
            continue

        if contract.state.is_decided:
            report.already_decided += 1
            continue
        if not is_sweepable(contract, today=today):
            report.too_recent += 1
            continue

        adopted = auto_adopt(contract, now=moment)
        kinds = [item.kind for item in adopted.items]

        # Named, not counted. "3 adopted" cannot be checked against anything;
        # "carried scan_jobs, draft_posts for week 2026-08-10" can.
        logger.info(
            "sweep: auto-adopted %s week of %s carrying %s",
            user_id,
            adopted.week_start,
            ", ".join(kinds) or "nothing",
        )
        report.carried.append((user_id, adopted.week_start.isoformat(), kinds))

        if dry_run:
            report.adopted += 1
            continue

        try:
            client.table(TABLE).upsert(
                to_row(user_id, adopted), on_conflict="user_id,week_start"
            ).execute()
        except Exception:
            logger.exception("could not save the adoption for %s", user_id)
            report.failed += 1
            # Undo the optimistic record. A report claiming an adoption that
            # did not land is the sweep lying about its own work.
            report.carried.pop()
            continue

        report.adopted += 1

    logger.info(
        "sweep complete: adopted=%d too_recent=%d already_decided=%d failed=%d%s",
        report.adopted,
        report.too_recent,
        report.already_decided,
        report.failed,
        " (dry run)" if dry_run else "",
    )
    return report


__all__ = ["GRACE", "SweepReport", "is_sweepable", "sweep"]
