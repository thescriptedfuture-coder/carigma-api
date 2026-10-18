"""Content Intelligence's writer: what it may write from, and what it may not ship.

The drafts go out under a real person's name. A prompt asks the model not to
invent; `check` is what makes that true, so most of this file is about drafts
that must NOT survive.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from carigma_api.services.content_writer import (
    ANGLES,
    MIN_WORDS,
    NothingToWriteFrom,
    brief_for,
    check,
    prompt,
    write,
)
from carigma_api.services.posts import Draft, SkipReason, Slot, SlotState, WeekPlan

MONDAY = date(2026, 10, 12)

PROFILE: dict[str, Any] = {
    "name": "Ravi Kumar",
    "currentRole": "Senior Auditor / Functional Business Analyst at CGDA",
    "targetRoles": "Senior Data Analyst, Data Analyst",
    "experience": "CGDA | Senior Auditor. Led the PFMS rollout across 40 field offices.",
    "skills": "SQL, Power BI, Python, requirements analysis",
    "keyAchievement": "Cut monthly reconciliation time by 40% and saved ₹2 crore in duplicate payments.",
    "tone": "Plain, specific, a little dry",
    "contentAvoid": "politics",
}


def words(n: int, lead: str = "A post") -> str:
    return lead + " " + " ".join(f"word{i}" for i in range(n))


def body(lead: str = "Five years in audit taught me one thing about data", extra: str = "") -> str:
    return (
        lead
        + "\n\n"
        + words(MIN_WORDS + 20, "Here is what I mean.")
        + (" " + extra if extra else "")
    )


def week(*slots: Slot) -> WeekPlan:
    return WeekPlan(week_start=MONDAY, slots=slots)


THU = Slot("THU", date(2026, 10, 15))


def brief(**kw: Any) -> Any:
    return brief_for({**PROFILE, **kw}, THU, week=week(THU))


# ── What it may write from ─────────────────────────────────────────────────


def test_a_profile_with_nothing_to_write_from_is_refused() -> None:
    """A job title alone is not something to write from. Asked to write about
    a person we know nothing about, the honest output is nothing."""
    with pytest.raises(NothingToWriteFrom):
        brief_for({"name": "Someone", "currentRole": "Analyst"}, THU, week=week(THU))


def test_the_prompt_carries_the_profile_and_the_rules() -> None:
    system, user = prompt(brief())

    assert "Never invent an employer" in system
    assert "PFMS rollout" in user
    assert "Never write about: politics" in user
    for label, _guide in ANGLES:
        assert label in user


def test_a_retraining_skip_reaches_the_writer_with_the_users_words() -> None:
    """V1's lesson: skip feedback must demonstrably alter the next generation."""
    declined = Slot(
        "MON",
        MONDAY,
        state=SlotState.SKIPPED,
        skip_reason=SkipReason.DRAFTS_WEAK,
        skip_note="too corporate, sounds nothing like me",
    )
    travelling = Slot(
        "WED", date(2026, 10, 14), state=SlotState.SKIPPED, skip_reason=SkipReason.NO_TIME
    )

    _system, user = prompt(brief_for(PROFILE, THU, week=week(declined, travelling, THU)))

    assert "too corporate, sounds nothing like me" in user
    assert "Drafts weren't good enough" in user
    # "Travelling" says nothing about the writing.
    assert "Travelling" not in user


def test_last_weeks_declines_count_too() -> None:
    last = WeekPlan(
        week_start=date(2026, 10, 5),
        slots=(
            Slot(
                "THU",
                date(2026, 10, 8),
                state=SlotState.SKIPPED,
                skip_reason=SkipReason.NOT_MY_TOPIC,
            ),
        ),
    )

    _system, user = prompt(brief_for(PROFILE, THU, week=week(THU), previous=last))

    assert "Topic doesn't feel like me" in user


def test_a_redraft_is_told_which_hooks_it_already_used() -> None:
    drafted = Slot(
        "THU",
        date(2026, 10, 15),
        state=SlotState.DRAFT_READY,
        drafts=(Draft(index=1, body=body("The spreadsheet nobody owned")),),
    )

    _system, user = prompt(brief_for(PROFILE, drafted, week=week(drafted)))

    assert "ANGLES ALREADY USED" in user
    assert "The spreadsheet nobody owned" in user


