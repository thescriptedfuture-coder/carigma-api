"""Profile Analyst (internal codename `profile`).

PORTED VERBATIM from V1 `resumeiq/agents.py::run_profile_agent`, plus the shared
context builders it depends on. The prompt text is byte-for-byte the V1 prompt
except for the agent's self-description, which follows the locked V2 naming
(roadmap Part 0.2) — "The Glow-Up Guru" no longer exists as a user-facing name.

Everything here is pure: profile dict in, result dict out. No I/O beyond the
Claude call, no session state.
"""

from __future__ import annotations

from typing import Any

from carigma_api.services.ai import call_claude_json
from carigma_api.services.constants import VARIANCE_NOTE, score_band

# Which profile field each fix writes into. Applying a fix updates the STORED
# profile so a re-run of the Profile Analyst actually scores the improved
# content — that is what makes the score climb. A flag alone is not enough;
# this was V1 bug 1.1 ("score never improves").
SCORE_FIX_FIELDS: dict[str, str] = {
    "headline": "linkedinHeadline",
    "about": "aboutSection",
}


def apply_score_fix(profile: dict[str, Any], fix_key: str, rewrite: str) -> dict[str, Any]:
    """Write an analyst rewrite into the profile field it targets and return the
    updated profile. No-op for unmapped fixes (e.g. "keywords" is copy guidance,
    not an auto-applied field)."""
    field = SCORE_FIX_FIELDS.get(fix_key)
    if field and (rewrite or "").strip():
        profile[field] = rewrite.strip()
    return profile


def build_memory_block(profile: dict[str, Any]) -> str:
    """The AI-memory layer: a persistent preference context every agent prompt
    reads, so Carigma feels like ONE intelligence that knows the user rather
    than five disconnected tools. Returns "" when there's nothing to say."""
    prefs = profile.get("preferences") or {}
    lines: list[str] = []
    if profile.get("targetRole"):
        lines.append(f"PRIMARY TARGET ROLE: {profile['targetRole']}")
    if profile.get("location"):
        lines.append(f"PREFERRED LOCATION: {profile['location']}")
    if profile.get("tone"):
        lines.append(f"PREFERRED WRITING TONE: {profile['tone']}")

    not_for_me = prefs.get("not_for_me") or []
    if not_for_me:
        counts: dict[str, int] = {}
        for reason in not_for_me:
            counts[reason] = counts.get(reason, 0) + 1
        lines.append(
            "LEARNED DISLIKES (avoid matching/recommending these): "
            + ", ".join(f"{r} (×{n})" for r, n in counts.items())
        )

    for key, value in prefs.items():
        if key in ("not_for_me", "email_daily", "email_weekly"):
            continue
        if isinstance(value, str) and value.strip():
            lines.append(f"{key.replace('_', ' ').upper()}: {value}")

    if not lines:
        return ""
    return "USER MEMORY (learned preferences — honor these across every task):\n" + "\n".join(lines)


def build_campaign_context(profile: dict[str, Any]) -> str:
    """The user's goals, injected into agent prompts, folded together with the
    AI-memory layer so every agent reads the same understanding of the user."""
    lines: list[str] = []
    if profile.get("primaryGoal"):
        lines.append(f"PRIMARY GOAL: {profile['primaryGoal']}")
    if profile.get("personalBrand"):
        lines.append(f"PERSONAL BRAND ANGLE: {profile['personalBrand']}")
    if profile.get("targetSectors"):
        lines.append(f"TARGET SECTORS / COMPANIES: {profile['targetSectors']}")
    if profile.get("keyAchievement"):
        lines.append(f"KEY ACHIEVEMENT TO FEATURE PROMINENTLY: {profile['keyAchievement']}")
    if profile.get("contentAvoid"):
        lines.append(f"AVOID IN CONTENT / MESSAGING: {profile['contentAvoid']}")
    memory = build_memory_block(profile)
    if memory:
        lines.append(memory)
    return "\n".join(lines) if lines else ""


def run(profile: dict[str, Any], *, api_key: str) -> dict[str, Any]:
    """Audit and optimise a LinkedIn profile.

    Returns the raw analyst payload enriched with `band` and `variance_note`.
    Raises UpstreamError (from call_claude_json) on failure — never a fabricated
    score, which is why the caller can safely treat an exception as "charge
    nothing".
    """
    campaign = build_campaign_context(profile)
    system = (
        "You are the Profile Analyst — a LinkedIn profile expert who has reviewed thousands "
        "of profiles and knows what makes recruiters stop scrolling. Return ONLY a raw JSON object."
    )
    user = f"""Audit and optimise this LinkedIn profile. Be honest — do NOT inflate scores.

Name: {profile.get("name", "")}
Current Headline: {profile.get("linkedinHeadline", "")}
About: {profile.get("aboutSection", "")}
Experience: {profile.get("experience", "")}
Skills: {profile.get("skills", "")}
Certifications: {profile.get("certifications", "")}
Target Roles: {profile.get("targetRoles", "")}

CAMPAIGN BRIEF (frame all recommendations toward this positioning):
{campaign}

Return an object with ALL these exact keys:
- profileScore (integer 0–100 — assess honestly. Penalise: vague headlines, About with no metrics,
  missing sections, generic skills lists)
- scoreBreakdown (object with integer scores 0–100 for each: headline, about, experience, skills, activity)
- scoreSummary (2-sentence honest verdict on the profile's current state)
- optimizedHeadline (string under 220 chars — keyword-rich, role-specific, speaks to recruiters)
- optimizedAbout (string 400–500 words — hook opening, story arc, specific achievements with numbers,
  clear transition narrative, call to action. No generic phrases.)
- topKeywordsToAdd (array of 6 strings — high-value ATS keywords missing from the profile)
- featuredSectionIdea (string — one specific, creative idea for the LinkedIn Featured section)
- quickWins (array of 4 objects: action (string), impact ("high"/"medium"/"low"), effort ("low"/"medium"/"high"),
  timeToComplete (string like "10 mins" or "1 hour"))
- weeklyGoal (string — the single most impactful thing to do this week)
- recruitersWouldSay (string — one honest sentence a recruiter would say reading this profile right now)

Return ONLY the JSON object. Start with {{ end with }}."""

    result = call_claude_json(system, user, 4000, api_key=api_key)
    if not isinstance(result, dict):
        from carigma_api.services.ai import UpstreamError

        raise UpstreamError("Profile Analyst returned a non-object payload.")

    score = _coerce_score(result.get("profileScore"))
    result["profileScore"] = score
    result["band"] = score_band(score)
    result["variance_note"] = VARIANCE_NOTE
    return result


def _coerce_score(value: Any) -> int:
    """Clamp the model's score into 0–100.

    A score outside the range is a model slip, not user data — clamping is
    honest (we still report what it judged) where inventing a default would not
    be. A non-numeric value is a genuine failure and must not silently become 0,
    because 0 is itself a meaningful score.
    """
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        from carigma_api.services.ai import UpstreamError

        raise UpstreamError(f"Profile Analyst returned a non-numeric score: {value!r}") from exc
    return max(0, min(100, score))
