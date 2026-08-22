"""Event-triggered emails. More reasons, never more frequency.

The rule is the whole feature, so most of this file is about the ceiling
rather than about the five triggers individually. Five reasons that produce
five emails is not a retention feature, it is the thing that makes people
unsubscribe — and once they do, the ones that mattered never arrive either.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from carigma_api.services import triggers
from carigma_api.services.emails import (
    EmailType,
    Preferences,
    SendStatus,
    send,
)
from carigma_api.services.triggers import DAILY_SLOT, Facts, choose, slot_key

USER = "user-1"
TO = "ravi@example.com"
TODAY = date(2026, 8, 17)


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, email: Any) -> None:
        self.sent.append(email)


class FakeLog:
    """Models the UNIQUE INDEX, because that index IS the ceiling.

    A log whose `claim_period` always returned True would let every test here
    pass while five emails went out on a Tuesday.
    """

    def __init__(self) -> None:
        self.claims: set[tuple[str, str, str]] = set()
        self.records: list[dict[str, Any]] = []

    def record(self, email: Any, **kw: Any) -> None:
        self.records.append(dict(kw))

    def claim_period(self, user_id: str, email_type: str, period_key: str) -> bool:
        key = (user_id, email_type, period_key)
        if key in self.claims:
            return False
        self.claims.add(key)
        return True


def a_match(score: int = 92) -> dict[str, Any]:
    return {"id": "job-1", "title": "Senior Data Analyst", "company": "Zomato", "score": score}


def everything_true() -> Facts:
    """All five facts at once. The interesting case."""
    return Facts(
        top_match=a_match(),
        band_move={
            "from": "EMERGING",
            "to": "CLEAR",
            "receipt": "headline now names Power BI",
            "improved": True,
        },
        stale_application={"id": "app-1", "company": "Swiggy", "role": "Analyst", "days": 12},
        milestone={"id": "m-1", "title": "10 posts", "text": "Ten posts shipped."},
        profile_update={"id": "u-1", "changes": 3},
    )


# ── The ceiling ────────────────────────────────────────────────────────────


def test_five_true_facts_produce_one_email() -> None:
    """The whole point. Not five, not a batched digest — one."""
    chosen = choose(everything_true())
    assert chosen is not None
    assert chosen.key == "stale_application"


def test_the_daily_slot_is_shared_with_the_daily_brief(FakeEmail: Any = None) -> None:
    """A brief and an event on the same day is two emails, whatever we call it.

    They claim the same namespace, so the database refuses the second. This is
    the difference between the rule being enforced and being remembered.
    """
    from carigma_api.services.emails import DailyFacts, build_daily_brief

    mailer, log = FakeMailer(), FakeLog()
    common = {
        "mailer": mailer,
        "log": log,
        "user_id": USER,
        "recipient": TO,
        "period_key": slot_key(TODAY),
        "prefs": Preferences(),
        "claim_as": DAILY_SLOT,
        "service_key": "test-signing-key",
        "app_url": "https://app.example.com",
    }

    brief = build_daily_brief(TO, DailyFacts(new_matches=3))
    first = send(brief, email_type=EmailType.DAILY_BRIEF, **common)

    trigger = choose(everything_true())
    assert trigger is not None
    event = trigger.build(TO, everything_true())
    second = send(event, email_type=EmailType.EVENT, **common)

    assert first.status is SendStatus.SENT
    assert second.status is SendStatus.DUPLICATE
    assert len(mailer.sent) == 1


def test_a_new_trigger_cannot_raise_anyones_volume() -> None:
    """The property that has to survive the next person adding a sixth.

    Nothing in the ceiling knows how many triggers exist: the claim is on
    (user, DAILY_SLOT, day). Simulated here by sending every trigger's email
    on one day and counting what actually left.
    """
    mailer, log = FakeMailer(), FakeLog()
    facts = everything_true()

    for trigger in triggers.TRIGGERS:
        email = trigger.build(TO, facts)
        send(
            email,
            mailer=mailer,
            log=log,
            user_id=USER,
            recipient=TO,
            email_type=EmailType.EVENT,
            period_key=slot_key(TODAY),
            prefs=Preferences(),
            claim_as=DAILY_SLOT,
            service_key="test-signing-key",
            app_url="https://app.example.com",
        )

    assert len(mailer.sent) == 1, "the ceiling is one per user per day, whatever fires"


def test_tomorrow_is_a_new_slot() -> None:
    """A ceiling that never reopened would be a mute button, not a cadence."""
    mailer, log = FakeMailer(), FakeLog()
    facts = everything_true()
    trigger = choose(facts)
    assert trigger is not None

    for day in (TODAY, date(2026, 8, 18)):
        send(
            trigger.build(TO, facts),
            mailer=mailer,
            log=log,
            user_id=USER,
            recipient=TO,
            email_type=EmailType.EVENT,
            period_key=slot_key(day),
            prefs=Preferences(),
            claim_as=DAILY_SLOT,
            service_key="test-signing-key",
            app_url="https://app.example.com",
        )

    assert len(mailer.sent) == 2


def test_the_ceiling_is_per_user() -> None:
    """One noisy day for one person is not a quiet day for everyone else."""
    mailer, log = FakeMailer(), FakeLog()
    facts = everything_true()
    trigger = choose(facts)
    assert trigger is not None

    for user in ("user-1", "user-2"):
        send(
            trigger.build(TO, facts),
            mailer=mailer,
            log=log,
            user_id=user,
            recipient=TO,
            email_type=EmailType.EVENT,
            period_key=slot_key(TODAY),
            prefs=Preferences(),
            claim_as=DAILY_SLOT,
            service_key="test-signing-key",
            app_url="https://app.example.com",
        )

    assert len(mailer.sent) == 2


# ── Priority, as an exact sequence ─────────────────────────────────────────


def test_the_order_is_pinned() -> None:
    """Reviewed as a list, like the Today ladder.

    Pairwise assertions let a reordering slip through in the middle; the whole
    sequence is what someone actually needs to agree with.
    """
    assert [t.key for t in sorted(triggers.TRIGGERS, key=lambda t: t.priority)] == [
        "stale_application",
        "profile_update",
        "band_move",
        "milestone",
        "strong_match",
    ]


def test_the_priorities_are_distinct() -> None:
    """A tie would make `sorted` order depend on declaration order, which is
    not a decision anybody made."""
    priorities = [t.priority for t in triggers.TRIGGERS]
    assert len(set(priorities)) == len(priorities)


def test_a_closing_window_beats_a_thing_with_no_deadline() -> None:
    """Time-bound beats browsable — the first of the two ranking axes."""
    facts = Facts(
        milestone={"id": "m-1", "text": "Ten posts shipped."},
        stale_application={"id": "app-1", "company": "Swiggy", "days": 12},
    )
    chosen = choose(facts)
    assert chosen is not None and chosen.key == "stale_application"


def test_something_they_did_beats_something_we_found() -> None:
    """The second axis.

    A band move is theirs; a match is inventory, and inventory is in Jobs
    whenever they want it. The first draft had this backwards and led with the
    match — the most interruptive email being about the thing that had not
    changed and would still be there tomorrow.
    """
    facts = Facts(
        top_match=a_match(),
        band_move={"from": "EMERGING", "to": "CLEAR", "receipt": "why", "improved": True},
    )
    chosen = choose(facts)
    assert chosen is not None and chosen.key == "band_move"


def test_even_a_milestone_outranks_inventory() -> None:
    """A milestone has nothing to do about it, and still wins.

    That follows from the second axis rather than in spite of it: "ten posts
    shipped" is a fact about them, and a job posting is a fact about the market
    that Jobs already shows.
    """
    facts = Facts(top_match=a_match(), milestone={"id": "m", "text": "Ten posts shipped."})
    chosen = choose(facts)
    assert chosen is not None and chosen.key == "milestone"


# ── Each fact has to be true ───────────────────────────────────────────────


def test_nothing_true_sends_nothing() -> None:
    assert choose(Facts()) is None


def test_a_mediocre_match_is_not_an_interruption() -> None:
    """Below the bar it belongs in the feed, where the user looks by choice."""
    assert choose(Facts(top_match=a_match(score=triggers.STRONG_MATCH - 1))) is None
    assert choose(Facts(top_match=a_match(score=triggers.STRONG_MATCH))) is not None


def test_a_band_move_without_a_receipt_does_not_fire() -> None:
    """A score change with no evidence is a number we are asking someone to
    trust. The receipt is a precondition, not a nice-to-have."""
    assert choose(Facts(band_move={"from": "EMERGING", "to": "CLEAR"})) is None
    assert (
        choose(Facts(band_move={"from": "EMERGING", "to": "CLEAR", "receipt": "why"})) is not None
    )


def test_a_match_we_cannot_name_produces_no_email() -> None:
    """`applies` passes on the score, and `build` still refuses.

    Two layers because they answer different questions: whether it is worth an
    email, and whether we can write an honest one.
    """
    facts = Facts(top_match={"id": "job-1", "score": 95})
    chosen = choose(facts)
    assert chosen is not None
    assert chosen.build(TO, facts) is None


def test_a_profile_update_email_never_implies_it_was_applied() -> None:
    """P6-1's whole design is approval. The email must not undercut it."""
    facts = Facts(profile_update={"id": "u-1", "changes": 2})
    email = choose(facts).build(TO, facts)  # type: ignore[union-attr]
    assert email is not None
    assert "Nothing has been changed" in email.body
    assert "approval" in email.body