# ── What it may not ship ───────────────────────────────────────────────────


def test_three_honest_drafts_survive_with_their_kind_as_the_note() -> None:
    raw = {
        "drafts": [
            {"angle": "Story", "body": body("The audit that changed how I read numbers")},
            {"angle": "practice", "body": body("One SQL habit from audit work")},
            {"angle": "View", "body": body("Dashboards are not the deliverable")},
        ]
    }

    kept, dropped = check(raw, brief())

    assert [d.note for d in kept] == ["Story", "Practice", "View"]
    assert dropped == []


def test_a_draft_with_a_blank_to_fill_is_dropped() -> None:
    """It would publish "[Company]" under someone's name."""
    raw = {"drafts": [{"angle": "Story", "body": body(extra="When I joined [Company], I")}]}

    kept, dropped = check(raw, brief())

    assert kept == []
    assert "blank" in dropped[0]


@pytest.mark.parametrize(
    "invented", ["cut costs by 63%", "saved ₹4 crore", "made it 3x faster", "a Rs 12 lakh budget"]
)
def test_a_figure_the_profile_does_not_contain_is_dropped(invented: str) -> None:
    """The classic invented metric. If the user never told us, it is not theirs."""
    raw = {"drafts": [{"angle": "Story", "body": body(extra=f"Last year we {invented}.")}]}

    kept, dropped = check(raw, brief())

    assert kept == [], f"{invented!r} survived"
    assert "figures the profile does not contain" in dropped[0]


@pytest.mark.parametrize("theirs", ["by 40%", "saved Rs 2 Cr", "₹2 crore"])
def test_a_figure_the_user_gave_us_survives_however_it_is_spelled(theirs: str) -> None:
    """ "₹2 crore" in the profile and "Rs 2 Cr" in the draft are the same claim.
    A check a respelling defeats would drop the honest posts and keep nothing."""
    raw = {"drafts": [{"angle": "Story", "body": body(extra=f"We cut it {theirs}.")}]}

    kept, _dropped = check(raw, brief())

    assert len(kept) == 1


def test_a_year_is_not_mistaken_for_rupees() -> None:
    raw = {"drafts": [{"angle": "Story", "body": body(extra="Five years 2019 to 2024.")}]}

    assert len(check(raw, brief())[0]) == 1


def test_too_short_too_long_empty_and_repeated_drafts_are_dropped() -> None:
    same = body("The same post twice")
    raw = {
        "drafts": [
            {"angle": "Story", "body": "Short."},
            {"angle": "Practice", "body": words(400)},
            {"angle": "View", "body": "   "},
        ]
    }
    assert check(raw, brief())[0] == []

    repeated = {"drafts": [{"angle": "Story", "body": same}, {"angle": "View", "body": same}]}
    kept, dropped = check(repeated, brief())
    assert len(kept) == 1
    assert dropped == ["a repeat of another draft"]


@pytest.mark.parametrize("raw", [None, "text", 7, {"posts": []}, {"drafts": "x"}, [None, 3]])
def test_a_reply_that_is_not_drafts_yields_none(raw: Any) -> None:
    assert check(raw, brief())[0] == []


def test_an_unknown_angle_keeps_the_draft_and_drops_the_label() -> None:
    """The chip says what kind of post it is — so a kind we did not ask for is
    not repeated as though we did."""
    kept, _ = check({"drafts": [{"angle": "Thought leadership", "body": body()}]}, brief())

    assert kept[0].note is None


def test_write_returns_only_what_survived() -> None:
    def writer(_system: str, _user: str, _max: int) -> Any:
        return {
            "drafts": [
                {"angle": "Story", "body": body()},
                {"angle": "View", "body": body("Different hook", extra="It grew 90%.")},
            ]
        }

    out = write(brief(), writer=writer)

    assert out == [{"body": body(), "note": "Story"}]


def test_a_dead_writer_raises_rather_than_returning_nothing() -> None:
    """A failure and an empty answer are different runs, and only the route
    decides how each is told. Swallowing here would make them the same."""

    def dead(*_a: Any) -> Any:
        raise RuntimeError("anthropic is down")

    with pytest.raises(RuntimeError):
        write(brief(), writer=dead)
