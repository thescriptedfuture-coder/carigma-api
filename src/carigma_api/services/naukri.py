"""Naukri: a bounded profile tune-up, scored on its own model.

Roadmap 7.1. **Not a reuse of the LinkedIn scorer and not its twin.** LinkedIn
is an ongoing weekly practice; Naukri is a task you complete and then refresh.
The two never share an axis, and this module never produces a blended number.

## Seven weighted dimensions

    Key Skills coverage          25%   vs live target-role JDs
    Resume headline quality      20%   role + years + tools + specialisation
    Parse-ability                20%   what RChilli will actually extract
    Completeness & verification  12%
    Filter-field hygiene         10%   these GATE recruiter search
    Designation clarity           8%   creative titles -> searchable ones
    Summary & recency             5%

The weights are informed estimates, not Naukri's formula, and the payload says
so. Presenting an estimate as the platform's own arithmetic would be the same
lie as fabricating a job.

## A dimension we cannot score honestly scores NOTHING

`Dimension.unavailable()` produces a dimension with no score, which is excluded
from the total and from the denominator. The alternative — assuming zero, or
assuming full marks, or guessing a middle — moves a number the user will read
as measurement. `ProviderUnavailable != empty` applied to a score.

So the total is *"62 out of the 80% we could actually assess"*, never 62 out of
100 with a fifth of it invented.

## Honesty guardrails (7.1, hard)

- Every suggested skill must be confirmable as TRUE by the user. Suggestions
  are `candidate` until confirmed; nothing is written to a profile from here.
- **No keyword stuffing.** RChilli normalises repeats to zero gain and human
  recruiters reject stuffed profiles, so repetition is capped explicitly and
  the cap is enforced, not advised.
- Every fix explains WHY it helps, tied to Resdex mechanics. A fix asserted
  without a reason teaches nothing and cannot be judged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: Roadmap 7.1. Order is display order: heaviest first, because that is the
#: order in which effort pays.
WEIGHTS: dict[str, int] = {
    "key_skills": 25,
    "headline": 20,
    "parseability": 20,
    "completeness": 12,
    "filter_fields": 10,
    "designation": 8,
    "summary_recency": 5,
}

#: A true skill listed more than this many times is stuffing. RChilli
#: normalises repeats — the third mention buys nothing algorithmically and
#: costs credibility with the human who reads it.
MAX_SKILL_REPEATS = 2


class Confidence(StrEnum):
    #: Measured from data we hold.
    MEASURED = "measured"
    #: We could not assess this. Excluded from the score entirely.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Dimension:
    """One scored axis, or an honest gap."""

    key: str
    label: str
    weight: int
    #: 0..100 within this dimension, or None when unavailable.
    score: int | None
    confidence: Confidence
    #: Why this score, in the user's terms. Never absent for a measured one.
    receipt: str
    #: Why we could not assess it.
    unavailable_reason: str | None = None
    #: What the user can DO to make it scoreable. Required whenever the
    #: dimension is unavailable — see `Dimension.unavailable`.
    unlocked_by: str | None = None

    @classmethod
    def unavailable(cls, key: str, label: str, reason: str, *, unlocked_by: str) -> Dimension:
        """An honest gap — and the thing that would close it.

        `unlocked_by` is REQUIRED, not optional. An absence stated without a
        next action is a dead end; stated with one it becomes the co-pilot
        model working — "we can't measure this yet, and here is what would let
        us". The type refuses the dead-end version, the same way `measured`
        refuses a score with no receipt.
        """
        if not unlocked_by.strip():
            raise ValueError(
                f"{key} is unavailable with no way to unlock it — an absence "
                f"without a next action is a dead end, not an honest gap"
            )
        return cls(
            key=key,
            label=label,
            weight=WEIGHTS[key],
            score=None,
            confidence=Confidence.UNAVAILABLE,
            receipt="",
            unavailable_reason=reason,
            unlocked_by=unlocked_by,
        )

    @classmethod
    def measured(cls, key: str, label: str, score: int, receipt: str) -> Dimension:
        if not receipt:
            # Same rule as the weekly review's facts: the type refuses a claim
            # with no evidence behind it.
            raise ValueError(f"{key} scored without a receipt")
        return cls(
            key=key,
            label=label,
            weight=WEIGHTS[key],
            score=max(0, min(100, score)),
            confidence=Confidence.MEASURED,
            receipt=receipt,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "weight": self.weight,
            "score": self.score,
            "confidence": str(self.confidence),
            "receipt": self.receipt,
            "unavailable_reason": self.unavailable_reason,
            "unlocked_by": self.unlocked_by,
        }


@dataclass(frozen=True)
class Fix:
    """One suggested change, with the reason it helps.

    `candidate_skills` are proposals the user must confirm as TRUE. Nothing
    here is applied; the tune-up writes only what a human ticks.
    """

    key: str
    title: str
    #: Tied to Resdex mechanics. Explaining beats asserting — a user who
    #: understands why will keep doing it after we stop saying so.
    why_it_helps: str
    candidate_skills: tuple[str, ...] = ()
    requires_confirmation: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "why_it_helps": self.why_it_helps,
            "candidate_skills": list(self.candidate_skills),
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass
class NaukriScore:
    dimensions: list[Dimension] = field(default_factory=list)
    fixes: list[Fix] = field(default_factory=list)

    @property
    def assessable_weight(self) -> int:
        """The share of the model we could actually measure."""
        return sum(d.weight for d in self.dimensions if d.score is not None)

    @property
    def score(self) -> int | None:
        """Out of `assessable_weight`, NOT out of 100.

        None when nothing could be assessed — which is a different answer from
        zero, and must not be rendered as a bad score.
        """
        assessable = self.assessable_weight
        if assessable == 0:
            return None
        earned = sum(d.weight * (d.score or 0) for d in self.dimensions if d.score is not None)
        return round(earned / assessable)

    @property
    def unavailable(self) -> list[Dimension]:
        return [d for d in self.dimensions if d.score is None]

    def as_dict(self) -> dict[str, Any]:
        assessable = self.assessable_weight
        return {
            "score": self.score,
            # Stated so no surface can render "62" as "62/100" when a fifth of
            # the model could not be assessed.
            "assessed_weight": assessable,
            "coverage_note": (
                f"Scored on the {assessable}% of the model we could assess."
                if assessable < 100
                else "Every dimension assessed."
            ),
            "dimensions": [d.as_dict() for d in self.dimensions],
            "fixes": [f.as_dict() for f in self.fixes],
            # The weights are ours, not Naukri's. Saying so is the difference
            # between an estimate and a claim about someone else's algorithm.
            "model_note": (
                "Weightings are Carigma's informed estimates, not Naukri's formula. "
                "They are tuned against real dashboard outcomes."
            ),
        }


# ── The dimensions ─────────────────────────────────────────────────────────


def score_key_skills(
    profile_skills: tuple[str, ...], jd_skills: tuple[str, ...]
) -> tuple[Dimension, Fix | None]:
    """25%. Coverage of TRUE skills against live target-role JDs.

    Unavailable when we have no JD corpus for the target role — scoring
    coverage against nothing would produce a number with no referent.
    """
    if not jd_skills:
        return (
            Dimension.unavailable(
                "key_skills",
                "Key Skills coverage",
                "We haven't scanned enough live listings for your target role yet.",
                unlocked_by="One Career Scout run gives this something to measure against.",
            ),
            None,
        )

    have = {s.strip().lower() for s in profile_skills if s.strip()}
    want = [s for s in jd_skills if s.strip()]
    missing = [s for s in want if s.strip().lower() not in have]
    covered = len(want) - len(missing)
    pct = round(100 * covered / len(want)) if want else 0

    dimension = Dimension.measured(
        "key_skills",
        "Key Skills coverage",
        pct,
        f"{covered} of {len(want)} skills recruiters searched for are on your profile.",
    )
    if not missing:
        return dimension, None

    return dimension, Fix(
        key="key_skills",
        title=f"{len(missing)} skills appear in your target roles but not on your profile",
        why_it_helps=(
            "Recruiters filter on Key Skills before Resdex ranks anyone. A true skill "
            "you have not listed removes you from the result set entirely — it is not "
            "a ranking penalty, it is absence."
        ),
        # Candidates only. The user ticks the ones they actually have.
        candidate_skills=tuple(missing[:10]),
    )


def score_headline(headline: str) -> tuple[Dimension, Fix | None]:
    """20%. Role + years + tools + specialisation + availability."""
    if not headline.strip():
        return (
            Dimension.measured("headline", "Resume headline", 0, "No headline on the profile yet."),
            Fix(
                key="headline",
                title="Your resume headline is empty",
                why_it_helps=(
                    "The headline is the line a recruiter reads in the Resdex result "
                    "list before deciding whether to open your profile at all."
                ),
                requires_confirmation=False,
            ),
        )

    parts = {
        "a role": bool(
            re.search(r"\b(analyst|engineer|manager|lead|consultant|developer)\b", headline, re.I)
        ),
        "years of experience": bool(re.search(r"\b\d+\+?\s*(years?|yrs?)\b", headline, re.I)),
        "tools": bool(
            re.search(r"\b(sql|python|excel|power ?bi|tableau|dbt|sap|aws)\b", headline, re.I)
        ),
        "a specialisation": len(headline.split()) >= 6,
    }
    present = [name for name, ok in parts.items() if ok]
    missing = [name for name, ok in parts.items() if not ok]
    pct = round(100 * len(present) / len(parts))

    dimension = Dimension.measured(
        "headline",
        "Resume headline",
        pct,
        f"Carries {len(present)} of {len(parts)}: {', '.join(present) or 'none'}.",
    )
    if not missing:
        return dimension, None
    return dimension, Fix(
        key="headline",
        title=f"Your headline is missing {', '.join(missing)}",
        why_it_helps=(
            "Recruiters scan headlines in a list view. Years and tools are the two "
            "things they filter on mentally before clicking, so a headline without "
            "them gets skipped even when the profile behind it matches."
        ),
        requires_confirmation=False,
    )


#: Things RChilli demonstrably mis-parses. Each carries the consequence, not
#: just the label — "multi-column" means nothing to someone who does not know
#: what the parser does with it.
PARSE_HAZARDS: dict[str, str] = {
    "text_boxes": "text in boxes is skipped entirely by the parser",
    "header_footer_contact": "contact details in a header or footer are often not read",
    "multi_column": "multi-column layouts get interleaved into nonsense",
    "scanned": "a scanned page has no text layer at all",
    "nonstandard_headings": "unusual section headings are not recognised as sections",
    "inconsistent_dates": "mixed date formats break the experience timeline",
}


def score_parseability(hazards: tuple[str, ...] | None) -> tuple[Dimension, Fix | None]:
    """20%. What the parser will actually extract.

    `None` means no resume has been analysed — unavailable, not zero. A user
    who has not uploaded is not a user with an unparseable resume.
    """
    if hazards is None:
        return (
            Dimension.unavailable(
                "parseability",
                "Resume parse-ability",
                "No resume analysed yet.",
                unlocked_by="Upload your resume and we can check what the parser actually reads.",
            ),
            None,
        )

    found = [h for h in hazards if h in PARSE_HAZARDS]
    pct = max(0, 100 - (len(found) * 100 // max(1, len(PARSE_HAZARDS))))
    dimension = Dimension.measured(
        "parseability",
        "Resume parse-ability",
        pct,
        "Parses cleanly." if not found else f"{len(found)} issues the parser will trip on.",
    )
    if not found:
        return dimension, None
    return dimension, Fix(
        key="parseability",
        title=f"{len(found)} things stop your resume parsing cleanly",
        why_it_helps=(
            "Naukri runs your file through a parser before a recruiter ever sees it. "
            "Anything it cannot read is not merely ranked lower — it is not in the "
            "record at all. Specifically: " + "; ".join(PARSE_HAZARDS[h] for h in found) + "."
        ),
        requires_confirmation=False,
    )


#: Fields that GATE recruiter search — they filter before ranking, so a blank
#: one removes the profile from the result set rather than lowering it.
FILTER_FIELDS = ("location", "total_experience", "current_ctc", "notice_period", "education")


def score_filter_fields(profile: dict[str, Any]) -> tuple[Dimension, Fix | None]:
    """10%. Blank gates are exclusions, not deductions."""
    blank = [f for f in FILTER_FIELDS if not str(profile.get(f) or "").strip()]
    pct = round(100 * (len(FILTER_FIELDS) - len(blank)) / len(FILTER_FIELDS))
    dimension = Dimension.measured(
        "filter_fields",
        "Filter fields",
        pct,
        f"{len(FILTER_FIELDS) - len(blank)} of {len(FILTER_FIELDS)} search filters filled.",
    )
    if not blank:
        return dimension, None
    return dimension, Fix(
        key="filter_fields",
        title=f"{len(blank)} search filters are blank: {', '.join(blank)}",
        why_it_helps=(
            "These are applied BEFORE ranking. A recruiter filtering for 'Bengaluru, "
            "3-6 years' never sees a profile with those fields empty, however well it "
            "would otherwise match."
        ),
        # CTC and notice period are facts only the user knows.
        requires_confirmation=True,
    )


def cap_repeats(skills: tuple[str, ...]) -> tuple[str, ...]:
    """Enforce the no-stuffing rule rather than advising it.

    RChilli normalises repeats, so a third mention buys nothing algorithmically
    and costs credibility with the human who reads it. The cap is applied here
    so no caller can opt out of it.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for skill in skills:
        key = skill.strip().lower()
        if not key:
            continue
        seen[key] = seen.get(key, 0) + 1
        if seen[key] <= MAX_SKILL_REPEATS:
            out.append(skill)
    return tuple(out)


__all__ = [
    "FILTER_FIELDS",
    "MAX_SKILL_REPEATS",
    "PARSE_HAZARDS",
    "WEIGHTS",
    "Confidence",
    "Dimension",
    "Fix",
    "NaukriScore",
    "cap_repeats",
    "score_filter_fields",
    "score_headline",
    "score_key_skills",
    "score_parseability",
]
