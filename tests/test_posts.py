"""The content practice — and the two rules the streak turns on.

1. Only a user-confirmed publish advances the streak. We never auto-post, so
   we have no other honest evidence that anything was shipped.
2. A skip PAUSES the streak; it never resets it.
"""

from __future__ import annotations

from datetime import date

import pytest

from carigma_api.services.posts import (
    Draft,
    ReviewNote,
    SkipReason,
    Slot,
    SlotState,
    SlotTransitionError,
    WeekPlan,
    default_week,
    mark_published,
    skip,
    streak_state,
)

MONDAY = date(2026, 8, 3)


def week(**kw: object) -> WeekPlan:
    base = default_week(MONDAY)
    return WeekPlan(
        week_start=base.week_start,
        slots=base.slots,
        cadence_days=base.cadence_days,
        cadence_per_week=base.cadence_per_week,
        **kw,  # type: ignore[arg-type]
    )


def with_slots(plan: WeekPlan, *slots: Slot) -> WeekPlan:
    return WeekPlan(
        week_start=plan.week_start,
        slots=slots,
        cadence_days=plan.cadence_days,
        cadence_per_week=plan.cadence_per_week,
        streak_weeks=plan.streak_weeks,
        paused_until=plan.paused_until,
    )


# ── The honest verb ────────────────────────────────────────────────────────


def test_only_a_confirmed_publish_counts() -> None:
    """Drafting is not shipping. The streak counts what the user did in the
    world, not what we produced."""
    pending = Slot("THU", date(2026, 8, 6))
    drafted = Slot(
        "THU", date(2026, 8, 6), state=SlotState.DRAFT_READY, drafts=(Draft(1, "a post"),)
    )

    assert pending.counts_toward_streak is False
    assert drafted.counts_toward_streak is False
    assert mark_published(drafted, at="09:02").counts_toward_streak is True


def test_marking_published_records_when_they_said_so() -> None:
    slot = mark_published(Slot("MON", MONDAY, state=SlotState.DRAFT_READY), at="09:02")

    assert slot.state is SlotState.PUBLISHED
    assert slot.published_at == "09:02"


def test_a_published_slot_cannot_be_published_twice() -> None:
    slot = mark_published(Slot("MON", MONDAY), at="09:02")
    with pytest.raises(SlotTransitionError, match="already marked published"):
        mark_published(slot, at="10:00")


def test_a_skipped_slot_cannot_then_be_marked_published() -> None:
    skipped = skip(Slot("MON", MONDAY), reason=SkipReason.NO_TIME)
    with pytest.raises(SlotTransitionError, match="was skipped"):
        mark_published(skipped, at="09:02")


# ── Skipping ───────────────────────────────────────────────────────────────


def test_a_committed_slot_needs_a_reason_because_the_reason_teaches() -> None:
    with pytest.raises(SlotTransitionError, match="needs a reason"):
        skip(Slot("MON", MONDAY))


def test_an_optional_slot_is_skipped_free_with_no_reason_asked() -> None:
    """Asking someone to justify not doing something they never committed to
    is how a tool starts to nag."""
    slot = skip(Slot("SAT", date(2026, 8, 8), optional=True))

    assert slot.state is SlotState.SKIPPED
    assert slot.skip_reason is None


def test_only_the_reasons_about_the_writing_retrain_the_agent() -> None:
    """ "Travelling" says nothing about the drafts. Treating it as negative
    feedback would teach the model the wrong lesson from a fine week."""
    assert SkipReason.NOT_MY_TOPIC.retrains is True
    assert SkipReason.DRAFTS_WEAK.retrains is True
    assert SkipReason.NO_TIME.retrains is False
    assert SkipReason.OTHER.retrains is False


def test_a_skip_note_is_the_users_own_words_and_is_kept_verbatim() -> None:
    """The tone law is a rule about copy CARIGMA writes. Running it over this
    field would mean refusing to record "I feel like I'm falling behind" —
    the product censoring the user to protect its own style guide."""
    note = "I'm falling behind and need a week off."
    assert skip(Slot("MON", MONDAY), reason=SkipReason.OTHER, note=note).skip_note == note


def test_a_published_slot_cannot_be_skipped() -> None:
    published = mark_published(Slot("MON", MONDAY), at="09:02")
    with pytest.raises(SlotTransitionError, match="already published"):
        skip(published, reason=SkipReason.NO_TIME)


# ── The streak ─────────────────────────────────────────────────────────────


def test_shipping_every_committed_slot_advances_the_streak() -> None:
    plan = with_slots(
        week(streak_weeks=6),
        mark_published(Slot("MON", MONDAY), at="09:02"),
        mark_published(Slot("THU", date(2026, 8, 6)), at="09:04"),
        Slot("SAT", date(2026, 8, 8), optional=True),
    )
    assert streak_state(plan).weeks == 7
    assert streak_state(plan).paused is False


