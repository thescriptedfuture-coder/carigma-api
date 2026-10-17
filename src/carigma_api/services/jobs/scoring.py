"""Ranking real postings against a real profile. Nothing here invents a job.

A port of V1's Opportunity Scout ranking pass, with one deliberate difference.

## V1 fails open with a score it made up

    "matchScore": int(a.get("matchScore") or 70),
    "whyMatch":   a.get("whyMatch") or
                  "A real, current opening matching your target role and location."

A job the model did not return — because the call failed, or because the JSON
array truncated — is shown at **70% with a sentence nobody wrote about it**.
V1's own comment records this firing: *"too small and the array truncates →
parse fails → every job falls back to a neutral 70% (the bug seen in the first
live run)"*. They saw it happen and kept the default.

That is a fabricated score presented as a measurement, and this product does
not do that. **An unscored job here carries `match_score: None`,
`why_match: None` and no skills keys at all** — which is precisely the state
`present_job` and the jobs surface already render as "we have not compared your
skills against this posting". Twenty-seven live rows are in it already.

Fail-open is still right: a real posting is worth showing without the fit copy.
What changes is that the absence looks like absence.

## The model never touches the facts

Title, company, location, salary and link come from the provider and are not in
the model's output at all — it is handed a listing and returns an annotation
keyed by index. There is no path by which a hallucinated company reaches a row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from carigma_api.services.jobs.models import NormalizedJob

logger = logging.getLogger(__name__)

#: Ranking ~15 jobs with six fields each produces a lot of JSON. V1 found the
#: hard way that too small a budget truncates the array; the difference is that
#: a truncation here costs the annotations rather than inventing them.
MAX_TOKENS = 8000

#: How much of a description the model sees. Long enough to judge, short enough
#: that fifteen of them fit in one call.
JD_CHARS = 800

SYSTEM = (
    "You are Career Scout. You are given REAL, already-sourced job postings and "
    "you RANK them for the candidate. HARD RULES: never invent or change any "
    "company, title, location, salary or link — only add your assessment; base "
    "every judgement on the ACTUAL description text; if a description is too thin "
    "to judge, say so in `why_match` and score conservatively. Return ONLY a raw "
    "JSON object with an `items` array."
)


class Scorer(Protocol):
    """The JSON-returning model call. Injected so tests never hit the API."""

    def __call__(self, system: str, user: str, max_tokens: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Annotation:
    """What the model added to one posting. Every field may be absent.

    `match_score is None` means NOT SCORED — not zero, and not a neutral 70.
    """

    match_score: int | None = None
    why_match: str | None = None
    matched_skills: list[str] | None = None
    missing_skills: list[str] | None = None
    key_requirements: list[str] | None = None


def _listing(jobs: list[NormalizedJob]) -> str:
    return "\n".join(
        f"id={i} | {job.title} @ {job.company} | {job.location} | "
        f"{job.salary or 'salary n/a'}\n   JD: {(job.description or '')[:JD_CHARS]}"
        for i, job in enumerate(jobs)
    )


def _prompt(jobs: list[NormalizedJob], profile: dict[str, Any]) -> str:
    return f"""Candidate: {profile.get("name") or ""}
Current role: {profile.get("currentRole") or ""}
Target roles: {profile.get("targetRoles") or ""}
Skills: {profile.get("skills") or ""}
Location: {profile.get("location") or ""}

REAL job postings (the facts are fixed — rank and annotate only):
{_listing(jobs)}

Return {{"items": [...]}} with one object per posting:
- id (the same integer)
- match_score (0-100, realistic — judge the real JD against this candidate)
- why_match (1-2 sentences, grounded in THIS posting's actual JD)
- matched_skills (array — the candidate's skills that actually appear in the JD)
- missing_skills (array — real JD requirements the candidate lacks)
- key_requirements (array of up to 4 short strings drawn from the JD)

Omit any posting you cannot judge rather than guessing at it."""


def _clean_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = [str(v).strip() for v in value if str(v).strip()]
    # `[]` is kept: "we compared and found nothing" is a real answer and a
    # different one from "we did not compare".
    return items


def _one(raw: Any) -> Annotation | None:
    if not isinstance(raw, dict):
        return None
    score = raw.get("match_score")
    if isinstance(score, bool) or not isinstance(score, int | float):
        score = None
    else:
        score = max(0, min(100, int(score)))
    why = raw.get("why_match")
    return Annotation(
        match_score=score,
        why_match=str(why).strip() if isinstance(why, str) and why.strip() else None,
        matched_skills=_clean_list(raw.get("matched_skills")),
        missing_skills=_clean_list(raw.get("missing_skills")),
        key_requirements=_clean_list(raw.get("key_requirements")),
    )


def score(
    jobs: list[NormalizedJob], profile: dict[str, Any], *, scorer: Scorer
) -> dict[int, Annotation]:
    """Annotations by job index. A missing index means NOT SCORED.

    Never raises. A posting without an annotation is still worth showing — it
    is real, current and matches the search — and the surface already has a
    state for a job whose skills were never compared.
    """
    if not jobs:
        return {}

    try:
        payload = scorer(SYSTEM, _prompt(jobs, profile), MAX_TOKENS)
    except Exception:
        # Every job stays unscored, and says so. V1 gave them all 70%.
        logger.exception("the scout ranking call failed; %d jobs stay unscored", len(jobs))
        return {}

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        logger.error("the scout returned no `items` array; %d jobs stay unscored", len(jobs))
        return {}

    out: dict[int, Annotation] = {}
    for raw in items:
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw["id"])
        except (KeyError, TypeError, ValueError):
            continue
        # An id outside the batch is the model inventing a posting to talk
        # about. Dropped rather than merged onto whatever is at that index.
        if not 0 <= index < len(jobs):
            logger.warning("the scout annotated id=%r, which is not in this batch", raw.get("id"))
            continue
        annotation = _one(raw)
        if annotation is not None:
            out[index] = annotation
    return out


def merge(job_row: dict[str, Any], annotation: Annotation | None) -> dict[str, Any]:
    """Fold an annotation into a row on its way to `jobs_feed`.

    Absent stays absent. The keys are simply not written when there is nothing
    to write, because `present_job` reads a missing `matched_skills` as "never
    compared" and an empty list as "compared, found nothing".
    """
    if annotation is None:
        return job_row

    row = dict(job_row)
    if annotation.match_score is not None:
        row["match_score"] = annotation.match_score
    if annotation.why_match is not None:
        row["why_match"] = annotation.why_match

    details = dict(row.get("details") or {})
    for key, value in (
        ("matched_skills", annotation.matched_skills),
        ("missing_skills", annotation.missing_skills),
        ("keyRequirements", annotation.key_requirements),
    ):
        if value is not None:
            details[key] = value
    row["details"] = details
    return row


__all__ = ["MAX_TOKENS", "SYSTEM", "Annotation", "Scorer", "merge", "score"]
