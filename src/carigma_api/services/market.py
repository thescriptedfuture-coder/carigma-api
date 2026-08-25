"""Market intelligence: surveyed by an agent, curated by a human, then live.

## The human is the honesty mechanism

"Real and sourced, never fabricated" is not a prompt instruction we hope holds.
An admin reads the survey, keeps what is true, and publishes. Everything a user
sees passed through that step, so the guarantee is structural rather than
aspirational — the model can only *propose*.

Which is why `publish` refuses an item without a source. A citation is what
makes the claim checkable, and an uncheckable market claim is indistinguishable
from a fabricated one.

## One curation, two audiences

Active users see the wire in-app; lapsed users get the same gist by email
(P6-3). One batch feeds both, so the product's voice is the same whether you
are inside it or not, and there is only one thing to keep true.

## The freshness rule, and why it is here rather than at each caller

**An unpublished or expired batch means no wire and no email — never a re-run
of last week's.** A stale digest resent is precisely the manufactured contact
the addendum forbids, and it does the most damage in the lapsed-user email,
where a fabricated reason to make contact costs the channel permanently.

So `live_batch()` is the single gate. Both the in-app wire and the email ask it
the same question and get the same answer; there is no second implementation to
diverge, and no caller position from which "just send last week's" is reachable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

#: How long a published batch stays live if the curator sets no explicit
#: expiry. Slightly over a week, so a survey running a day late does not create
#: a silent gap — but bounded, because "market intelligence" from a month ago
#: is not intelligence.
DEFAULT_LIFETIME = timedelta(days=8)


class BatchState(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    EXPIRED = "expired"


class NotPublishable(ValueError):
    """Refused before it can reach a user."""


@dataclass(frozen=True)
class MarketItem:
    """One claim, with the citation that makes it checkable."""

    headline: str
    detail: str
    source_name: str
    source_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "detail": self.detail,
            "source_name": self.source_name,
            "source_url": self.source_url,
        }

    @classmethod
    def from_stored(cls, raw: Any) -> MarketItem:
        data = raw if isinstance(raw, dict) else {}
        return cls(
            headline=str(data.get("headline") or ""),
            detail=str(data.get("detail") or ""),
            source_name=str(data.get("source_name") or ""),
            source_url=str(data.get("source_url") or ""),
        )

    @property
    def is_sourced(self) -> bool:
        """A claim without a real link is not sourced.

        Checked on the URL rather than on the name, because a name is free text
        someone can fill with anything; a URL is a thing a reader can open.
        """
        return self.source_url.startswith(("http://", "https://")) and bool(self.headline.strip())


@dataclass
class Batch:
    """A week's curated market intelligence."""

    batch_key: str
    items: list[MarketItem] = field(default_factory=list)
    published_at: datetime | None = None
    next_refresh: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Batch:
        return cls(
            batch_key=str(row.get("batch_key") or ""),
            items=[MarketItem.from_stored(i) for i in (row.get("items") or [])],
            published_at=_parse(row.get("published_at")),
            next_refresh=_parse(row.get("next_refresh")),
        )

    def state(self, now: datetime) -> BatchState:
        if self.published_at is None:
            return BatchState.DRAFT
        if self.expires_at is not None and now >= self.expires_at:
            return BatchState.EXPIRED
        return BatchState.PUBLISHED

    @property
    def expires_at(self) -> datetime | None:
        if self.published_at is None:
            return None
        return self.next_refresh or (self.published_at + DEFAULT_LIFETIME)

    def is_live(self, now: datetime) -> bool:
        return self.state(now) is BatchState.PUBLISHED

    def as_dict(self, now: datetime) -> dict[str, Any]:
        return {
            "batch_key": self.batch_key,
            "items": [i.as_dict() for i in self.items],
            "state": str(self.state(now)),
            "published_at": _iso(self.published_at),
            "expires_at": _iso(self.expires_at),
        }


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def assert_publishable(items: list[MarketItem]) -> None:
    """Refuse a batch that cannot be checked.

    An empty batch is refused too. Publishing nothing is not the same as
    publishing an empty week — the honest way to have nothing to say is to
    leave the batch unpublished, which the freshness gate already handles.
    """
    if not items:
        raise NotPublishable(
            "A batch with no items cannot be published. Leave it as a draft — "
            "no wire and no email is the correct outcome for a quiet week."
        )
    unsourced = [i.headline or "(untitled)" for i in items if not i.is_sourced]
    if unsourced:
        raise NotPublishable(
            "Every item needs a real source URL before it can go live. "
            f"Missing: {', '.join(unsourced)}"
        )


def live_batch(batches: list[Batch], now: datetime) -> Batch | None:
    """THE gate. The one published, unexpired batch — or nothing.

    Every consumer goes through here: the in-app wire, the Sunday review's
    gist, and the lapsed-user digest. `None` means there is nothing true to
    say this week, and the only correct response to that is silence.

    Deliberately does NOT fall back to the most recent batch. That fallback is
    the whole failure mode: it looks like resilience and behaves like
    fabrication, and it does the most damage in the email where a manufactured
    reason to make contact costs the channel permanently.
    """
    live = [b for b in batches if b.is_live(now)]
    if not live:
        return None
    # Newest wins if a curator ever publishes two — the later one is the
    # deliberate act.
    return max(live, key=lambda b: b.published_at or datetime.min.replace(tzinfo=UTC))


@dataclass(frozen=True)
class Wire:
    """What a surface renders. Carries its own emptiness rather than making
    each caller invent one."""

    available: bool
    items: tuple[MarketItem, ...] = ()
    batch_key: str | None = None
    empty: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "available": self.available,
            "items": [i.as_dict() for i in self.items],
            "batch_key": self.batch_key,
        }
        if self.empty:
            payload["empty"] = self.empty
        return payload


#: The honest empty state. Says what happened, why, and what follows — the
#: <EmptyHonest> contract, in the API rather than invented per surface.
NO_RESEARCH = {
    "happened": "No market read this week.",
    "why": "Each one is researched and checked by hand before it goes out.",
    "next": "The next read replaces this as soon as it is ready.",
}


def wire_for(batches: list[Batch], now: datetime) -> Wire:
    """The in-app market wire, or an honest nothing."""
    batch = live_batch(batches, now)
    if batch is None:
        return Wire(available=False, empty=NO_RESEARCH)
    return Wire(available=True, items=tuple(batch.items), batch_key=batch.batch_key)


def gist_for_email(batches: list[Batch], now: datetime, limit: int = 3) -> list[MarketItem] | None:
    """The same curation, shaped for an email.

    `None` — not `[]` — when there is nothing live. The caller must be unable
    to treat "no research" as "an email with an empty section", so the two are
    different types rather than different lengths.
    """
    batch = live_batch(batches, now)
    if batch is None:
        return None
    return list(batch.items[:limit])


__all__ = [
    "DEFAULT_LIFETIME",
    "NO_RESEARCH",
    "Batch",
    "BatchState",
    "MarketItem",
    "NotPublishable",
    "Wire",
    "assert_publishable",
    "gist_for_email",
    "live_batch",
    "wire_for",
]