# ── The other grammars ─────────────────────────────────────────────────────


def test_a_lapsed_user_hears_only_the_re_engagement_sequence() -> None:
    """Two conversations at once reads as a system that does not know who it
    is talking to. The lapsed sequence has its own cadence and its own end."""
    assert choose(Facts(lapsed=True, top_match=a_match())) is None


def test_the_daily_brief_toggle_governs_events() -> None:
    """An event email is a daily-brief-shaped thing: "here is what happened".

    A separate toggle would be a second chance to reach someone who already
    said no.
    """
    off = Preferences(daily_brief=False)
    assert off.allows(EmailType.EVENT) is False
    assert off.allows(EmailType.WEEKLY_REVIEW) is True


def test_unsubscribing_silences_events() -> None:
    assert Preferences(unsubscribed_all=True).allows(EmailType.EVENT) is False


def test_a_receipt_still_gets_through() -> None:
    """Unsubscribing from marketing must not stop proof that someone paid."""
    assert Preferences(unsubscribed_all=True).allows(EmailType.RECEIPT) is True


def test_an_unsubscribed_user_burns_no_slot() -> None:
    """Preferences are checked before the claim, so a suppressed event does not
    silently consume the day and block a brief the user DID want."""
    mailer, log = FakeMailer(), FakeLog()
    facts = everything_true()
    trigger = choose(facts)
    assert trigger is not None

    send(
        trigger.build(TO, facts),
        mailer=mailer,
        log=log,
        user_id=USER,
        recipient=TO,
        email_type=EmailType.EVENT,
        period_key=slot_key(TODAY),
        prefs=Preferences(daily_brief=False),
        claim_as=DAILY_SLOT,
        service_key="test-signing-key",
        app_url="https://app.example.com",
    )

    assert log.claims == set(), "an unsubscribed send must not claim the slot"


