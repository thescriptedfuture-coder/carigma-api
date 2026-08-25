"""Where a user is in setup, derived from their profile.

## Derived, never stored

There is no `onboarding_step` column and there must not be one. A stored step
is a second source of truth about the same profile: close the tab after the
extraction and a stored `step = 2` disagrees with a row that plainly has a
headline in it. The step is a FUNCTION of what has been written, so a refresh,
a new device and a week later all answer the same way.

The one thing that IS stored is `onboarded`, because it is not derivable:
"finished setup" and "has enough fields" are different claims, and only the
user can make the first one.

## Measured against the real rows before it was written

Twelve live profiles:

    onboarded    8/12 true, 4 null      cadence      8/12
    name        10/12                   platforms    0/12
    target_roles 10/12                  target_role  4/12

Two things follow.

**`onboarded` does not imply `target_role`.** Eight profiles are onboarded and
only four have one. Anything that requires it on an onboarded profile is broken
for half of them today, so `TARGETS` checks `target_roles` — the field ten of
twelve actually carry — and treats `target_role` as a refinement.

**`platforms` is empty on every row**, so the platform step is genuinely new
and every existing user will meet it. It cannot be a blocking step for them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

#: Cadences the product actually supports, and V1's live distribution:
#: 3 (six users), 5 (one), 2 (one). Offered in that order.
CADENCES: tuple[int, ...] = (2, 3, 5)
DEFAULT_CADENCE = 3

#: What the platform step may write. `naukri` is India-specific and the reason
#: the step exists — the two lenses never share an axis.
PLATFORMS: tuple[str, ...] = ("linkedin", "naukri")


class Step(StrEnum):
    """The four beats of Brief 3a, plus the two ends."""

    #: Nothing extracted yet. The upload/paste screen.
    UPLOAD = "upload"
    #: Text is in, the analyst has not produced a score yet.
    DECODING = "decoding"
    #: The reveal: the gap, the real rewrite, the week. Nothing to fill in.
    REVEAL = "reveal"
    #: Platform preference, targets and cadence.
    TARGETS = "targets"
    #: Finished. `onboarded` is true.
    DONE = "done"


def has_profile_text(profile: dict[str, Any]) -> bool:
    """Whether an extraction has landed.

    Checks the fields extraction actually produces rather than `name` alone: a
    LinkedIn export without a parseable name still yields a headline and an
    experience section, and sending that user back to the upload screen would
    make them redo work that succeeded.
    """
    return any(
        str(profile.get(key) or "").strip()
        for key in ("currentRole", "linkedinHeadline", "experience", "skills", "aboutSection")
    )


def step_for(profile: dict[str, Any], *, scored: bool) -> Step:
    """Where this user is. `scored` says whether the analyst has produced one.

    Order matters: `onboarded` wins over everything, because a user who
    finished setup and later cleared a field has not become un-onboarded.
    """
    if profile.get("onboarded"):
        return Step.DONE
    if not has_profile_text(profile):
        return Step.UPLOAD
    if not scored:
        return Step.DECODING
    if not str(profile.get("targetRoles") or "").strip():
        return Step.REVEAL
    return Step.TARGETS


def normalise_platforms(raw: list[str] | None) -> list[str]:
    """Keep the known ones, in a stable order, without duplicates.

    An empty result is legitimate and means "neither" — a user who is not on
    Naukri and does not want LinkedIn scoring. It is NOT the same as never
    having been asked, which is `None` on the column, and the two must stay
    distinguishable: all twelve live rows are the second.
    """
    chosen = {p.strip().lower() for p in (raw or [])}
    return [p for p in PLATFORMS if p in chosen]


def completion(
    *,
    target_roles: str,
    cadence: int | None,
    platforms: list[str] | None,
    location: str | None,
) -> dict[str, Any]:
    """The profile patch that finishes setup.

    `targetRole` (singular) is set alongside `targetRoles` because V1 writes
    both and downstream code reads both. Only four of twelve live rows have the
    singular, so nothing may depend on it being present — but new rows should
    not add to that.
    """
    roles = target_roles.strip()
    if not roles:
        # The one hard requirement. Every agent aims at something, and a blank
        # target means the Scout searches for nothing and the analyst
        # benchmarks against nothing.
        raise ValueError("Pick a target role so the agents know what to aim at.")

    patch: dict[str, Any] = {
        "targetRoles": roles,
        "targetRole": roles.split(",")[0].strip(),
        "cadence": cadence if cadence in CADENCES else DEFAULT_CADENCE,
        "platforms": normalise_platforms(platforms),
        "onboarded": True,
    }
    if city := (location or "").strip():
        patch["location"] = city
    return patch


__all__ = [
    "CADENCES",
    "DEFAULT_CADENCE",
    "PLATFORMS",
    "Step",
    "completion",
    "has_profile_text",
    "normalise_platforms",
    "step_for",
]
