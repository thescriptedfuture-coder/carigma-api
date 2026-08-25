"""Naukri: a bounded profile tune-up, scored on its own model.

Roadmap 7.1. **Not a reuse of the LinkedIn scorer and not its twin.** LinkedIn
is an ongoing weekly practice; Naukri is a task you complete and then refresh.
The two never share an axis, and this module never produces a blended number.

## The model is what we compute, not what we designed

Roadmap 7.1 designs seven weighted dimensions. **Two are built.** The other
five are declared in `NOT_YET_MODELLED` and are deliberately NOT part of the
score, the weights or the denominator:

    BUILT           Resume headline              20
                    Filter-field hygiene         10   these GATE recruiter search

    NOT BUILT       Key Skills coverage               needs a live JD corpus
                    Parse-ability                     needs stored resume hazards
                    Completeness & verification       no scorer
                    Designation clarity               no scorer
                    Summary & recency                 no scorer

They were previously scored as `unavailable`, which put 45 points into a
denominator nobody could ever earn and made `COMPLETE` unreachable by
construction. A model advertising points it cannot award is the same defect as
a button that goes nowhere, one level up: it reads as a ceiling the user is
failing to reach, when in fact we simply do not measure it yet.

So the denominator is `TOTAL_WEIGHT` — the sum of what we actually compute —
and the unbuilt five are named without a score, a weight, or an action.

## "We don't assess this" and "we couldn't assess this for you" are different

They are different `Confidence` values, because they are different statements
and a user reading one as the other draws the wrong conclusion about their own
profile:

    UNAVAILABLE   we could not assess YOUR profile — carries `unlocked_by`,
                  and the thing that unlocks it is something the user can do
    NOT_BUILT     we do not assess this dimension at all yet — carries no
                  action, because there is nothing the user could do

`Dimension.unavailable` requires `unlocked_by`; `Dimension.not_built` refuses
it. The types make the two impossible to confuse.

## A dimension we cannot score honestly scores NOTHING

`Dimension.unavailable()` produces a dimension with no score, excluded from the
total and from the denominator. The alternative — assuming zero, or full
marks, or a middle — moves a number the user will read as measurement.

## Honesty guardrails (7.1, hard)

- Every fix explains WHY it helps, tied to Resdex mechanics. A fix asserted
  without a reason teaches nothing and cannot be judged.
- Nothing is written to a profile from here. `Fix` describes; it never applies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: What the lens actually assesses. Display order: heaviest first, because that
#: is the order in which effort pays.
#:
#: The numbers are roadmap 7.1's original weights, kept rather than
#: renormalised to 100. Rescaling them would invent precision — it would say
#: the headline is 67% of the Naukri model, which is a claim about Naukri we
#: have no basis for. They are points out of `TOTAL_WEIGHT`, and the payload
#: says so.
WEIGHTS: dict[str, int] = {
    "headline": 20,
    "filter_fields": 10,
}

#: The denominator. `sum(WEIGHTS.values())`, never 100 — see the module note.
TOTAL_WEIGHT = sum(WEIGHTS.values())

#: Designed in 7.1, not built. Named so the lens can say what it does not do,
#: without pretending these are scores the user is missing out on.
#:
#: The value is what each would NEED, not what the user should do — that
#: distinction is the whole point of the `NOT_BUILT` state.
NOT_YET_MODELLED: dict[str, tuple[str, str]] = {
    "key_skills": (
        "Key Skills coverage",
        "a corpus of live listings for your target role to measure coverage against",
    ),
    "parseability": (
        "Resume parse-ability",
        "your resume's parse hazards stored at upload time",
    ),
    "completeness": ("Completeness & verification", "a scorer"),
    "designation": ("Designation clarity", "a scorer"),
    "summary_recency": ("Summary & recency", "a scorer"),
}

#: Roadmap 7.1. Charged ONCE PER CYCLE, not per run and not per step.
#:
#: The cycle is a bounded task and the design explicitly promises nothing nags
#: between its steps. Per-step billing would make the user weigh a charge at
#: every step of something they have already committed to — which produces
#: mid-cycle abandonment, the exact failure the bounded grammar exists to
#: prevent. You pay to start the tune-up; finishing it costs nothing more.
TUNEUP_CREDITS = 10

#: One constant, because it is a claim about how OUR model works. A copy of it
#: frozen inside a stored payload would keep asserting provenance for weights
#: that have since changed, so surfaces read this rather than the blob.
MODEL_NOTE = (
    "Weightings are Carigma's informed estimates, not Naukri's formula. "
    "They are tuned against real dashboard outcomes."
)


def scope_note() -> str:
    """What this tune-up covers, said before the user reads a number.

    Derived from the two dicts rather than written out, so it cannot drift from
    what the code does — which is precisely how "the seven dimensions" survived
    in the copy while four of them had no scorer.
    """
    built = len(WEIGHTS)
    total = built + len(NOT_YET_MODELLED)
    return (
        f"This tune-up assesses {built} of the {total} dimensions in the Naukri model. "
        f"The other {total - built} are designed but not built yet — they are listed "
        f"below rather than scored, so nothing here counts against you for a "
        f"dimension we do not measure."
    )


@dataclass(frozen=True)
class UnlockAction:
    """Somewhere to go, from where the user is standing.

    `unlocked_by` says what would make a dimension scoreable; this makes it
    reachable. A sentence describing something elsewhere is a signpost with no
    road — the user has to work out where "run Career Scout" happens.

    Only ever attached to an UNAVAILABLE dimension, never a NOT_BUILT one: an
    action the product cannot honour is a lie with a click target.
    """

    label: str
    route: str

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "route": self.route}


class NaukriState(StrEnum):
    """What the lens is actually showing.

    Carried explicitly so no surface has to infer it from `score is None`,
    which is the kind of derivation that ends up spelled differently in two
    places.
    """

    #: Never run. The state EVERY existing user is in at cutover, so it is the
    #: primary path rather than an edge case.
    NEVER_RUN = "never_run"
    #: Some modelled dimensions measured, some not.
    PARTIAL = "partial"
    #: Everything we model was assessed. Reachable, unlike before.
    COMPLETE = "complete"


class CycleState(StrEnum):
    """Where the bounded tune-up is. Stored on the row, not derived.

    Naukri is a task you complete and then refresh — the completion state is
    the point of the grammar, and it is a fact about what the user did rather
    than something recomputed from a score.
    """

    #: No row exists. This value never appears IN a row — see `should_persist`.
    NEVER_RUN = "never_run"
    #: A cycle is open. Re-scoring within it is free.
    NEEDS_TUNEUP = "needs_tuneup"
    #: Done, until the freshness meter decays.
    OPTIMIZED = "optimized"


class Confidence(StrEnum):
    #: Measured from data we hold.
    MEASURED = "measured"
    #: We could not assess YOUR profile. Carries a way out.
    UNAVAILABLE = "unavailable"
    #: We do not assess this dimension at all yet. Carries no action, because
    #: there is nothing the user could do about it.
    NOT_BUILT = "not_built"


@dataclass(frozen=True)
class Dimension:
    """One scored axis, an honest gap, or something we do not do yet."""

    key: str
    label: str
    weight: int
    #: 0..100 within this dimension, or None when not scored.
    score: int | None
    confidence: Confidence
    #: Why this score, in the user's terms. Never absent for a measured one.
    receipt: str
    #: Why we could not assess it.
    unavailable_reason: str | None = None
    #: What the user can DO to make it scoreable. Required whenever the
    #: dimension is UNAVAILABLE — see `Dimension.unavailable`. Never set on a
    #: NOT_BUILT one.
    unlocked_by: str | None = None
    #: Where to do it.
    unlock_action: UnlockAction | None = None
    #: What WE would need to build. Only ever set on a NOT_BUILT dimension —
    #: it is a statement about our roadmap, not an instruction to the user.
    needs: str | None = None

    @classmethod
    def unavailable(
        cls,
        key: str,
        label: str,
        reason: str,
        *,
        unlocked_by: str,
        action: UnlockAction | None = None,
    ) -> Dimension:
        """An honest gap in THIS user's data — and the thing that would close it.

        `unlocked_by` is REQUIRED, not optional. An absence stated without a
        next action is a dead end; stated with one it becomes the co-pilot
        model working. The type refuses the dead-end version, the same way
        `measured` refuses a score with no receipt.

        Use `not_built` instead when the gap is OURS. Telling a user to upload
        a resume so we can score parse-ability, when nothing reads the upload,
        is a dead end wearing the costume of a next action.
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
            unlock_action=action,
        )

    @classmethod
    def not_built(cls, key: str) -> Dimension:
        """A dimension we do not assess yet.

        Weight ZERO, always: it is not in `WEIGHTS`, not in the denominator,
        and cannot drag a percentage down. And no action of any kind, because
        the thing standing in the way is our roadmap, not the user's profile.
        """
        label, needs = NOT_YET_MODELLED[key]
        return cls(
            key=key,
            label=label,
            weight=0,
            score=None,
            confidence=Confidence.NOT_BUILT,
            receipt="",
            needs=needs,
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
            "unlock_action": self.unlock_action.as_dict() if self.unlock_action else None,
            "needs": self.needs,
        }