# ── Tone ───────────────────────────────────────────────────────────────────


def test_no_trigger_email_can_shame_anyone() -> None:
    """`Email.__post_init__` runs `assert_no_guilt`, so constructing one is the
    check. The stale-application email is the risk: "you haven't followed up"
    is one clumsy sentence away from a telling-off."""
    facts = everything_true()
    for trigger in triggers.TRIGGERS:
        email = trigger.build(TO, facts)
        assert email is not None, trigger.key
        assert email.email_type is EmailType.EVENT


def test_the_stale_application_email_offers_closing_it_as_a_real_answer() -> None:
    facts = Facts(stale_application={"id": "a", "company": "Swiggy", "days": 12})
    email = choose(facts).build(TO, facts)  # type: ignore[union-attr]
    assert email is not None
    assert "Closing it out is also" in email.body
    # No instruction, no deadline invented for them.
    assert "you should" not in email.body.lower()
    assert "must" not in email.body.lower()


# ── Structural ─────────────────────────────────────────────────────────────


def test_every_proactive_email_claims_the_shared_slot() -> None:
    """The guard on the rule itself.

    A sender that calls `send()` for a marketing email WITHOUT
    `claim_as=DAILY_SLOT` gets its own namespace back, and the ceiling quietly
    becomes one-per-type-per-day.

    ## This guard was vacuous on its first write, twice over

    It scanned `src/` — the senders live in `scripts/` — and it matched
    `ast.Name`, while every real call site is `mail.send(...)`, an
    `ast.Attribute`. It found zero call sites and passed, which is
    indistinguishable from finding them all correct.

    Hence `assert found`, below: a guard that can pass by finding NOTHING needs
    a non-empty assertion on its input.
    """
    roots = [Path(triggers.__file__).parents[1], Path(triggers.__file__).parents[3] / "scripts"]
    offenders: list[str] = []
    found = 0

    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # `send(...)` or `<anything>.send(...)` — the second is the
                # shape every real caller uses.
                callee = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                    if isinstance(node.func, ast.Name)
                    else None
                )
                if callee != "send":
                    continue
                kwargs = {k.arg for k in node.keywords if k.arg}
                # `email_type=` is what distinguishes the email sender from a
                # mailer's own `.send(email)`.
                if "email_type" not in kwargs:
                    continue
                if any(k.arg is None for k in node.keywords):
                    continue  # `**common`: cannot see inside, not judged
                found += 1
                if "claim_as" not in kwargs:
                    offenders.append(f"{path.name}:{node.lineno}")

    assert found >= 2, f"no send() call sites found — this guard proves nothing (saw {found})"
    assert offenders == [], (
        "a proactive send without `claim_as` gets its own daily namespace, "
        f"which turns one email a day into one PER TYPE per day: {offenders}"
    )


