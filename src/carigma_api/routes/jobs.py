"""The jobs feed and Career Scout. Roadmap §9.

    GET  /jobs/feed                free — the main list, and the older postings
    GET  /jobs/{id}                free
    GET  /jobs/{id}/apply-kit      free — returns the STORED kit, or 404
    GET  /jobs/{id}/cv.pdf         free — the kit was already paid for
    POST /jobs/{id}/not-for-me     free — records a decision
    POST /jobs/scan                15 credits, and nothing when it finds nothing

`scan` is the only priced endpoint here, and
`test_only_the_scan_charges_anything` holds every other one to free.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import enforce_agent_rate_limit, idempotency_key, replay_if_seen
from carigma_api.services import credits as credits_service
from carigma_api.services import cv_pdf
from carigma_api.services import runs as runs_service
from carigma_api.services.ai import UpstreamError, call_claude_json
from carigma_api.services.credits import InsufficientCredits
from carigma_api.services.jobs import (
    AdzunaProvider,
    JobsGateway,
    JSearchProvider,
    quota,
    scoring,
    store,
)
from carigma_api.services.jobs.feed import DEFAULT_STATUSES, present_feed, present_job
from carigma_api.services.repository import (
    ProfileRepository,
    SupabaseCreditStore,
    _as_rows,
    user_client,
)
from carigma_api.services.run_store import SupabaseRunStore
from carigma_api.services.runs import RunReporter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])

TABLE = "jobs_feed"


class NotForMe(BaseModel):
    #: Free text, because the useful reasons are the ones we did not think of.
    #: V1 stored strings like "Salary too low" and they are the signal.
    reason: str = Field(min_length=1, max_length=200)


def _db(request: Request, settings: Settings) -> Any:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return user_client(settings, token)


def _unavailable(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "read_failed", "message": f"We couldn't load {what} just now."},
    )


@router.get("/jobs/feed")
def get_jobs_feed(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Every job we have surfaced for THIS user.

    Scoped by `user_id` as well as by RLS. The filter is defence in depth, not
    the boundary — the boundary is the database, and this reads through the
    caller's own token so `auth.uid()` is real.
    """
    db = _db(request, settings)
    try:
        res = (
            db.table(TABLE)
            .select("*")
            .eq("user_id", user.id)
            .in_("status", list(DEFAULT_STATUSES))
            .order("match_score", desc=True)
            .execute()
        )
    except Exception as exc:
        logger.exception("could not read the jobs feed for %s", user.id)
        raise _unavailable("your job matches") from exc

    return present_feed(_as_rows(res.data if res else None))


def _one(db: Any, user_id: str, job_id: int) -> dict[str, Any]:
    try:
        res = db.table(TABLE).select("*").eq("id", job_id).eq("user_id", user_id).execute()
    except Exception as exc:
        logger.exception("could not read job %s for %s", job_id, user_id)
        raise _unavailable("that job") from exc

    rows = _as_rows(res.data if res else None)
    if not rows:
        # A job belonging to someone else is a 404, not a 403: telling a caller
        # that an id exists but is not theirs is itself a disclosure.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "We don't have that job."},
        )
    return rows[0]


@router.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    row = _one(_db(request, settings), user.id, job_id)
    return present_job(row, now=datetime.now(UTC))