@dataclass(frozen=True)
class Fix:
    """One suggested change, with the reason it helps.

    Nothing here is applied. The tune-up describes what to change and why; the
    change itself happens where that field is edited.
    """

    key: str
    title: str
    #: Tied to Resdex mechanics. Explaining beats asserting — a user who
    #: understands why will keep doing it after we stop saying so.
    why_it_helps: str
    #: Where the user would make this change.
    action: UnlockAction | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "why_it_helps": self.why_it_helps,
            "action": self.action.as_dict() if self.action else None,
        }


@dataclass
class NaukriScore:
    dimensions: list[Dimension] = field(default_factory=list)
    fixes: list[Fix] = field(default_factory=list)

    @property
    def assessable_weight(self) -> int:
        """The share of the model we could actually measure.

        NOT_BUILT dimensions carry weight 0, so they cannot appear here even by
        accident.
        """
        return sum(d.weight for d in self.dimensions if d.score is not None)

    @property
    def score(self) -> int | None:
        """Out of `assessable_weight`, NOT out of 100.

        None when nothing could be assessed — a different answer from zero, and
        it must not be rendered as a bad score.
        """
        assessable = self.assessable_weight
        if assessable == 0:
            return None
        earned = sum(
            d.weight * (d.score or 0) for d in self.dimensions if d.score is not None
        )  # falsy-ok: guarded by `if d.score is not None` in the same comprehension
        return round(earned / assessable)

    @property
    def unavailable(self) -> list[Dimension]:
        return [d for d in self.dimensions if d.confidence is Confidence.UNAVAILABLE]

    @property
    def not_built(self) -> list[Dimension]:
        return [d for d in self.dimensions if d.confidence is Confidence.NOT_BUILT]

    @property
    def state(self) -> NaukriState:
        assessable = self.assessable_weight
        if assessable == 0:
            return NaukriState.NEVER_RUN
        if assessable < TOTAL_WEIGHT:
            return NaukriState.PARTIAL
        return NaukriState.COMPLETE

    def as_dict(self) -> dict[str, Any]:
        assessable = self.assessable_weight
        return {
            "score": self.score,
            # Points, and the denominator they are out of. Stated so no surface
            # can render "62" as "62/100" when the model is not 100 points and
            # part of it may not have been assessed.
            "assessed_weight": assessable,
            "total_weight": TOTAL_WEIGHT,
            "state": str(self.state),
            # State-aware, because "Scored on the 0% of the model we could
            # assess" is accurate and reads like a bug. A never-run lens has
            # not scored badly on nothing — it has not started, and the honest
            # sentence for that is a different sentence, not a degenerate case
            # of the partial one.
            "coverage_note": _coverage_note(self.state, assessable),
            # What the tune-up covers at all, said up front rather than left to
            # be inferred from a list of cards.
            "scope_note": scope_note(),
            "dimensions": [d.as_dict() for d in self.dimensions],
            "fixes": [f.as_dict() for f in self.fixes],
            "model_note": MODEL_NOTE,
        }


