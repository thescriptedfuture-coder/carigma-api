"""The Naukri lens: a bounded tune-up, over HTTP. Roadmap 7.1.

    GET  /naukri/score   what the lens shows          FREE
    POST /naukri/score   run the tune-up              10 on cycle start, then free

There is no `/naukri/fix`. It merged user-confirmed skills into a profile, and
the only thing that ever produced a confirmable skill was `score_key_skills` —
which is gone, because no JD corpus exists to score against. An endpoint that
writes to a profile with nothing on the other end reachable by a user is a
live write path with no gate in front of it, which is worse than dead code.
Fixes now describe the change and point at where it is made.

## The never-run state is the PRIMARY path

`naukri_scores` is empty and V1 never had Naukri scoring, so every existing
user lands on never-run at cutover. The GET returns it without writing a row —
**the absence of a row IS the never-run state** — and it carries two working
unlock actions rather than a dead end with good grammar.

## Billing

The charge goes through `services.runs.execute` like every other run. Per-cycle
pricing needed no protocol change: starting a cycle and continuing one are
different WORK, so they are different actions, and the cost stays a pure
function of the action.

    run_naukri   10   begins the bounded tune-up
    naukri_step  0    every step after (FREE_ACTIONS)

There is no second charging path here, and no cost override anywhere.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import enforce_agent_rate_limit
from carigma_api.services import credits as credits_service
from carigma_api.services import runs as runs_service
from carigma_api.services.credits import InsufficientCredits
from carigma_api.services.naukri import (
    MODEL_NOTE,
    NOT_YET_MODELLED,
    CycleState,
    Dimension,
    NaukriScore,
    UnlockAction,
    score_filter_fields,
    score_headline,
    should_persist,
    starts_new_cycle,
)
from carigma_api.services.repository import (
    ProfileRepository,
    SupabaseCreditStore,
    _as_rows,
    user_client,
)
from carigma_api.services.run_store import SupabaseRunStore
from carigma_api.services.runs import RunReporter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["naukri"])

TABLE = "naukri_scores"


def _deps(request: Request, settings: Settings) -> tuple[Any, ...]:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    client = user_client(settings, token)
    return (
        client,
        ProfileRepository(client),
        SupabaseCreditStore(client),
        SupabaseRunStore(client),
    )


def _latest(client: Any, user_id: str) -> dict[str, Any] | None:
    res = (
        client.table(TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = _as_rows(res.data if res else None)
    return rows[0] if rows else None


def _cycle_state_of(row: dict[str, Any]) -> CycleState:
    """Where the stored row says the cycle is.

    A value the enum does not know is a corrupted or future-written row, and
    there is no honest way to read it. It resolves to `needs_tuneup` — the FREE
    reading — because the two error directions are not symmetric: guessing
    free costs us a run, guessing paid charges someone twice for one cycle, and
    the credit rule exists to make the second impossible. Logged loudly, since
    the only way this happens is a bug on the write side.
    """
    raw = str(row.get("cycle_state") or "")
    try:
        return CycleState(raw)
    except ValueError:
        logger.error("naukri_scores row %s has an unknown cycle_state %r", row.get("id"), raw)
        return CycleState.NEEDS_TUNEUP


def _build_score(profile: dict[str, Any]) -> NaukriScore:
    """Score what we assess, and name what we do not assess yet.

    `profile` is the repository's shape — **camelCase**, produced by
    `db_to_profile`, not the database's snake_case columns. Reading
    `linkedin_headline` here returned None for every user who has a headline
    and reported "No headline on the profile yet", which is a fabricated
    finding about a real profile. A missing key looks exactly like an empty
    field, so the mistake is silent by construction.
    """
    dimensions = []
    fixes = []
    for dimension, fix in (
        score_headline(str(profile.get("linkedinHeadline") or "")),
        score_filter_fields(profile),
    ):
        dimensions.append(dimension)
        if fix is not None:
            fixes.append(fix)

    # Named, not scored. Weight 0, no action — the thing in the way is our
    # roadmap, and an action the product cannot honour is a lie with a click
    # target.
    dimensions.extend(Dimension.not_built(key) for key in NOT_YET_MODELLED)
    return NaukriScore(dimensions=dimensions, fixes=fixes)


def _preview_score() -> NaukriScore:
    """The lens before it has run. Nothing is scored, deliberately.

    The never-run read used to call `_build_score`, which measures whatever it
    can — so a free GET returned a real score, a real coverage percentage and
    a real list of fixes while the same payload said `cycle_state: never_run`.
    That is the whole tune-up given away on the read path, a payload
    contradicting itself, and a paid POST with nothing left to sell.

    The two assessed dimensions are UNAVAILABLE here — waiting on the run,
    which the user can do. The unbuilt five are NOT_BUILT in both states,
    because running changes nothing about them.
    """
    waiting = "The tune-up hasn't looked at this yet."
    run_it = UnlockAction(label="Run the tune-up", route="/signal/naukri")

    dimensions = [
        Dimension.unavailable(
            "headline",
            "Resume headline",
            waiting,
            unlocked_by="Run the tune-up and we'll score your headline against what Resdex ranks.",
            action=run_it,
        ),
        Dimension.unavailable(
            "filter_fields",
            "Filter fields",
            waiting,
            unlocked_by="Run the tune-up and we'll check which recruiter filters you pass.",
            action=run_it,
        ),
        *(Dimension.not_built(key) for key in NOT_YET_MODELLED),
    ]
    # No fixes. Nothing measured means nothing to fix, and inventing one would
    # be a finding about a profile we have never assessed.
    return NaukriScore(dimensions=dimensions, fixes=[])


@router.get("/naukri/score")
def get_naukri_score(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """What the lens shows. FREE — reading is not a run.

    With no row, returns the never-run state and **writes nothing**. That is
    the path every existing user is on, so it is the one that has to be right.
    """
    client, _, _, _ = _deps(request, settings)

    try:
        row = _latest(client, user.id)
    except Exception:
        logger.exception("could not read the naukri score for %s", user.id)
        # Could-not-look is not never-run. Rendering the never-run screen here
        # would tell the user to go and run a tune-up they may already have.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "read_failed", "message": "We couldn't load your Naukri tune-up."},
        ) from None

    if row is None:
        # No profile read at all: nothing on this screen depends on one, and a
        # query whose result is unused is a query that will grow a use.
        payload = _preview_score().as_dict()
        payload["cycle_state"] = str(CycleState.NEVER_RUN)
        payload["next_run_costs"] = credits_service.cost_of("run_naukri")
        return payload

    cycle_state = _cycle_state_of(row)
    return {
        # The row's `dimensions` column holds a whole scored payload, so this
        # spread carries state, coverage_note, dimensions and fixes with it.
        **(row.get("dimensions") or {}),
        "score": row.get("score"),
        "cycle_state": str(cycle_state),
        "optimized_at": row.get("optimized_at"),
        "parse_issues": row.get("parse_issues") or [],
        # A re-run mid-cycle is free; after `optimized` it starts a new one.
        "next_run_costs": credits_service.cost_of(
            "run_naukri" if starts_new_cycle(cycle_state) else "naukri_step"
        ),
        # Read from the constant, never from the blob. A payload written under
        # older weights would otherwise keep asserting provenance for them.
        "model_note": MODEL_NOTE,
    }


@router.post("/naukri/score")
async def run_naukri_tuneup(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Run the tune-up.

    Charged 10 on the run that BEGINS a cycle and nothing thereafter, because
    the cycle promises nothing nags between its steps. The action carries that
    — there is no branch here that decides a price.
    """
    client, profiles, credit_store, run_store = _deps(request, settings)

    enforce_agent_rate_limit(user.id, "naukri")

    try:
        row = _latest(client, user.id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "read_failed", "message": "We couldn't load your Naukri tune-up."},
        ) from exc

    latest_state = _cycle_state_of(row) if row else None
    action = "run_naukri" if starts_new_cycle(latest_state) else "naukri_step"

    try:
        credits_service.check_affordable(
            credit_store, user.id, action, enforced=settings.credits_enforced
        )
    except credits_service.CreditsUnavailable as exc:
        # We could not read the balance, so we cannot account for this run.
        # Refusing is not "charging on failure" — nothing is charged and no
        # work starts. Before this, an unreadable balance ran the work FREE.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="We could not check your credits just now. Try again in a moment.",
            headers={"X-Error-Code": "credits_unavailable"},
        ) from exc
    except InsufficientCredits as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"You need {exc.required} credits for the Naukri tune-up. You have {exc.balance}."
            ),
            headers={"X-Error-Code": "insufficient_credits"},
        ) from exc

    profile = profiles.load(user.id) or {}
    run = runs_service.new_run(user.id, "naukri")

    def work(reporter: RunReporter) -> dict[str, Any]:
        """Score, then STORE. The saving is part of the work, deliberately.

        A cycle is a stored thing — the whole point of the bounded grammar is
        that the next visit knows where you are. So a run that scores but
        cannot save has not delivered, and settling it as a failure means the
        one existing rule (no charge on failure) already covers it. The
        alternative was returning the score with a warning, which charged 10
        for a cycle that left no row: the next run would find no row, call
        itself a cycle start, and charge 10 again. Refunding would have meant a
        second credit path; this needs none.
        """
        reporter.step("score", "Reading your profile against live listings")
        score = _build_score(profile)
        reporter.complete_step()

        # An honest empty when nothing could be assessed, so `execute` reaches
        # its own no-charge branch. Returning the full dict of unavailables
        # instead would be non-empty by len(), so it would BILL for a run that
        # stores nothing. The rule that nothing unassessable is stored and the
        # rule that nothing empty is charged only agree if the work says
        # "empty" out loud.
        if not should_persist(score):
            return {}

        reporter.step("save", "Saving your tune-up")
        payload = score.as_dict()
        client.table(TABLE).insert(
            {
                "user_id": user.id,
                "score": score.score,
                "dimensions": payload,
                "parse_issues": [],
                "cycle_state": str(CycleState.NEEDS_TUNEUP),
                "created_at": datetime.now(UTC).isoformat(),
            }
        ).execute()
        reporter.complete_step()
        return payload

    settled = await runs_service.execute(
        run, work, credit_store=credit_store, action=action, run_store=run_store
    )

    if settled.status is runs_service.RunStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "upstream", "message": "The tune-up couldn't run. Nothing charged."},
            headers={"X-Error-Code": "upstream_unavailable"},
        )

    if settled.status is runs_service.RunStatus.EMPTY:
        never_run = _preview_score().as_dict()
        never_run["cycle_state"] = str(CycleState.NEVER_RUN)
        never_run["next_run_costs"] = credits_service.cost_of("run_naukri")
        never_run["credits"] = settled.credits.as_dict() if settled.credits else None
        return never_run

    result = dict(settled.result or {})
    result["cycle_state"] = str(CycleState.NEEDS_TUNEUP)
    result["next_run_costs"] = credits_service.cost_of("naukri_step")
    result["credits"] = settled.credits.as_dict() if settled.credits else None
    return result


__all__ = ["router"]
