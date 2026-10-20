"""Profile Analyst endpoints — the first fully extracted agent.

Proves the whole P2 spine end-to-end: verified JWT → stateless service →
run protocol → credit rule → persisted result.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import enforce_agent_rate_limit, idempotency_key, replay_if_seen
from carigma_api.services import credits as credits_service
from carigma_api.services import runs as runs_service
from carigma_api.services.agents import profile_analyst
from carigma_api.services.analysis_store import AnalysisStore
from carigma_api.services.clock import local
from carigma_api.services.constants import CREDIT_COSTS, VARIANCE_NOTE, score_band
from carigma_api.services.credits import InsufficientCredits
from carigma_api.services.repository import (
    ProfileRepository,
    ScoreRepository,
    SupabaseCreditStore,
    user_client,
)
from carigma_api.services.run_store import SupabaseRunStore, replay_payload
from carigma_api.services.runs import AgentRun, RunNotStored, RunReporter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["score"])


class ComputeScoreRequest(BaseModel):
    # The FIRST score is free — it is the product's promise, and charging for it
    # would gate the one thing that proves the value (contract §2).
    onboarding: bool = False
    #: Answer at once with the run's id, and do the work after the answer —
    #: the client polls `GET /agents/runs/{id}`. Off by default so onboarding,
    #: which still waits on the request, is unchanged until it is converted.
    background: bool = False


class ApplyFixRequest(BaseModel):
    fix_key: str = Field(min_length=1)
    rewrite: str = Field(min_length=1)


def _deps(request: Request, user: Any, settings: Settings) -> tuple[Any, Any, Any, Any]:
    """Build per-request, per-user Supabase-backed collaborators.

    Everything that needs the caller's own client is built HERE, in one place,
    so a test that substitutes this substitutes all of it. An extra
    `user_client(...)` call elsewhere in the handler would slip past that.
    """
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    client = user_client(settings, token)
    return (
        ProfileRepository(client),
        ScoreRepository(client),
        SupabaseCreditStore(client),
        SupabaseRunStore(client),
    )


def _analyses(request: Request, settings: Settings) -> AnalysisStore:
    """The stored analysis, through the caller's own client (RLS)."""
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return AnalysisStore(user_client(settings, token))


#: V1's re-sync nudge: a reading older than this is offered a re-run, beside
#: the reading itself.
RESYNC_AFTER_DAYS = 30


