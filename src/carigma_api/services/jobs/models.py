"""The normalized job shape — the boundary every provider is flattened to.

**This is the contract that makes the jobs layer source-agnostic.** Everything
downstream of it — ranking, caching, dedupe, freshness, the honesty rules — sees
only `NormalizedJob` and never learns which provider produced it. Adding a
provider means writing an adapter and touching nothing else.

Two fields carry rules rather than data, and both exist because V1 shipped the
bug they prevent:

- `salary` is `None` unless the SOURCE published one. Adzuna returns *predicted*
  salaries; a prediction rendered as a fact is a lie about someone's pay. The
  UI hides the chip when this is None — it never guesses, and never falls back
  to "competitive".
- `apply_url` points at the ACTUAL POSTING. V1's bug 1.2 dressed a keyword
  search up as "Apply", which wasted users' time and eroded trust. Search links
  live in `search_links` and must be labelled as searches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ApplyOption:
    """One real place this job can be applied to."""

    publisher: str
    url: str


@dataclass(frozen=True)
class NormalizedJob:
    """A job as the rest of the system understands it."""

    # Identity
    source: str
    source_id: str
    title: str
    company: str
    location: str

    # The real posting. Never a search.
    apply_url: str
    apply_options: tuple[ApplyOption, ...] = ()

    # Facts the source published. None means "the source didn't say" — which is
    # information, and is shown as absence rather than filled with a guess.
    salary: str | None = None
    job_type: str | None = None
    posted_at: datetime | None = None
    description: str | None = None

    # Anything provider-specific a future adapter needs to keep. Deliberately
    # opaque: nothing in the ranking path may read it, or the layer stops being
    # source-agnostic.
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        """Cross-source identity: title + company + location.

        Two providers listing the same role must collapse to one card. Matching
        on source id cannot do that — the ids are per-provider by definition.
        """
        return "|".join(
            part.strip().lower()
            for part in (self.title, self.company, _canonical_location(self.location))
        )


def _canonical_location(location: str) -> str:
    """Normalise the location enough for dedupe to work across providers.

    Providers disagree cosmetically about the same place ("Bengaluru, KA, India"
    vs "Bangalore, India"), and a dedupe that misses those shows the user the
    same job twice.
    """
    text = (location or "").strip().lower()
    for noise in (", india", " india"):
        if text.endswith(noise):
            text = text[: -len(noise)]
    aliases = {
        "bengaluru": "bangalore",
        "gurgaon": "gurugram",
        "bombay": "mumbai",
        "new delhi": "delhi",
        "ncr": "delhi",
    }
    head = text.split(",")[0].strip()
    return aliases.get(head, head)
