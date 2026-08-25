"""Market intelligence.

Two rules carry everything, and the second is the one P6-3 depends on:

1. **A human curates, and an unsourced claim cannot be published.** That is
   what makes "real and sourced, never fabricated" structural rather than a
   prompt instruction we hope holds.
2. **No curated research this week means no wire and no email — never a re-run
   of last week's.** A stale digest resent is manufactured contact, and it does
   the most damage in the lapsed-user email.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from carigma_api.services.market import (
    DEFAULT_LIFETIME,
    Batch,
    BatchState,
    MarketItem,
    NotPublishable,
    assert_publishable,
    gist_for_email,
    live_batch,
    wire_for,
)

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def item(
    headline: str = "Analytics hiring up 12%", url: str = "https://example.com/report"
) -> MarketItem:
    return MarketItem(
        headline=headline,
        detail="Across Bengaluru and Pune.",
        source_name="Some Report",
        source_url=url,
    )


def batch(
    key: str = "mkt_2026_w33",
    *,
    published: datetime | None = NOW - timedelta(days=1),
    refresh: datetime | None = None,
    items: list[MarketItem] | None = None,
) -> Batch:
    return Batch(
        batch_key=key,
        items=items if items is not None else [item()],
        published_at=published,
        next_refresh=refresh,
    )


# ── The human-in-the-loop gate ─────────────────────────────────────────────


def test_an_unsourced_item_cannot_be_published() -> None:
    """A claim a reader cannot check is indistinguishable from a fabricated
    one."""
    with pytest.raises(NotPublishable, match="source URL"):
        assert_publishable([item(url="")])


def test_a_source_must_be_a_real_link_not_just_a_name() -> None:
    """A name is free text someone can fill with anything. A URL is a thing a
    reader can open."""
    with pytest.raises(NotPublishable):
        assert_publishable([MarketItem("Claim", "Detail", "Trust me", "not-a-url")])


def test_an_empty_batch_cannot_be_published() -> None:
    """The honest way to have nothing to say is to leave it a draft — which
    the freshness gate already handles as silence."""
    with pytest.raises(NotPublishable, match="no items"):
        assert_publishable([])


def test_a_fully_sourced_batch_publishes() -> None:
    assert_publishable([item(), item("Second thing", "https://example.com/two")])


# ── The freshness gate — P6-3 depends on this ──────────────────────────────


def test_a_draft_is_not_live() -> None:
    assert live_batch([batch(published=None)], NOW) is None


def test_an_expired_batch_is_not_live() -> None:
    stale = batch(published=NOW - timedelta(days=30))
    assert stale.state(NOW) is BatchState.EXPIRED
    assert live_batch([stale], NOW) is None


def test_next_refresh_overrides_the_default_lifetime() -> None:
    """The curator's explicit expiry wins over the fallback."""
    short = batch(published=NOW - timedelta(days=2), refresh=NOW - timedelta(hours=1))
    assert short.state(NOW) is BatchState.EXPIRED

    long = batch(published=NOW - timedelta(days=20), refresh=NOW + timedelta(days=1))
    assert long.state(NOW) is BatchState.PUBLISHED


def test_a_batch_with_no_refresh_expires_on_the_default_lifetime() -> None:
    edge = batch(published=NOW - DEFAULT_LIFETIME)
    assert edge.state(NOW) is BatchState.EXPIRED

    fresh = batch(published=NOW - DEFAULT_LIFETIME + timedelta(hours=1))
    assert fresh.state(NOW) is BatchState.PUBLISHED


def test_nothing_live_NEVER_falls_back_to_last_week() -> None:
    """The whole failure mode. A fallback here looks like resilience and
    behaves like fabrication."""
    last_week = batch("mkt_2026_w32", published=NOW - timedelta(days=30))
    draft = batch("mkt_2026_w33", published=None)

    assert live_batch([last_week, draft], NOW) is None


def test_the_newest_live_batch_wins() -> None:
    older = batch("mkt_2026_w32", published=NOW - timedelta(days=6))
    newer = batch("mkt_2026_w33", published=NOW - timedelta(days=1))

    chosen = live_batch([older, newer], NOW)
    assert chosen is not None and chosen.batch_key == "mkt_2026_w33"


# ── What each consumer gets ────────────────────────────────────────────────


def test_the_wire_renders_an_honest_nothing_rather_than_stale_items() -> None:
    wire = wire_for([batch(published=NOW - timedelta(days=30))], NOW)

    assert wire.available is False
    assert wire.items == ()
    assert wire.empty is not None
    # <EmptyHonest>'s three parts, so no surface has to invent copy.
    assert set(wire.empty) == {"happened", "why", "next"}


def test_the_empty_state_does_not_blame_the_user() -> None:
    """A market read is true regardless of whether anyone opened the app, so
    its absence is our business, not theirs."""
    text = " ".join(wire_for([], NOW).empty.values()).lower()  # type: ignore[union-attr]

    for banned in ("you", "your", "miss", "behind", "inactive"):
        assert banned not in text.split(), f"{banned!r} makes our gap the user's fault"


def test_a_live_wire_carries_the_items_and_the_batch_it_came_from() -> None:
    wire = wire_for([batch()], NOW)

    assert wire.available is True
    assert len(wire.items) == 1
    assert wire.batch_key == "mkt_2026_w33"


def test_the_email_gist_is_None_not_empty_when_there_is_no_research() -> None:
    """Different TYPES, not different lengths. A caller must be unable to treat
    "no research" as "an email with an empty section" — that email is the
    manufactured contact this whole feature refuses to send."""
    assert gist_for_email([batch(published=None)], NOW) is None
    assert gist_for_email([], NOW) is None


def test_the_email_gist_is_the_same_curation_the_wire_shows() -> None:
    """One curation, two audiences. If these could diverge, the product would
    say different things inside and outside the app."""
    live = batch(items=[item(), item("Two", "https://example.com/2")])

    wire = wire_for([live], NOW)
    gist = gist_for_email([live], NOW)

    assert gist is not None
    assert [i.headline for i in gist] == [i.headline for i in wire.items]


def test_the_gist_is_capped_so_an_email_stays_readable() -> None:
    many = batch(items=[item(f"Item {n}", f"https://example.com/{n}") for n in range(10)])

    gist = gist_for_email([many], NOW, limit=3)

    assert gist is not None and len(gist) == 3
