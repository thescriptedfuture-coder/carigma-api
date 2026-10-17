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

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ApplyOption:
    """One real place this job can be applied to."""

    publisher: str
    url: str
    # Whether the link reaches the EMPLOYER directly rather than an aggregator.
    # Added after the live shape check: JSearch reported `job_apply_is_direct`
    # false for 10/10 real results, routing through SimplyHired, Shine, apna.co
    # and BeBee. Those are real postings — the honesty rule holds — but "the
    # real posting, on a job board" is not the same as "the employer's own
    # posting", and the user is entitled to know which they are about to open.
    is_direct: bool = False


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
    posted_at: datetime | None = None
    #: When the SOURCE says the posting closes. Usually None: most boards do
    #: not publish one, and absence is not "open forever" — `feed.is_current`
    #: falls back to when the posting was last seen alive.
    expires_at: datetime | None = None
    description: str | None = None

    # Who is hosting the posting we link to (SimplyHired, Shine, the employer).
    # Shown to the user so "Apply" never implies the employer's own site when
    # it is an aggregator.
    publisher: str | None = None

    # NOTE: frequently None. Adzuna India returns `contract_time` on some rows
    # and omits it on others, so the UI must treat a missing job type as the
    # normal case and simply omit the chip. Left as None rather than inferred —
    # guessing "Full-time" would be inventing a term of employment.
    job_type: str | None = None

    # Anything provider-specific a future adapter needs to keep. Deliberately
    # opaque: nothing in the ranking path may read it, or the layer stops being
    # source-agnostic.
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        """Identity across rescans and across providers: V1's key, exactly.

        `sha1("company title")[:16]` after folding everything that is not a
        letter or digit to a space — `resumeiq/jobs_feed.py:dedup_key`.

        ## Why V1's, and not a better one

        V2 shipped its own readable `title|company|location`. Every row V1 had
        written — 93 of them, with users' dismissals and paid apply kits on
        them — carried the hash, so no V2 scan could ever recognise one. The
        first real scan brought back a posting Ravi had dismissed in July, as
        new, with a fresh "New today". The key is the join between a scan and
        a decision the user already made; a second spelling of it severs that
        join silently.

        Location is left out, as V1 leaves it out. JSearch reported one ETS
        posting as "New Delhi, IN" at 04:41 and "IN" at 04:47, and the
        location-bearing key filed it twice. One title at one company in two
        cities is, in practice, one requisition.
        """
        base = re.sub(r"[^a-z0-9]+", " ", f"{self.company} {self.title}".lower()).strip()
        return hashlib.sha1(base.encode(), usedforsecurity=False).hexdigest()[:16]
