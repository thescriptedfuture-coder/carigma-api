"""Incremental profile changes: detect → propose → the user approves.

The gap this fills: there is a *re-upload* path, but it replaces the whole
document. There is no path for "I earned a certification — add it properly".

## Why the proposal exists at all

Never a bare "regenerate" button the user has to find. The system notices the
change and offers the consequences; the user says yes. That is the same
co-pilot model as the weekly contract, and for the same reason — a rewrite the
user did not ask for is a rewrite they cannot trust.

## Scale the response to the size of the event

This is the detail that makes it feel intelligent rather than mechanical:

    skill / certification   a quiet update — headline tweak, Naukri key skills
    project                 update, and OFFER a post (genuinely newsworthy)
    role / company change   MAJOR — invalidates the headline, the positioning,
                            possibly the target roles and the whole content
                            plan. A proper re-decode, not a tune.

A job change treated like a skill addition would feel broken. So would a
certification triggering a full re-onboarding.

## Credits

Detection and the proposal are **free**. Charging to notice a change would
suppress exactly the behaviour we want. The regeneration that follows approval
is a normal agent run at its usual cost, and is charged by `services.runs` like
any other — this module never charges anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    SKILL = "skill"
    CERTIFICATION = "certification"
    PROJECT = "project"
    ROLE = "role"
    EDUCATION = "education"


class Operation(StrEnum):
    ADD = "add"
    REMOVE = "remove"


class Magnitude(StrEnum):
    """How much of the career story this change invalidates."""

    QUIET = "quiet"
    NOTABLE = "notable"
    MAJOR = "major"


class State(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    DECLINED = "declined"
    APPLIED = "applied"


#: Which agents a magnitude re-runs. Named agents, not "everything", so the
#: cost of an approval is predictable and can be shown BEFORE the user agrees.
AGENTS_FOR: dict[Magnitude, tuple[str, ...]] = {
    Magnitude.QUIET: ("profile",),
    Magnitude.NOTABLE: ("profile",),
    Magnitude.MAJOR: ("profile", "content", "jobs"),
}

#: The event → magnitude table from the spec, as data rather than branches so
#: it can be asserted directly in a test.
_MAGNITUDE: dict[Kind, Magnitude] = {
    Kind.SKILL: Magnitude.QUIET,
    Kind.CERTIFICATION: Magnitude.QUIET,
    Kind.EDUCATION: Magnitude.QUIET,
    Kind.PROJECT: Magnitude.NOTABLE,
    Kind.ROLE: Magnitude.MAJOR,
}


def magnitude_of(kind: Kind, operation: Operation = Operation.ADD) -> Magnitude:
    """How big an event this is.

    Removals are handled as carefully as additions and carry the SAME
    magnitude: removing the role you were positioned around invalidates just as
    much as adding one. Treating a removal as minor is how a stale headline
    survives a career change.
    """
    return _MAGNITUDE[kind]


@dataclass(frozen=True)
class Consequence:
    """One thing that would change, in the user's words rather than ours."""

    target: str
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return {"target": self.target, "summary": self.summary}


@dataclass(frozen=True)
class Proposal:
    """What we are offering to do, and what it costs.

    `projected` is the honesty rule from the decode's beat 2: any score
    movement arising from an edit is a PROJECTION until the agent has actually
    re-scored. Presenting a projection as a measurement is the same lie as
    fabricating a job.
    """

    kind: Kind
    operation: Operation
    magnitude: Magnitude
    headline: str
    consequences: tuple[Consequence, ...]
    agents: tuple[str, ...]
    offers_post: bool = False
    projected_note: str | None = None
    #: Free. Stated explicitly because the user is about to be asked to agree
    #: to something, and "will this cost me?" is the first question.
    detection_cost_credits: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "operation": str(self.operation),
            "magnitude": str(self.magnitude),
            "headline": self.headline,
            "consequences": [c.as_dict() for c in self.consequences],
            "agents": list(self.agents),
            "offers_post": self.offers_post,
            "projected_note": self.projected_note,
            "detection_cost_credits": self.detection_cost_credits,
        }


def _label(payload: dict[str, Any], kind: Kind) -> str:
    """What the user called this thing. Never invented.

    An empty label yields a generic noun rather than a guess — inventing
    "Senior Analyst" because the field was blank would be fabrication, and the
    honesty rule here is the same one that governs job listings.
    """
    for key in ("name", "title", "label"):
        value = (payload.get(key) or "").strip()
        if value:
            return value
    return str(kind).replace("_", " ")


