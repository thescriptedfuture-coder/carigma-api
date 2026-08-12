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
    "naukri_step": "Naukri tune-up step",
    "regenerate_slot": "Regenerate one post",
}

# Actions that are deliberately FREE. Listed explicitly so "is this free?" is a
# lookup rather than a judgement call scattered across routes.
#
# skip_slot: the user is handing us training signal. Charging for it would be
# charging someone to help us improve the product (P2 brief §21 Q4).
# onboarding_score: the first score is the product's promise; charging for it
# would gate the one thing that proves the value.
FREE_ACTIONS: Final = frozenset(
    {
        "skip_slot",
        "onboarding_score",
        "score_fix",
        # Mid-cycle Naukri work. `run_naukri` (10) buys the WHOLE bounded
        # tune-up, so every step after the first is free by being a different
        # ACTION rather than by a special case in the charging path.
        #
        # This is why per-cycle billing needed no change to the run protocol:
        # the cost is still a pure function of the action, and picking which
        # action a piece of work IS was always the caller's job. A
        # state-dependent price would have meant a `cost_override` parameter,
        # which is a way for every caller to set its own price.
        "naukri_step",
    }
)

# ── Score bands ────────────────────────────────────────────────────────────
# The V2 ladder, from the LOCKED design system (Brief 2 §"Score bands — a ladder
# of names, not colours"). This REPLACES V1's four-band
# buried/underselling/solid/strong set.
#
# Two properties the design is explicit about, and that the API must not
# undermine:
#   1. The band is a NEUTRAL LADDER — no red shame at 35, no green medal at 80.
#      The API therefore returns a name only; it never returns a colour, and the
#      client colours movement (▲/▼), not position.
#   2. There is deliberately NO combined-score band. LinkedIn and Naukri are two
#      instruments on two ladders; nothing here may average them.
SCORE_BANDS: Final = (
    (0, 39, "FAINT"),
    (40, 59, "EMERGING"),
    (60, 74, "CLEAR"),
    (75, 89, "STRONG"),
    (90, 100, "COMMANDING"),
)


def score_band(score: int) -> str:
    for low, high, name in SCORE_BANDS:
        if low <= score <= high:
            return name
    # Out of range is a caller bug, not user data — clamp to the nearest end
    # rather than inventing a sixth band.
    return "FAINT" if score < 0 else "COMMANDING"


# The honesty note that ships INSIDE the score payload. Claude reasons
# contextually rather than applying a fixed formula, so run-to-run variance is
# real; saying so is the guardrail against implying false precision.
VARIANCE_NOTE: Final = (
    "A ±5–10 point swing between runs is normal — the analyst reasons about "
    "your profile like a recruiter would, not by a fixed formula. Focus on the "
    "specific feedback, not the exact number."
)

VERIFY_DISCLAIMER: Final = "Review and adjust before sending — Carigma drafts, you verify."