def test_an_optional_slot_left_alone_does_not_hold_the_streak_back() -> None:
    """It was never promised, so not doing it cannot cost anything."""
    plan = with_slots(
        week(streak_weeks=6),
        mark_published(Slot("MON", MONDAY), at="09:02"),
        mark_published(Slot("THU", date(2026, 8, 6)), at="09:04"),
        skip(Slot("SAT", date(2026, 8, 8), optional=True)),
    )
    assert streak_state(plan).weeks == 7


def test_a_skip_pauses_the_streak_and_never_resets_it() -> None:
    """THE rule. Six weeks of work does not evaporate because someone
    travelled."""
    plan = with_slots(
        week(streak_weeks=6),
        skip(Slot("MON", MONDAY), reason=SkipReason.NO_TIME),
        skip(Slot("THU", date(2026, 8, 6)), reason=SkipReason.NO_TIME),
    )
    state = streak_state(plan)

    assert state.weeks == 6, "the streak must be HELD, not lost"
    assert state.paused is True
    assert "Paused at 6 weeks" == state.sentence()


def test_a_paused_streak_never_reads_as_zero() -> None:
    for weeks in (1, 6, 40):
        plan = with_slots(
            week(streak_weeks=weeks),
            skip(Slot("MON", MONDAY), reason=SkipReason.DRAFTS_WEAK),
        )
        assert streak_state(plan).weeks == weeks


def test_cadence_zero_pauses_rather_than_breaks() -> None:
    """ "No slots this week — you set cadence to 0 for exam season." A
    deliberate pause is a decision, not a lapse."""
    plan = WeekPlan(week_start=MONDAY, slots=(), cadence_per_week=0, streak_weeks=6)
    state = streak_state(plan)

    assert state.paused is True
    assert state.weeks == 6
    assert state.sentence() == "Paused at 6 weeks"


def test_a_partial_week_holds_the_streak_rather_than_advancing_it() -> None:
    plan = with_slots(
        week(streak_weeks=6),
        mark_published(Slot("MON", MONDAY), at="09:02"),
        Slot("THU", date(2026, 8, 6), state=SlotState.DRAFT_READY),
    )
    assert streak_state(plan).weeks == 6
    assert streak_state(plan).paused is False


def test_no_streak_sentence_shames() -> None:
    from carigma_api.services.weekly import assert_no_guilt

    for weeks, paused in ((0, False), (6, False), (6, True)):
        plan = WeekPlan(
            week_start=MONDAY, slots=(), streak_weeks=weeks, cadence_per_week=0 if paused else 2
        )
        assert_no_guilt(streak_state(plan).sentence())


# ── Drafts ─────────────────────────────────────────────────────────────────


def test_reading_time_is_an_estimate_not_false_precision() -> None:
    draft = Draft(1, " ".join(["word"] * 240))
    assert draft.word_count == 240
    # 240 words at 240 wpm is a minute; rounded to the nearest 5s.
    assert draft.reading_seconds == 60
    assert draft.reading_seconds % 5 == 0


def test_a_very_short_draft_still_reports_a_sane_time() -> None:
    assert Draft(1, "Hi.").reading_seconds >= 5


def test_a_slot_reports_how_many_variants_it_actually_has() -> None:
    slot = Slot("THU", date(2026, 8, 6), drafts=(Draft(1, "a"), Draft(2, "b", "stronger hook")))
    assert slot.as_dict()["variant_count"] == 2


# ── The review checklist ───────────────────────────────────────────────────


def test_a_suggestion_is_marked_as_advice_not_failure() -> None:
    """ "▲ Ending is soft — consider a question" sits beside the ticks. It is
    advisory: a checklist that scolds makes people defend their own writing."""
    assert ReviewNote(True, "Hook — question in line 1").as_dict()["glyph"] == "✓"
    assert ReviewNote(False, "Ending is soft — consider a question").as_dict()["glyph"] == "▲"


# ── The week plan ──────────────────────────────────────────────────────────


def test_the_default_week_carries_the_optional_one_off() -> None:
    plan = default_week(MONDAY)
    optional = [s for s in plan.slots if s.optional]

    assert len(optional) == 1
    assert optional[0].day == "SAT"
    assert plan.committed == 2, "the optional slot is not a commitment"


def test_the_cadence_label_says_which_days() -> None:
    assert default_week(MONDAY).as_dict()["cadence_label"] == "2/wk · Mon + Thu"


def test_a_paused_week_says_paused_rather_than_showing_zero_slots() -> None:
    plan = WeekPlan(week_start=MONDAY, slots=(), cadence_per_week=0, streak_weeks=6)
    assert plan.as_dict()["cadence_label"] == "Paused"
    assert plan.is_paused is True