@pytest.mark.parametrize("trigger", triggers.TRIGGERS, ids=lambda t: t.key)
def test_every_trigger_identifies_the_fact_that_fired(trigger: Any) -> None:
    """So the same band crossing does not re-send tomorrow when the slot frees.

    The ceiling limits volume; identity is what stops repetition. Without it,
    one crossing would be announced every day until something else outranked
    it.
    """
    identity = trigger.identity(everything_true())
    assert identity and identity != "None", trigger.key


# ── The cross-TYPE order ───────────────────────────────────────────────────


def test_the_type_order_is_pinned() -> None:
    """Which email wins the day, decided rather than left to cron ordering.

    The slot guarantees ONE proactive email. It does not decide which, and
    "whichever cron ran first" is an accident of how two schedule entries were
    written — not something anybody chose.
    """
    from carigma_api.services.emails import PROACTIVE_ORDER

    assert [str(t) for t in PROACTIVE_ORDER] == [
        "weekly_review",
        "event",
        "daily_brief",
        "market_digest",
    ]


def test_a_weekly_review_beats_everything_it_shares_a_day_with() -> None:
    """It happens once. A missed one means that week has no record at all."""
    from carigma_api.services.emails import outranks

    for loser in (EmailType.EVENT, EmailType.DAILY_BRIEF, EmailType.MARKET_DIGEST):
        assert outranks(EmailType.WEEKLY_REVIEW, loser), loser


def test_something_specific_beats_the_general_roundup() -> None:
    """An event is about them. The brief is the most repeatable thing we send."""
    from carigma_api.services.emails import outranks

    assert outranks(EmailType.EVENT, EmailType.DAILY_BRIEF)
    assert not outranks(EmailType.DAILY_BRIEF, EmailType.EVENT)


def test_ranking_a_receipt_is_refused_rather_than_answered() -> None:
    """A receipt does not compete for the slot, so there is no right answer.

    Returning False would be a wrong answer to a question nobody should have
    asked, and the caller would carry on believing the ranking meant something.
    """
    from carigma_api.services.emails import outranks

    with pytest.raises(ValueError):
        outranks(EmailType.RECEIPT, EmailType.DAILY_BRIEF)


def test_every_proactive_type_is_ranked() -> None:
    """A marketing type missing from the order would silently never win.

    The non-empty rule's cousin: a list that omits a member does not fail, it
    just quietly makes that member last forever.
    """
    from carigma_api.services.emails import PROACTIVE_ORDER

    marketing = {t for t in EmailType if t.is_marketing}
    assert marketing == set(PROACTIVE_ORDER), (
        f"unranked marketing types would never win a contested day: "
        f"{marketing - set(PROACTIVE_ORDER)}"
    )