@router.get("/jobs/{job_id}/apply-kit")
def get_apply_kit(
    job_id: int,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The stored kit, or an honest 404.

    Nine of the ninety-three stored rows have one. This returns what was saved
    and never generates on read — a kit conjured here would be unpaid work
    presented as a record of work already done.
    """
    row = _one(_db(request, settings), user.id, job_id)
    kit = row.get("apply_kit")
    if not isinstance(kit, dict) or not kit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "no_kit",
                "message": "No apply kit has been built for this job yet.",
            },
        )
    return {
        **kit,
        "created_at": row.get("kit_created_at"),
        # Shipped in the payload rather than hard-coded in a component, so a
        # surface cannot render the kit without it.
        "disclaimer": "Review and adjust before sending — Carigma drafts, you verify.",
    }


def build_gateway(settings: Settings, db: Any, user_id: str) -> JobsGateway:
    """The providers, in fallback order, each one counted at the call.

    JSearch first because its coverage of Indian postings is better; Adzuna is
    the top-up when JSearch returns thin. Both are wrapped in
    `RecordedProvider`, so no path reaches the network without first leaving
    the row the daily cap counts — and if that row cannot be written, the call
    is not made.
    """
    return JobsGateway(
        [
            quota.RecordedProvider(
                JSearchProvider(api_key=settings.jsearch_api_key, host=settings.jsearch_host),
                db,
                user_id=user_id,
            ),
            quota.RecordedProvider(
                AdzunaProvider(app_id=settings.adzuna_app_id, app_key=settings.adzuna_app_key),
                db,
                user_id=user_id,
            ),
        ]
    )


#: Spec 3.4: "Rescan now (rate-limit 1/6h; disabled tooltip `Next scan at
#: 15:12`)". V1 enforces it server-side. The first V2 scan did not, and the
#: second real scan ran six minutes after the first, cost 15 credits, and found
#: one new posting.
RESCAN_AFTER = timedelta(hours=6)


def last_scan_at(db: Any, user_id: str) -> datetime | None:
    """When this user's last scan that FOUND something finished.

    A failed or empty scan does not start the clock — it charged nothing and
    told the user nothing, and making them wait six hours to try again after a
    provider timeout would punish them for our bad day.
    """
    try:
        res = (
            db.table("agent_runs")
            .select("finished_at,status")
            .eq("user_id", user_id)
            .eq("agent", "jobs")
            .eq("status", "succeeded")
            .execute()
        )
    except Exception:
        logger.exception("could not read the last scan for %s", user_id)
        return None
    stamps = []
    for row in res.data or []:
        try:
            when = datetime.fromisoformat(str(row.get("finished_at")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        stamps.append(when if when.tzinfo else when.replace(tzinfo=UTC))
    return max(stamps) if stamps else None


def _span(delta: timedelta) -> str:
    """A duration as a person says it. Relative, because a clock time needs a
    timezone and the server does not know the reader's: "opens at 10:47 UTC"
    is a sum an IST user has to do in their head, and gets wrong by 5:30."""
    minutes = max(1, round(delta.total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes / 60
    if hours < 1.5:
        return "about an hour"
    return f"about {round(hours)} hours"


#: A provider that ANSWERED, possibly with nothing. `jsearch: 0` is a search
#: that ran and matched nothing; `jsearch: unavailable` is one that did not run.
_ANSWERED = re.compile(r"^\w+: \d+$")


def _scorer(settings: Settings) -> scoring.Scorer:
    """The ranking call, bound to the real client.

    Separate from `build_gateway` so a test can replace one without the other:
    a scan with a working provider and a dead model is a real state, and it is
    the one where V1 invented a 70% score.
    """

    def call(system: str, user: str, max_tokens: int) -> dict[str, Any]:
        result = call_claude_json(system, user, max_tokens, api_key=settings.anthropic_api_key)
        return result if isinstance(result, dict) else {}

    return call


@router.post("/jobs/scan")
async def scan(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Career Scout. 15 credits (`run_jobs`), and nothing on a failure.

    The order below is the one the posts route learned the hard way: replay,
    then rate limit, then the STRUCTURAL refusals, then credits. You should not
    have to be able to afford something to be told it is impossible.

    The structural refusal here is the shared quota. V1 and V2 draw on one
    JSearch key and live V1 users have first claim, so a scan that would exceed
    today's cap is refused before any question about credits and before any
    provider call. `quota.refusal` also refuses when it CANNOT COUNT, because
    zero and unknown must not read the same when the resource is somebody
    else's.
    """
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    client = user_client(settings, token)
    credit_store = SupabaseCreditStore(client)
    run_store = SupabaseRunStore(client)
    action = "run_jobs"

    if (replayed := replay_if_seen(run_store, user.id, request, agent="jobs")) is not None:
        return replayed

    enforce_agent_rate_limit(user.id, "jobs")

    profile = ProfileRepository(client).load(user.id)
    role = str(profile.get("targetRoles") or "").split(",")[0].strip()
    city = str(profile.get("location") or "").strip()
    if not role:
        # Scanning for "" returns whatever the provider feels like. A scan with
        # no target is not a scan, and it would spend shared quota to find out.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "no_target_role",
                "message": "Set a target role first — a scan needs something to look for.",
            },
        )

    db = _db(request, settings)

    # The six-hour gate, before the quota and before credits: it is the
    # cheapest refusal and it is about this user alone.
    previous = last_scan_at(db, user.id)
    now = datetime.now(UTC)
    if previous is not None and now - previous < RESCAN_AFTER:
        ready = previous + RESCAN_AFTER
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "scanned_recently",
                "message": (
                    f"Your last scan finished {_span(now - previous)} ago. The next one opens "
                    f"in {_span(ready - now)} — new postings rarely appear faster than that, "
                    "and a scan that finds nothing new still costs 15 credits. "
                    "Nothing was charged."
                ),
                "next_scan_at": ready.isoformat(),
            },
            headers={"X-Error-Code": "scanned_recently"},
        )

    if (blocked := quota.refusal(db, cap=settings.jsearch_daily_cap)) is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "quota_reached", "message": blocked},
            headers={"X-Error-Code": "quota_reached"},
        )

    try:
        credits_service.check_affordable(
            credit_store, user.id, action, enforced=settings.credits_enforced
        )
    except credits_service.CreditsUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "credits_unavailable",
                "message": (
                    "We couldn't check your credits just now, so you weren't "
                    "charged and nothing ran. Try again in a moment."
                ),
            },
            headers={"X-Error-Code": "credits_unavailable"},
        ) from exc
    except InsufficientCredits as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(f"A scan costs {exc.required} credits. You have {exc.balance}."),
            headers={"X-Error-Code": "insufficient_credits"},
        ) from exc

    run = runs_service.new_run(user.id, "jobs")
    if key := idempotency_key(request):
        run_store.remember_idempotency(user.id, key, run.id)

    gateway = build_gateway(settings, db, user.id)
    #: What the search said, kept outside `work` because an EMPTY run returns
    #: `{}` — the only shape `runs.execute` will not charge for — and the
    #: notes would otherwise be lost with it.
    searched: dict[str, Any] = {"notes": []}

    def work(reporter: RunReporter) -> dict[str, Any]:
        reporter.step("search", f"Searching for {role} in {city or 'your area'}")
        found, notes = gateway.search(role=role, city=city)
        searched["notes"] = notes
        if not found:
            if any(_ANSWERED.match(note) for note in notes):
                # A source ANSWERED and matched nothing. That is a real result
                # — "no postings for this role in this city today" — and it is
                # free: `{}` is the empty result `runs.execute` will not bill.
                return {}
            # Nobody answered. That is a failure, and it says so.
            raise UpstreamError("; ".join(notes) or "No job source returned anything.")

        reporter.step("rank", f"Ranking {len(found)} postings against your profile")
        annotations = scoring.score(found, profile, scorer=_scorer(settings))

        reporter.step("save", "Saving what is new")
        saved = store.persist(db, user_id=user.id, jobs=found)
        scores = store.annotate(
            db, user_id=user.id, jobs=found, annotations=annotations, stored=saved.details
        )

        return {
            "found": len(found),
            "new": len(saved.new),
            "seen_again": saved.seen_again,
            "scored": len(annotations),
            "sources": list(gateway.provider_names),
            "notes": notes,
            "searched_for": {"role": role, "city": city or None},
            # NAMED, not only counted. "3 new roles — marked New today below"
            # pointed at three rows scored 18, 45 and 48, which the score sort
            # had put in the middle of a 58-row list. A count tells someone
            # something happened; a name tells them where to look.
            "new_jobs": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "company": row["company"],
                    "match_score": scores.get(row["dedup_key"]),
                }
                for row in saved.new
            ],
        }

    settled = await runs_service.execute(
        run, work, credit_store=credit_store, action=action, run_store=run_store
    )

    if settled.status is runs_service.RunStatus.EMPTY:
        # A search ran and matched nothing. Not an error — a 200 with the
        # facts, so the surface can say what was searched for rather than
        # pretend something broke.
        return {
            "found": 0,
            "new": 0,
            "seen_again": 0,
            "scored": 0,
            "sources": list(gateway.provider_names),
            "notes": searched["notes"],
            "searched_for": {"role": role, "city": city or None},
            "new_jobs": [],
            "credits": settled.credits.as_dict() if settled.credits else None,
        }

    if settled.status is not runs_service.RunStatus.SUCCEEDED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "scan_failed",
                "message": "We couldn't reach the job sources just now. You weren't charged.",
            },
            headers={"X-Error-Code": "scan_failed"},
        )

    return {
        **settled.result,
        "credits": settled.credits.as_dict() if settled.credits else None,
    }


