"""The Naukri lens: a bounded tune-up, over HTTP. Roadmap 7.1.

    GET  /naukri/score   what the lens shows          FREE
    POST /naukri/score   run the tune-up              10 on cycle start, then free
    POST /naukri/fix     apply a confirmed fix        free (mid-cycle)

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
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import enforce_agent_rate_limit
from carigma_api.services import credits as credits_service
from carigma_api.services import runs as runs_service
from carigma_api.services.credits import InsufficientCredits
from carigma_api.services.naukri import (
    MODEL_NOTE,
    CycleState,
    Dimension,
    NaukriScore,
    UnlockAction,
    cap_repeats,
    score_filter_fields,
    score_headline,
    score_key_skills,
    score_parseability,
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


class FixRequest(BaseModel):
    key: str = Field(min_length=1, max_length=40)
    #: Only skills the user has TICKED. The API never promotes a candidate on
    #: its own — 7.1's hard rule is that every suggested skill is confirmed as
    #: true by the person whose profile it is.
    confirmed_skills: list[str] = Field(default_factory=list)


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


def _build_score(profile: dict[str, Any], jd_skills: tuple[str, ...]) -> NaukriScore:
    """Score every dimension we can, and honestly decline the rest.

    `profile` is the repository's shape — **camelCase**, produced by
    `db_to_profile`, not the database's snake_case columns. Reading
    `linkedin_headline` here returned None for every user who has a headline
    and reported "No headline on the profile yet", which is a fabricated
    finding about a real profile. A missing key looks exactly like an empty
    field, so the mistake is silent by construction.
    """
    skills = cap_repeats(tuple(str(profile.get("skills") or "").split(",")))

    dimensions = []
    fixes = []
    for dimension, fix in (
        score_key_skills(skills, jd_skills),
        score_headline(str(profile.get("linkedinHeadline") or "")),
        # No resume text is retained, so there is nothing to detect hazards in.
        # `None` is the honest argument, NOT an empty tuple: a user who has not
        # been analysed is not a user whose resume parses cleanly.
        score_parseability(None),
        score_filter_fields(profile),
    ):
        dimensions.append(dimension)
        if fix is not None:
            fixes.append(fix)
    return NaukriScore(dimensions=dimensions, fixes=fixes)


def _preview_score() -> NaukriScore:
    """The lens before it has run. Nothing is scored, deliberately.

    The never-run read used to call `_build_score`, which measures whatever it
    can — so a free GET returned a real score, a real coverage percentage and
    a real list of fixes while the same payload said `cycle_state: never_run`.
    That is the whole tune-up given away on the read path, a payload
    contradicting itself, and a paid POST with nothing left to sell.

    The two kinds of absence keep their own copy, because they are undone by
    different things: `headline` and `filter_fields` are waiting on the run,
    while `key_skills` and `parseability` would still be unavailable after it.
    Collapsing them into one sentence would promise that running the tune-up
    unlocks all four.
    """
    waiting = "The tune-up hasn't looked at this yet."
    run_it = UnlockAction(label="Run the tune-up", route="/signal/naukri")

    dimensions = [
        score_key_skills((), ())[0],
        Dimension.unavailable(
            "headline",
            "Resume headline",
            waiting,
            unlocked_by="Run the tune-up and we'll score your headline against what Resdex ranks.",
            action=run_it,
        ),
        score_parseability(None)[0],
        Dimension.unavailable(
            "filter_fields",
            "Filter fields",
            waiting,
            unlocked_by="Run the tune-up and we'll check which recruiter filters you pass.",
            action=run_it,
        ),
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
        credits_service.check_affordable(credit_store, user.id, action)
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
        score = _build_score(profile, ())
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


@router.post("/naukri/fix")
def apply_naukri_fix(
    body: FixRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Record a fix the user confirmed. Free — mid-cycle work already paid for.

    Only `confirmed_skills` are accepted, and they are capped for repetition on
    the way in. Nothing is promoted from `candidate_skills` automatically: 7.1's
    rule is that a suggested skill must be confirmed TRUE by the person whose
    profile it is, and an API that could self-confirm would make that rule
    advisory.
    """
    _, profiles, _, _ = _deps(request, settings)

    confirmed = cap_repeats(tuple(body.confirmed_skills))
    if body.confirmed_skills and not confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "nothing_confirmed", "message": "No usable skills in that list."},
        )

    profile = profiles.load(user.id) or {}
    existing = cap_repeats(tuple(str(profile.get("skills") or "").split(",")))
    merged = cap_repeats((*existing, *confirmed))

    try:
        profiles.save(user.id, {"skills": ", ".join(s.strip() for s in merged if s.strip())})
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": "We couldn't save that just now. Your profile is unchanged.",
            },
        ) from exc

    return {
        "key": body.key,
        "applied": list(confirmed),
        # Free, and said so — the user agreed to one charge for the cycle.
        "charged": 0,
        "cycle_state": str(CycleState.NEEDS_TUNEUP),
    }


__all__ = ["router"]
