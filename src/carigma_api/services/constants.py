"""Shared constants — ported from V1 `resumeiq/config.py`.

Values are carried over unchanged unless the P2 brief set a new one; those are
marked NEW and cite the decision.
"""

from __future__ import annotations

from typing import Final

MODEL: Final = "claude-sonnet-4-6"

# ── Agents ─────────────────────────────────────────────────────────────────
# V2 canonical user-facing names (roadmap Part 0.2, D8 — LOCKED). Internal
# codenames are unchanged from V1 so no backend churn.
AGENT_LABELS: Final[dict[str, str]] = {
    "content": "Content Intelligence",
    "jobs": "Career Scout",
    "profile": "Profile Analyst",
    "interview": "Interview Intelligence",
    "cv": "CV Architect",
    "naukri": "Profile Analyst",  # the Naukri track shares the analyst identity
}

VALID_AGENTS: Final = tuple(AGENT_LABELS)

# ── Credits ────────────────────────────────────────────────────────────────
SIGNUP_CREDITS: Final = 100

CREDIT_COSTS: Final[dict[str, int]] = {
    "run_content": 12,
    "run_jobs": 15,
    "run_profile": 10,
    "run_interview": 12,
    "run_all": 30,
    "copy_caption": 2,
    "copy_image_prompt": 2,
    "custom_post": 5,
    "cv_generate": 15,
    "cv_photo": 5,
    # NEW in V2 (P2 brief §21 Q3): same class of work as Profile Analyst, so
    # identical pricing — and charged ONCE for the whole tune-up, never
    # per-step, because the cycle promises "nothing nags between steps".
    "run_naukri": 10,
    # NEW in V2 (P2 brief §21 Q4): one slot is a fraction of a week's plan.
    # Priced low on purpose — the skip-and-regenerate loop is how Content
    # Intelligence learns voice, and pricing it high suppresses the signal.
    "regenerate_slot": 3,
}

CREDIT_LABELS: Final[dict[str, str]] = {
    "run_content": "Weekly content plan",
    "run_jobs": "Career Scout run",
    "run_profile": "Profile Analyst run",
    "run_interview": "Interview Intelligence run",
    "run_all": "Run all 3 agents",
    "copy_caption": "Copy post caption",
    "copy_image_prompt": "Copy AI image prompt",
    "custom_post": "Custom post",
    "cv_generate": "Tailored CV",
    "cv_photo": "CV headshot add-on",
    "run_naukri": "Naukri tune-up",
    "regenerate_slot": "Regenerate one post",
}

# Actions that are deliberately FREE. Listed explicitly so "is this free?" is a
# lookup rather than a judgement call scattered across routes.
#
# skip_slot: the user is handing us training signal. Charging for it would be
# charging someone to help us improve the product (P2 brief §21 Q4).
# onboarding_score: the first score is the product's promise; charging for it
# would gate the one thing that proves the value.
FREE_ACTIONS: Final = frozenset({"skip_slot", "onboarding_score", "score_fix"})

# ── Score bands ────────────────────────────────────────────────────────────
SCORE_BANDS: Final = (
    (0, 39, "buried"),
    (40, 59, "underselling"),
    (60, 79, "solid"),
    (80, 100, "strong"),
)


def score_band(score: int) -> str:
    for low, high, name in SCORE_BANDS:
        if low <= score <= high:
            return name
    return "buried" if score < 0 else "strong"


# The honesty note that ships INSIDE the score payload. Claude reasons
# contextually rather than applying a fixed formula, so run-to-run variance is
# real; saying so is the guardrail against implying false precision.
VARIANCE_NOTE: Final = (
    "A ±5–10 point swing between runs is normal — the analyst reasons about "
    "your profile like a recruiter would, not by a fixed formula. Focus on the "
    "specific feedback, not the exact number."
)

VERIFY_DISCLAIMER: Final = "Review and adjust before sending — Carigma drafts, you verify."