@router.get("/jobs/{job_id}/cv.pdf")
def download_cv(
    job_id: int,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    template: str | None = None,
) -> Response:
    """The tailored CV as a file. Free.

    V2 could preview a CV and not produce one — `Download PDF` was removed when
    it turned out to have no handler, so the artifact most likely to be sent to
    an employer could be read on screen and nowhere else.

    Free because the kit already cost 15 credits, and this calls no model and
    reaches no provider. V1 priced it identically.
    """
    row = _one(_db(request, settings), user.id, job_id)
    kit = row.get("apply_kit")
    cv = kit.get("cv") if isinstance(kit, dict) else None
    if not isinstance(cv, dict) or not cv:
        # A PDF rendered from nothing is a page with a name on it and no
        # career, and it would arrive looking like a product that works.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "no_kit",
                "message": "No apply kit has been built for this job yet.",
            },
        )

    pdf, chosen, filename = cv_pdf.render(cv, template)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Which layout actually came back, since an unknown key falls back
            # to the default rather than failing.
            "X-Carigma-Template": chosen,
        },
    )


@router.post("/jobs/{job_id}/not-for-me")
def dismiss_job(
    job_id: int,
    body: NotForMe,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Record why this one was wrong. Free — it is the user teaching us.

    V1's bug B was that the reason did not save, which made the whole gesture
    theatre. The response echoes the stored reason so the caller can tell the
    difference between "recorded" and "accepted and dropped".
    """
    db = _db(request, settings)
    _one(db, user.id, job_id)

    try:
        res = (
            db.table(TABLE)
            .update({"status": "dismissed", "dismiss_reason": body.reason.strip()})
            .eq("id", job_id)
            .eq("user_id", user.id)
            .execute()
        )
    except Exception as exc:
        logger.exception("could not dismiss job %s for %s", job_id, user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": "We couldn't save that just now. The job is unchanged.",
            },
        ) from exc

    rows = _as_rows(res.data if res else None)
    stored = rows[0] if rows else {}
    return {
        "id": job_id,
        "status": stored.get("status") or "dismissed",
        # Read back from the row, not echoed from the request. The point of the
        # fix is that the reason is stored, and only the row can prove that.
        "dismiss_reason": stored.get("dismiss_reason"),
    }


__all__ = ["router"]