@router.get("/score/latest")
def latest_score(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The last reading, with the day it was taken. Free.

    Signal refused to show any score it had not measured in the session —
    "We won't show a score we haven't just measured" — to a user with
    twenty-two readings. A stored reading with its date is a record: the
    surface labels it ("Measured 11 Aug · 69 days ago") and offers a re-run.
    """
    try:
        stored = _analyses(request, settings).latest(user.id)
    except Exception as exc:
        logger.exception("could not read the stored analysis for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "read_failed",
                "message": "We couldn't load your last score just now.",
            },
        ) from exc

    run_cost = CREDIT_COSTS["run_profile"]
    if stored is None:
        return {
            "analysis": None,
            "measured_at": None,
            "measured_on": None,
            "age_days": None,
            "stale": False,
            "run_cost": run_cost,
        }
    age = (
        (local(datetime.now(UTC)).date() - stored.measured_on).days if stored.measured_on else None
    )
    return {
        "analysis": _shown(stored.analysis),
        "measured_at": stored.measured_at.isoformat() if stored.measured_at else None,
        "measured_on": stored.measured_on.isoformat() if stored.measured_on else None,
        "age_days": age,
        # Unknown age is not "fresh" and not "stale": nothing is claimed.
        "stale": age is not None and age >= RESYNC_AFTER_DAYS,
        "run_cost": run_cost,
    }


def _shown(analysis: dict[str, Any]) -> dict[str, Any]:
    """The reading in the shape Signal renders — V1's rows carry more keys
    (`weeklyGoal`, `topKeywordsToAdd` ...) and none of `band` or the variance
    note, which V2 adds to every reading it takes. Both are derived here from
    the number, so a V1 reading renders exactly as a V2 one does."""
    score = int(analysis["profileScore"])
    breakdown = analysis.get("scoreBreakdown")
    wins = analysis.get("quickWins")
    return {
        "profileScore": score,
        "band": score_band(score),
        "scoreBreakdown": {
            str(k): int(v)
            for k, v in (breakdown.items() if isinstance(breakdown, dict) else ())
            if isinstance(v, int | float)
        },
        "scoreSummary": str(analysis.get("scoreSummary") or ""),
        "optimizedHeadline": str(analysis.get("optimizedHeadline") or ""),
        "optimizedAbout": str(analysis.get("optimizedAbout") or ""),
        "quickWins": [
            {
                "action": str(w.get("action") or ""),
                "impact": str(w.get("impact") or ""),
                "effort": str(w.get("effort") or ""),
            }
            for w in (wins if isinstance(wins, list) else [])
            if isinstance(w, dict) and w.get("action")
        ],
        "variance_note": VARIANCE_NOTE,
    }


#: A run in the background writes with the caller's own token after the
#: response has gone — its steps, the charge, the reading, the finish. A token
#: that expires mid-run turns every one of those writes into a 401: nothing is
#: charged, and nothing is kept, including the row the page is polling. So a
#: background run starts only on a token that outlives a long run. The web
#: refreshes its session before starting one; this is the backstop.
BACKGROUND_TOKEN_SECONDS = 300


def _token_seconds_left(user: Any) -> float | None:
    exp = (getattr(user, "claims", None) or {}).get("exp")
    return float(exp) - time.time() if isinstance(exp, int | float) else None


@router.post("/score/compute", status_code=status.HTTP_200_OK)
async def compute_score(
    body: ComputeScoreRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Any:
    """Run the Profile Analyst.

    ## Two ways to wait

    **`background: false`** (onboarding): the request is held open for the
    whole run and answers with the reading.

    **`background: true`** (Signal): answers `202` at once with the run —
    its id, `status: running`, its steps — and does the work after the
    answer has gone. The page polls `GET /agents/runs/{id}`.

    Held open, a 30–60 s run is a request a network can drop on the way
    back: on 19 Oct three runs SUCCEEDED, were charged and kept, and the page
    received none of the answers. A run the page can ask about cannot be
    lost that way.

    ## The credit rule is unchanged

    `runs_service.execute` is still the only path that can charge, after a
    non-empty success. The background task is that same call, made later,
    with the caller's own client — the user's token, so RLS sees `auth.uid()`
    for every write, exactly as it did inside the request. No service key.
    """
    profiles, scores, credit_store, run_store = _deps(request, user, settings)

    # A repeated Idempotency-Key is answered from the original run BEFORE the
    # rate limiter and the credit check — a retry of work already done is not a
    # new request, and must be neither throttled nor charged again.
    if (replayed := replay_if_seen(run_store, user.id, request, agent="profile")) is not None:
        return replayed

    if body.background:
        left = _token_seconds_left(user)
        if left is not None and left < BACKGROUND_TOKEN_SECONDS:
            # Before the limiter, so a refusal costs the caller nothing.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "session_expiring",
                    "message": "Your sign-in needs refreshing before a run. Try again.",
                },
                headers={"X-Error-Code": "session_expiring"},
            )

    # Limiter 1 (§21 Q7). The credit gate below stops cost; this stops abuse.
    enforce_agent_rate_limit(user.id, "profile")

    action = "onboarding_score" if body.onboarding else "run_profile"

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
                f"You need {exc.required} credits to run the Profile Analyst. "
                f"You have {exc.balance}."
            ),
            headers={"X-Error-Code": "insufficient_credits"},
        ) from exc

    profile = profiles.load(user.id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Add your profile details first — the analyst needs something to read.",
        )

    run = runs_service.new_run(user.id, "profile")
    if key := idempotency_key(request):
        run_store.remember_idempotency(user.id, key, run.id)

    analyses = _analyses(request, settings)

    def work(reporter: RunReporter) -> dict[str, Any]:
        reporter.step("analyse", "Reading your profile")
        result = profile_analyst.run(profile, api_key=settings.anthropic_api_key)
        reporter.complete_step()
        # Kept BEFORE the run is settled, not after: a page polling the run
        # refetches the score the moment it reads `succeeded`, and a reading
        # written after that moment is a reading the page misses.
        reporter.step("keep", "Keeping the reading")
        reporter.complete_step(failed=not _keep(user.id, result, run, scores, analyses))
        return result

    # Recorded as started before any work, so a retry under the same key —
    # or a duplicate racing this one — finds THIS run and is not charged.
    try:
        holder = runs_service.begin(run, run_store)
    except RunNotStored as exc:
        if body.background:
            # The row is the answer a background run gives. Without it the
            # first poll would be told there is no such run.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="We couldn't start the analyst just now. Try again in a moment.",
                headers={"X-Error-Code": "run_not_started"},
            ) from exc
        # Held open, the answer rides on this response; the record is
        # best-effort, as it always was.
        holder = None
    if holder is not None:
        return replay_payload(holder)

    if body.background:
        background_tasks.add_task(
            _settle_in_background,
            run,
            work,
            credit_store=credit_store,
            action=action,
            run_store=run_store,
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=run.as_dict())

    settled = await runs_service.execute(
        run, work, credit_store=credit_store, action=action, run_store=run_store
    )

    if settled.status is runs_service.RunStatus.FAILED:
        # No credits were charged — see services/runs.execute.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=runs_service.friendly_error(settled.error),
            headers={"X-Error-Code": "upstream_unavailable"},
        )

    return {
        "result": settled.result,
        "credits": settled.credits.as_dict() if settled.credits else None,
        "provenance": {
            "agent_label": "Profile Analyst",
            "ran_at": settled.started_at.isoformat(),
        },
    }


def _keep(user_id: str, result: Any, run: AgentRun, scores: Any, analyses: AnalysisStore) -> bool:
    """Record the score and keep the whole reading. True when both landed.

    Only a real result — never persist a fabricated one. Not fatal either
    way: the user is about to see this reading, and losing the copy costs a
    record, not the answer. The step says which happened.
    """
    if credits_service.is_empty_result(result):
        return True
    kept = True
    try:
        scores.record(user_id, int(result["profileScore"]))
    except Exception:
        logger.exception("Score history write failed for %s", user_id)
        kept = False
    # The whole reading, so Signal can show it next time with its date.
    try:
        analyses.save(user_id, result, at=run.started_at)
    except Exception:
        logger.exception("could not keep the analysis for %s", user_id)
        kept = False
    return kept


async def _settle_in_background(run: AgentRun, work: Any, **kwargs: Any) -> None:
    """`execute`, after the answer has gone. It does not raise — but this is
    the last frame anything could reach, so nothing escapes it unlogged."""
    try:
        await runs_service.execute(run, work, **kwargs)
    except Exception:  # noqa: BLE001 — there is no caller left to tell
        logger.exception("background run %s did not settle", run.id)


@router.post("/score/fix")
async def apply_fix(
    body: ApplyFixRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Apply an analyst rewrite to the stored profile.

    This WRITES the rewrite before returning. That is the whole fix for V1's
    "score never improves" bug: a flag alone left the next run scoring stale
    data. Free — the user is accepting our suggestion, not buying anything.
    """
    profiles, _scores, _credits, _runs = _deps(request, user, settings)

    profile = profiles.load(user.id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    if body.fix_key not in profile_analyst.SCORE_FIX_FIELDS:
        raise HTTPException(
            # Literal 422: Starlette renamed the constant, and pinning to either
            # spelling couples us to a version for no benefit.
            status_code=422,
            detail=(
                f"'{body.fix_key}' isn't an applyable fix. "
                f"Applyable: {', '.join(sorted(profile_analyst.SCORE_FIX_FIELDS))}."
            ),
        )

    updated = profile_analyst.apply_score_fix(profile, body.fix_key, body.rewrite)
    saved = profiles.save(user.id, updated)

    field = profile_analyst.SCORE_FIX_FIELDS[body.fix_key]
    return {"applied": True, "field": field, "profile": saved}


@router.get("/score/history")
async def score_history(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = 60,
) -> dict[str, Any]:
    _profiles, scores, _credits, _runs = _deps(request, user, settings)
    return {"items": scores.history(user.id, min(max(limit, 1), 200))}
