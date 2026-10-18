"""Content Intelligence: one slot, three drafts, nothing invented. Roadmap S7.

`POST /posts/week/{day}/regenerate` was fully built — credits, replay guard,
rate limit, slot write — with one line where the writer belonged:
`raise UpstreamError("Content Intelligence is not wired up yet.")`. This is
that line.

## What it may write from

The profile: the role, the experience, the skills and certifications, and the
lines the user wrote about themselves — their key achievement, their brand,
what they want to be known for. V1's rule, kept: *"No invented facts,
employers or numbers: ground every post in the profile provided."*

The model is told so, and the output is CHECKED, because a prompt is a request
and the drafts go out under a real person's name:

- **A template blank is dropped.** `[Company]`, `<your role>`, `XX%` — a
  draft with one would publish an unfilled hole.
- **A figure the profile does not contain is dropped.** A percentage, a rupee
  or crore amount, a "3x": the classic invented metric. If the number is not in
  what the user told us, it is not theirs to claim.

A draft that fails either check is not shown and not counted. When none
survive, the run is EMPTY, and an empty run is never charged.

## What it learns from

- **Angles already used.** The first lines of this week's other drafts, and of
  this slot's current drafts when redrafting — V1's `recentHooks`, so a redraft
  is a different post rather than the same one reworded.
- **Skips that retrain.** "Topic doesn't feel like me" and "Drafts weren't good
  enough" from this week and last, with the user's note when they left one.
  "Travelling" says nothing about the writing and is not passed in.

## Three drafts, three kinds of post

A story from their experience, a practice from their skills, and a view on
their field. The kind is the draft's `note`, so the chip above the paper says
what sort of post it is rather than "Draft 2".
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from carigma_api.services import grounding as rules
from carigma_api.services.posts import Slot, WeekPlan

logger = logging.getLogger(__name__)

#: `(system, user, max_tokens) -> parsed JSON`. The real one is `call_claude_json`.
Writer = Callable[[str, str, int], Any]

MAX_TOKENS = 3000

#: How many drafts a slot gets. Three is a choice, not a page of options.
VARIANTS = 3

#: The kinds of post, in the order they are asked for. Each becomes a note.
ANGLES = (
    ("Story", "a moment from their own experience, told plainly, and what it taught them"),
    ("Practice", "one concrete technique from their skills that a peer could use tomorrow"),
    ("View", "a clear opinion about their field, argued from their own work"),
)

#: Below this a draft is a caption, not a post; above it nobody reads to the end.
MIN_WORDS = 60
MAX_WORDS = 320

#: Profile fields the drafts may be grounded in, with a cap on each so one
#: enormous field cannot crowd out the rest of the prompt.
GROUNDING: tuple[tuple[str, str, int], ...] = (
    ("currentRole", "Current role", 300),
    ("targetRoles", "Moving towards", 300),
    ("industry", "Industry", 200),
    ("experience", "Experience (ground truth)", 3000),
    ("skills", "Skills", 600),
    ("certifications", "Certifications", 600),
    ("keyAchievement", "Their key achievement, in their words", 500),
    ("aboutSection", "Their About section", 1200),
)

SYSTEM = (
    "You are Carigma's Content Intelligence. You write LinkedIn posts that sound "
    "like the person, not like AI, and every claim in them comes from what the "
    "person told us about themselves.\n\n"
    "Hard rules:\n"
    "- Never invent an employer, a project, an event, a colleague, a result or a "
    "number. If the profile does not say it, the post does not say it.\n"
    "- No template blanks: never write [Company], <your role>, XX% or anything a "
    "person would have to fill in.\n"
    "- If the profile is thin, write about the craft — a lesson, a practice, a "
    "view — rather than making up a story to fill the space.\n"
    "- The first line is the hook: under 15 words, and it must make someone stop.\n"
    "- Short paragraphs with blank lines between them. No emoji runs. At most "
    "three hashtags, on the last line."
)


class NothingToWriteFrom(ValueError):
    """The profile has nothing a post could be grounded in.

    Not a model failure, and not something to charge for: asked to write about
    a person we know nothing about, the honest output is nothing, and the
    dishonest one is a stranger's career.
    """


@dataclass(frozen=True)
class Brief:
    """Everything the writer is given for one slot."""

    name: str
    day_label: str
    grounding: tuple[tuple[str, str], ...]
    tone: str
    niche: str
    brand: str
    avoid_topics: str
    #: First lines already used, so a redraft is a different post.
    used_hooks: tuple[str, ...] = ()
    #: What the user said when they declined drafts — the lesson, in their words.
    feedback: tuple[str, ...] = ()
    #: Every figure the drafts may cite, read from the grounding text.
    known_figures: frozenset[str] = field(default_factory=frozenset)


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str | int | float) else ""


def _first_line(body: str) -> str:
    return body.strip().split("\n", 1)[0].strip()[:160]


def brief_for(
    profile: dict[str, Any],
    slot: Slot,
    *,
    week: WeekPlan,
    previous: WeekPlan | None = None,
) -> Brief:
    """What to write from, for this slot. Raises `NothingToWriteFrom`."""
    grounding = tuple(
        (label, _text(profile.get(key))[:cap])
        for key, label, cap in GROUNDING
        if _text(profile.get(key))
    )
    # A role alone is a job title, not something to write from. The experience,
    # the skills or the user's own words about themselves have to be there.
    substance = {
        "Experience (ground truth)",
        "Skills",
        "Their key achievement, in their words",
        "Their About section",
    }
    if not any(label in substance for label, _ in grounding):
        raise NothingToWriteFrom("the profile has no experience, skills or about text")

    # Every draft this week, this slot's own included: a redraft must be a
    # different post, not the same one reworded.
    used = [_first_line(d.body) for other in week.slots for d in other.drafts if d.body.strip()]

    feedback: list[str] = []
    for plan in (week, previous):
        if plan is None:
            continue
        for skipped in plan.slots:
            reason = skipped.skip_reason
            if reason is None or not reason.retrains:
                continue
            line = f'Declined a {skipped.day} post: "{reason.label}"'
            if skipped.skip_note:
                line += f" — they said: {skipped.skip_note.strip()[:200]}"
            feedback.append(line)

    corpus = " ".join(text for _, text in grounding)
    return Brief(
        name=_text(profile.get("name")) or "the author",
        day_label=f"{slot.day} {slot.slot_date:%d %b}",
        grounding=grounding,
        tone=_text(profile.get("tone")) or "Professional yet warm",
        niche=_text(profile.get("niche")),
        brand=_text(profile.get("personalBrand")),
        avoid_topics=_text(profile.get("contentAvoid")),
        used_hooks=tuple(dict.fromkeys(h for h in used if h)),
        feedback=tuple(feedback),
        known_figures=frozenset(rules.figures(corpus)),
    )


def prompt(brief: Brief) -> tuple[str, str]:
    """(system, user) for one slot."""
    lines = [f"Write {VARIANTS} LinkedIn posts for {brief.name}, for {brief.day_label}."]
    lines.append("")
    lines.append("WHAT YOU MAY WRITE FROM — this is everything you know about them:")
    for label, text in brief.grounding:
        lines.append(f"{label}: {text}")
    lines.append("")
    lines.append(f"Voice: {brief.tone}")
    if brief.niche:
        lines.append(f"Known for / wants to be known for: {brief.niche}")
    if brief.brand:
        lines.append(f"Their brand line: {brief.brand}")
    if brief.avoid_topics:
        lines.append(f"Never write about: {brief.avoid_topics}")

    if brief.used_hooks:
        lines.append("")
        lines.append("ANGLES ALREADY USED — every post must be a different idea from these:")
        lines.extend(f"- {hook}" for hook in brief.used_hooks[:12])

    if brief.feedback:
        lines.append("")
        lines.append(
            "THEY DECLINED EARLIER DRAFTS. Learn from it: a topic complaint means a "
            "different subject; a quality complaint means plainer, more specific, more "
            "like them."
        )
        lines.extend(f"- {item}" for item in brief.feedback[:6])

    lines.append("")
    lines.append("One post of each kind, in this order:")
    for i, (label, guide) in enumerate(ANGLES, start=1):
        lines.append(f"{i}. {label} — {guide}")
    lines.append("")
    lines.append(
        'Return a JSON object: {"drafts": [{"angle": "Story", "body": "..."}, ...]} with '
        f"exactly {VARIANTS} items. Each body is the whole post, ready to copy, "
        f"{MIN_WORDS + 40}–{MAX_WORDS - 100} words, hook as its first line."
    )
    return SYSTEM, "\n".join(lines)


# ── Checking what came back ────────────────────────────────────────────────


@dataclass(frozen=True)
class Checked:
    """One draft that passed, or why one did not."""

    body: str
    note: str | None


def check(raw: Any, brief: Brief) -> tuple[list[Checked], list[str]]:
    """(drafts that may be shown, reasons the others were dropped).

    Read field by field. Anything that is not a non-empty string body is not a
    draft, and the model's own label is kept only when it is one of the kinds
    that were asked for.
    """
    items = raw.get("drafts") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return [], ["the reply had no drafts in it"]

    kept: list[Checked] = []
    dropped: list[str] = []
    seen: set[str] = set()
    labels = {label.lower(): label for label, _ in ANGLES}

    for item in items[:VARIANTS]:
        if not isinstance(item, dict):
            dropped.append("not a draft")
            continue
        body = item.get("body")
        if not isinstance(body, str) or not body.strip():
            dropped.append("empty")
            continue
        body = body.strip()
        words = len(body.split())
        if words < MIN_WORDS:
            dropped.append(f"too short ({words} words)")
            continue
        if words > MAX_WORDS:
            dropped.append(f"too long ({words} words)")
            continue
        if blank := rules.blank_in(body):
            dropped.append(f"left a blank to fill: {blank}")
            continue
        if made_up := rules.invented(body, brief.known_figures):
            dropped.append(f"cites figures the profile does not contain: {', '.join(made_up)}")
            continue
        key = re.sub(r"\W+", " ", body.lower()).strip()
        if key in seen:
            dropped.append("a repeat of another draft")
            continue
        seen.add(key)
        angle = item.get("angle")
        note = labels.get(angle.strip().lower()) if isinstance(angle, str) else None
        kept.append(Checked(body=body, note=note))

    return kept, dropped


def write(brief: Brief, *, writer: Writer) -> list[dict[str, Any]]:
    """Drafts for one slot, checked. `[]` when nothing survived.

    The writer's own failure propagates — `runs.execute` reports it and charges
    nothing. An answer with nothing usable in it returns `[]`, which the route
    turns into an empty run, which is also never charged.
    """
    system, user = prompt(brief)
    raw = writer(system, user, MAX_TOKENS)
    kept, dropped = check(raw, brief)
    if dropped:
        logger.warning("content writer dropped %d draft(s): %s", len(dropped), "; ".join(dropped))
    return [{"body": d.body, "note": d.note} for d in kept]


__all__ = [
    "ANGLES",
    "MAX_TOKENS",
    "VARIANTS",
    "Brief",
    "Checked",
    "NothingToWriteFrom",
    "Writer",
    "brief_for",
    "check",
    "prompt",
    "write",
]