def _coverage_note(state: NaukriState, assessable: int) -> str:
    if state is NaukriState.NEVER_RUN:
        # No number at all. A percentage here invites the reader to treat zero
        # as a result rather than as an absence of one.
        return "Not scored yet — the tune-up hasn't run."
    if state is NaukriState.PARTIAL:
        pct = round(100 * assessable / TOTAL_WEIGHT)
        return f"Scored on the {pct}% of what we assess that we could read."
    return "Everything this tune-up assesses was assessed."


def starts_new_cycle(latest: CycleState | None) -> bool:
    """Whether this run BEGINS a cycle, and therefore whether it charges.

    Charged when there is no open cycle: no row at all, or the last one closed
    as `optimized`. A run while `needs_tuneup` is mid-cycle work the user has
    already paid for, and charging again would bill them twice for one task.
    """
    return latest is None or latest is CycleState.OPTIMIZED


def should_persist(score: NaukriScore) -> bool:
    """A never-run score must NOT be written.

    Scoring nothing and persisting it would put a null score in
    `naukri_scores` that every later trend line has to special-case forever —
    and it would claim, in the record, that we assessed a profile we did not.

    **The absence of a row IS the never-run state.** One representation, so
    nothing can disagree with it.
    """
    return score.state is not NaukriState.NEVER_RUN


# ── The dimensions we actually assess ──────────────────────────────────────

EDIT_PROFILE = UnlockAction(label="Edit your profile", route="/settings")


def score_headline(headline: str) -> tuple[Dimension, Fix | None]:
    """20 points. Role + years + tools + specialisation."""
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
                action=EDIT_PROFILE,
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
        action=EDIT_PROFILE,
    )


#: The recruiter-search filters we can actually see.
#:
#: `total_experience`, `current_ctc` and `notice_period` were here and are not
#: any more. They are not columns on `profiles`, there is no input for them
#: anywhere, and no run could ever fill them — so the dimension was capped for
#: every user forever and its fix named three fields nobody could supply.
#: They come back when they have somewhere to be typed.
FILTER_FIELDS = ("location", "education")


def score_filter_fields(profile: dict[str, Any]) -> tuple[Dimension, Fix | None]:
    """10 points. Blank gates are exclusions, not deductions."""
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
        action=EDIT_PROFILE,
    )


__all__ = [
    "FILTER_FIELDS",
    "MODEL_NOTE",
    "NOT_YET_MODELLED",
    "TOTAL_WEIGHT",
    "TUNEUP_CREDITS",
    "WEIGHTS",
    "Confidence",
    "CycleState",
    "Dimension",
    "Fix",
    "NaukriScore",
    "NaukriState",
    "UnlockAction",
    "score_filter_fields",
    "score_headline",
    "scope_note",
    "should_persist",
    "starts_new_cycle",
]