def build_proposal(
    kind: Kind,
    payload: dict[str, Any],
    operation: Operation = Operation.ADD,
) -> Proposal:
    """The offer, scaled to the event. Never applies anything."""
    magnitude = magnitude_of(kind, operation)
    what = _label(payload, kind)
    verb = "added" if operation is Operation.ADD else "removed"

    if magnitude is Magnitude.MAJOR:
        headline = (
            f"You {verb} {what}. That changes the story, not just a line of it."
            if operation is Operation.ADD
            else f"You {verb} {what}. Your positioning still refers to it."
        )
        consequences = (
            Consequence("headline", "Your LinkedIn headline still describes the old role."),
            Consequence(
                "positioning", "Your positioning and About section were written around it."
            ),
            Consequence("targets", "Your target roles may no longer be the right ones."),
            Consequence("content", "This week's content plan was built for the old story."),
        )
        return Proposal(
            kind=kind,
            operation=operation,
            magnitude=magnitude,
            headline=headline,
            consequences=consequences,
            agents=AGENTS_FOR[magnitude],
            projected_note=(
                "Any score change shown before the re-decode is projected, not measured."
            ),
        )

    if magnitude is Magnitude.NOTABLE:
        return Proposal(
            kind=kind,
            operation=operation,
            magnitude=magnitude,
            headline=f"You {verb} {what}.",
            consequences=(
                Consequence("profile", "Your profile picks it up."),
                # A project is genuinely newsworthy, which is the whole reason
                # a post is OFFERED here and nowhere else. Offered, never
                # queued: Carigma drafts, you publish.
                Consequence("content", "It is worth a post — we can draft one."),
            ),
            agents=AGENTS_FOR[magnitude],
            offers_post=True,
            projected_note="Any score change shown before the re-run is projected, not measured.",
        )

    return Proposal(
        kind=kind,
        operation=operation,
        magnitude=magnitude,
        headline=f"You {verb} {what}.",
        consequences=(
            Consequence("headline", "We can work it into your LinkedIn headline."),
            Consequence("naukri", "And refresh your Naukri key skills."),
        ),
        agents=AGENTS_FOR[magnitude],
        projected_note="Any score change shown before the re-run is projected, not measured.",
    )


class AlreadyDecided(ValueError):
    """A proposal can be decided once.

    Re-deciding is refused rather than overwritten, exactly as the weekly
    contract refuses it: the second answer would silently replace a record of
    what the user actually agreed to.
    """


@dataclass
class ChangeRecord:
    """A stored row, as the service sees it."""

    id: int
    user_id: str
    kind: Kind
    operation: Operation
    payload: dict[str, Any]
    magnitude: Magnitude
    proposal: dict[str, Any]
    state: State = State.PROPOSED
    decided_at: str | None = None
    applied_at: str | None = None

    @property
    def user_actually_agreed(self) -> bool:
        """True ONLY for a real yes.

        Deliberately not `state != 'declined'`. There is no auto-adopt in this
        flow and there must never be one, so anything that is not an explicit
        approval is not agreement.
        """
        return self.state in (State.APPROVED, State.APPLIED) and self.decided_at is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": str(self.kind),
            "operation": str(self.operation),
            "magnitude": str(self.magnitude),
            "state": str(self.state),
            "user_approved": self.user_actually_agreed,
            "proposal": self.proposal,
            "decided_at": self.decided_at,
            "applied_at": self.applied_at,
        }


def decide(record: ChangeRecord, *, approved: bool, now_iso: str) -> ChangeRecord:
    """Record the user's answer. Once."""
    if record.state is not State.PROPOSED:
        raise AlreadyDecided(
            f"This change was already {record.state}. Add a new one rather than re-deciding."
        )
    record.state = State.APPROVED if approved else State.DECLINED
    record.decided_at = now_iso
    return record


#: Everything a caller may add. Rejecting unknown keys keeps free text out of
#: fields every agent downstream will read as fact.
ALLOWED_FIELDS: dict[Kind, frozenset[str]] = {
    Kind.SKILL: frozenset({"name", "level"}),
    Kind.CERTIFICATION: frozenset({"name", "issuer", "issued_on", "credential_id"}),
    Kind.PROJECT: frozenset({"title", "summary", "link"}),
    Kind.ROLE: frozenset({"title", "company", "started_on", "summary"}),
    Kind.EDUCATION: frozenset({"name", "institution", "completed_on"}),
}


class UnknownField(ValueError):
    """A field we do not model. Refused rather than stored.

    Storing it would put unvalidated free text into the context every agent
    reads as fact — the shortest path to Carigma stating something the user
    never said.
    """


def clean_payload(kind: Kind, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the modelled fields, refuse the rest, invent nothing."""
    allowed = ALLOWED_FIELDS[kind]
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise UnknownField(f"{kind} does not take {', '.join(unknown)}")
    return {k: v for k, v in payload.items() if v not in (None, "")}


__all__ = [
    "AGENTS_FOR",
    "ALLOWED_FIELDS",
    "AlreadyDecided",
    "ChangeRecord",
    "Consequence",
    "Kind",
    "Magnitude",
    "Operation",
    "Proposal",
    "State",
    "UnknownField",
    "build_proposal",
    "clean_payload",
    "decide",
    "magnitude_of",
]
