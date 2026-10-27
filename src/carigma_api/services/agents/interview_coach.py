"""Interview Intelligence (internal codename `interview`). Roadmap S14.

PORTED VERBATIM from V1 `resumeiq/agents.py::run_interview_agent`. The prompt
text is byte-for-byte V1's except the agent's self-description, which follows
the locked V2 naming (roadmap Part 0.2): "The Interview Ace" is not a name a
user sees any more. The kit's shape is V1's too, because `interview_preps`
already holds kits V1 wrote, and V2 opens them as they are.

## What V2 adds: a kit is checked before it is kept

V1 saved whatever came back. Here the payload is narrowed to the shape the page
renders (`kit_from`): a question without a model answer is dropped, a category
or difficulty outside V1's vocabulary is left out rather than shown, and fewer
than `MIN_QUESTIONS` usable questions is NOTHING USABLE — not kept, not
charged. A half-empty kit for 12 credits is the failure to avoid, and a kit is
the one thing someone reads the night before an interview.

The answers are rehearsal material drafted from the candidate's own profile,
and the payload says so (`DISCLAIMER`): V1's page carried the same caption.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from carigma_api.services.agents.profile_analyst import build_campaign_context

#: The model call, as a seam: `(system, user, max_tokens) -> payload`.
Writer = Callable[[str, str, int], object]

#: Fewer usable questions than this is not a kit. V1 asked for eight.
MIN_QUESTIONS = 4

CATEGORIES = ("behavioural", "technical", "situational", "role-specific")
DIFFICULTIES = ("easy", "medium", "hard")

#: Shipped in the payload, so no surface can render a kit without it. V1's
#: caption under every kit.
DISCLAIMER = (
    "These are prepared to help you rehearse. Adjust the details to your real experience "
    "and verify anything specific before your interview."
)


class NothingToPrepFrom(Exception):
    """The profile holds no experience, and model answers "grounded in the
    candidate's real background" need one. Refused before credits or the model."""


def require_background(profile: dict[str, Any]) -> None:
    if not str(profile.get("experience") or "").strip():
        raise NothingToPrepFrom


def prompt(profile: dict[str, Any], target_role: str) -> tuple[str, str]:
    """V1's system and user prompts, for `target_role`."""
    campaign = build_campaign_context(profile)
    role = str(
        target_role or profile.get("targetRoles") or profile.get("currentRole") or "the target role"
    ).strip()
    system = (
        "You are Interview Intelligence — a senior hiring manager and career coach who has run "
        "thousands of interviews. You prepare candidates with realistic questions, model "
        "answers grounded in THEIR background, and honest, specific coaching. "
        "Return ONLY a raw JSON object."
    )
    user = f"""Build an interview-prep kit for this candidate targeting: {role}

Candidate:
Name: {profile.get("name", "")}
Current role: {profile.get("currentRole", "")}
Experience: {profile.get("experience", "")}
Skills: {profile.get("skills", "")}
Education: {profile.get("education", "")}
Certifications: {profile.get("certifications", "")}

CAMPAIGN BRIEF (use to tailor positioning):
{campaign}

Return an object with ALL these exact keys:
- role (string — the target role you prepared for)
- overview (string — 2-3 sentence prep strategy specific to THIS candidate and role)
- questions (array of 8 objects mixing behavioural, technical/role-specific and situational:
    question (string),
    category ("behavioural" | "technical" | "situational" | "role-specific"),
    difficulty ("easy" | "medium" | "hard"),
    whyAsked (string — what the interviewer is really assessing),
    modelAnswer (string — a strong STAR-structured example answer in first person, grounded
      in the candidate's real background; 60-110 words),
    tips (array of 2 short strings))
- questionsToAskThem (array of 4 strings — sharp questions the candidate should ask the interviewer)
- redFlags (array of 3 strings — common mistakes THIS candidate should avoid)
- closingPitch (string — a 2-sentence "why hire me" summary tailored to the role)

Return ONLY the JSON object. Start with {{ end with }}."""
    return system, user


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _texts(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [t for t in (_text(v) for v in value) if t][:limit]


def _question(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    question, answer = _text(raw.get("question")), _text(raw.get("modelAnswer"))
    if not question or not answer:
        return None
    # V1 accepted the American spelling in its colours table; one vocabulary here.
    category = _text(raw.get("category")).lower().replace("behavioral", "behavioural")
    difficulty = _text(raw.get("difficulty")).lower()
    return {
        "question": question,
        "category": category if category in CATEGORIES else None,
        "difficulty": difficulty if difficulty in DIFFICULTIES else None,
        "whyAsked": _text(raw.get("whyAsked")) or None,
        "modelAnswer": answer,
        "tips": _texts(raw.get("tips"), 3),
    }


def kit_from(raw: object, *, role: str, minimum: int = MIN_QUESTIONS) -> dict[str, Any] | None:
    """The kit the page renders, or None when there is not one worth keeping.

    `minimum` is for a NEW kit. A stored one — V1 built two — is read with a
    lower bar, because refusing to show a kit someone paid for is worse than
    showing a short one.
    """
    if not isinstance(raw, dict):
        return None
    questions = [q for q in (_question(r) for r in raw.get("questions") or []) if q]
    if len(questions) < minimum:
        return None
    return {
        "role": _text(raw.get("role")) or role,
        "overview": _text(raw.get("overview")) or None,
        "questions": questions[:10],
        "questionsToAskThem": _texts(raw.get("questionsToAskThem"), 6),
        "redFlags": _texts(raw.get("redFlags"), 5),
        "closingPitch": _text(raw.get("closingPitch")) or None,
    }


def write(profile: dict[str, Any], target_role: str, *, writer: Writer) -> dict[str, Any] | None:
    """One kit for `target_role`, or None when nothing usable came back.

    Raises what the writer raises (an upstream failure): the caller's run is
    then failed, and a failed run is not charged.
    """
    system, user = prompt(profile, target_role)
    return kit_from(writer(system, user, 4500), role=target_role)
