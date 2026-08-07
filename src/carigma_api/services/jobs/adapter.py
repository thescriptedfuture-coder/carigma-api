"""The provider interface, and the source-agnostic machinery around it.

The whole point: **adding a provider means writing an adapter and touching
nothing else.** Ranking, caching, dedupe and the honesty rules live here and
know only `NormalizedJob`.

Modelled against TWO providers from the start (JSearch and Adzuna), because an
interface derived from a single implementation is just that implementation
wearing a hat. Public ATS endpoints (Greenhouse, Lever, Ashby) are the likely
third — free, legal, and the most direct apply links available. Nothing here
should need to change to accept one.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from carigma_api.services.jobs.models import NormalizedJob

logger = logging.getLogger(__name__)


class ProviderUnavailable(Exception):
    """The provider could not be reached, or refused us.

    Distinct from "returned nothing": an outage is not evidence that no jobs
    exist, so the orchestrator must not report it as an honest-empty result.
    """


class QuotaExhausted(ProviderUnavailable):
    """We hit this provider's cap. Stale cache is better than a lie."""


@runtime_checkable
class JobProvider(Protocol):
    """What a job source must do. Nothing more is permitted to leak upward."""

    name: str

    def available(self) -> bool:
        """Is this provider configured? An unconfigured provider is skipped
        silently; a configured one that fails is an error worth reporting."""
        ...

    def search(self, *, role: str, city: str, limit: int) -> list[NormalizedJob]:
        """Return REAL postings, already normalized.

        Must raise ProviderUnavailable rather than returning [] on failure —
        the difference between "nothing matched" and "we couldn't look" is the
        difference between an honest empty state and a silent lie.
        """
        ...


def dedupe_across_sources(
    jobs: list[NormalizedJob],
    *,
    preferred_order: tuple[str, ...] = (),
) -> list[NormalizedJob]:
    """Collapse the same role listed by several providers into one card.

    `preferred_order` decides which copy survives — usually the provider with
    the better apply link. A job present in two sources is one job; showing it
    twice makes a 12-match day look like 20 and is quietly dishonest about how
    much the scan found.
    """
    rank = {name: i for i, name in enumerate(preferred_order)}
    best: dict[str, NormalizedJob] = {}

    for job in jobs:
        key = job.dedupe_key
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = job
            continue
        # Lower rank wins; unknown providers sort last.
        if rank.get(job.source, len(rank)) < rank.get(incumbent.source, len(rank)):
            best[key] = job

    return list(best.values())


class JobsGateway:
    """Fans a query out across providers and returns one clean list.

    Deliberately knows nothing about *which* providers it holds. The fallback
    behaviour is a property of the list order, not of any provider's identity.
    """

    def __init__(self, providers: list[JobProvider], *, min_results: int = 5) -> None:
        self._providers = providers
        # Below this, try the next provider as a top-up rather than settling.
        self._min_results = min_results

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self._providers)

    def search(
        self, *, role: str, city: str, limit: int = 20
    ) -> tuple[list[NormalizedJob], list[str]]:
        """Returns (jobs, notes).

        `notes` records what happened per provider so the caller can be honest
        with the user — "Adzuna was unreachable" is a fact worth surfacing, and
        is very different from "no jobs matched".
        """
        collected: list[NormalizedJob] = []
        notes: list[str] = []
        configured = [p for p in self._providers if p.available()]

        if not configured:
            notes.append("No job source is configured.")
            return [], notes

        for provider in configured:
            if len(collected) >= self._min_results:
                break
            try:
                found = provider.search(role=role, city=city, limit=limit)
                collected.extend(found)
                notes.append(f"{provider.name}: {len(found)}")
            except QuotaExhausted:
                logger.warning("Provider %s quota exhausted", provider.name)
                notes.append(f"{provider.name}: quota reached")
            except ProviderUnavailable as exc:
                logger.warning("Provider %s unavailable: %s", provider.name, exc)
                notes.append(f"{provider.name}: unavailable")
            except Exception:  # noqa: BLE001 — one bad provider must not kill the scan
                logger.exception("Provider %s raised unexpectedly", provider.name)
                notes.append(f"{provider.name}: failed")

        deduped = dedupe_across_sources(collected, preferred_order=self.provider_names)
        return deduped, notes
