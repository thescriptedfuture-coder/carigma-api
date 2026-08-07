"""Concrete providers. Each one is the ONLY place that knows its own API shape.

Ported from V1's `jobs_jsearch.py` and `jobs_api.py`, restructured to satisfy
`JobProvider`. The V1 hard-won details are preserved verbatim, because each was
paid for with a production bug:

- JSearch's endpoint is **`/search-v2`**. The old `/search` returns 404 even
  with a valid key, and jobs are nested under `data.jobs`.
- Adzuna's **predicted salaries are dropped, not shown.** `salary_is_predicted`
  means the number is a model's guess, and a guess about someone's pay
  presented as a fact is the worst kind of fabrication this product could ship.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from carigma_api.services.jobs.adapter import ProviderUnavailable, QuotaExhausted
from carigma_api.services.jobs.models import ApplyOption, NormalizedJob

logger = logging.getLogger(__name__)

_TIMEOUT = 15


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, int | float):
            return datetime.fromtimestamp(float(value), tz=UTC)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None


class JSearchProvider:
    """JSearch via RapidAPI (or the OpenWeb Ninja gateway)."""

    name = "jsearch"

    def __init__(self, api_key: str, host: str = "jsearch.p.rapidapi.com") -> None:
        self._key = api_key
        self._host = host

    def available(self) -> bool:
        return bool(self._key)

    def search(self, *, role: str, city: str, limit: int) -> list[NormalizedJob]:
        query = f"{role} in {city}" if city else role
        # /search-v2 — the old /search 404s. See the module docstring.
        url = f"https://{self._host}/search-v2"
        headers = {"x-rapidapi-key": self._key, "x-rapidapi-host": self._host}

        try:
            res = httpx.get(
                url,
                headers=headers,
                params={"query": query, "date_posted": "week", "country": "in"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"jsearch unreachable: {exc}") from exc

        if res.status_code == 429:
            raise QuotaExhausted("jsearch rate limit")
        if res.status_code >= 400:
            raise ProviderUnavailable(f"jsearch HTTP {res.status_code}")

        payload = res.json()
        # Jobs are nested under data.jobs, not data.
        raw = payload.get("data", {})
        items = raw.get("jobs", []) if isinstance(raw, dict) else raw
        return [j for j in (self._normalize(item) for item in items[:limit]) if j]

    def _normalize(self, raw: dict[str, Any]) -> NormalizedJob | None:
        apply_url = raw.get("job_apply_link") or raw.get("job_google_link")
        title = raw.get("job_title")
        company = raw.get("employer_name")
        # No real apply link means we cannot honour "links go to the actual
        # posting" — so we drop the row rather than substitute a search.
        if not (apply_url and title and company):
            return None

        options = tuple(
            ApplyOption(
                publisher=str(o.get("publisher", "")),
                url=str(o.get("apply_link", "")),
                is_direct=bool(o.get("is_direct")),
            )
            for o in (raw.get("apply_options") or [])
            if o.get("apply_link")
        )

        return NormalizedJob(
            source=self.name,
            source_id=str(raw.get("job_id", apply_url)),
            title=str(title),
            company=str(company),
            location=", ".join(p for p in (raw.get("job_city"), raw.get("job_country")) if p),
            apply_url=str(apply_url),
            apply_options=options,
            salary=_jsearch_salary(raw),
            # Live check: 10/10 results routed through aggregators
            # (SimplyHired, Shine, apna.co, BeBee). Surfacing the publisher is
            # how "Apply" avoids implying the employer's own site.
            publisher=raw.get("job_publisher"),
            job_type=raw.get("job_employment_type"),
            posted_at=_parse_ts(raw.get("job_posted_at_timestamp")),
            description=raw.get("job_description"),
        )


def _jsearch_salary(raw: dict[str, Any]) -> str | None:
    """Only when the source published one."""
    lo, hi = raw.get("job_min_salary"), raw.get("job_max_salary")
    if not lo and not hi:
        return None
    currency = raw.get("job_salary_currency") or "INR"
    period = (raw.get("job_salary_period") or "YEAR").lower()
    if lo and hi:
        return f"{currency} {int(lo):,} – {int(hi):,} per {period}"
    value = int(lo or hi or 0)
    return f"{currency} {value:,} per {period}"


class AdzunaProvider:
    """Adzuna (India). V1's fallback, kept so the interface is proven twice."""

    name = "adzuna"
    _BASE = "https://api.adzuna.com/v1/api/jobs/in/search/1"

    def __init__(self, app_id: str, app_key: str) -> None:
        self._id = app_id
        self._key = app_key

    def available(self) -> bool:
        return bool(self._id and self._key)

    def search(self, *, role: str, city: str, limit: int) -> list[NormalizedJob]:
        try:
            res = httpx.get(
                self._BASE,
                params={
                    "app_id": self._id,
                    "app_key": self._key,
                    "what": role,
                    "where": city or "India",
                    "results_per_page": limit,
                    "content-type": "application/json",
                },
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"adzuna unreachable: {exc}") from exc

        if res.status_code == 429:
            raise QuotaExhausted("adzuna rate limit")
        if res.status_code >= 400:
            raise ProviderUnavailable(f"adzuna HTTP {res.status_code}")

        return [j for j in (self._normalize(item) for item in res.json().get("results", [])) if j]

    def _normalize(self, raw: dict[str, Any]) -> NormalizedJob | None:
        apply_url = raw.get("redirect_url")
        title = raw.get("title")
        company = (raw.get("company") or {}).get("display_name")
        if not (apply_url and title and company):
            return None

        return NormalizedJob(
            source=self.name,
            source_id=str(raw.get("id", apply_url)),
            title=str(title),
            company=str(company),
            location=str((raw.get("location") or {}).get("display_name", "")),
            apply_url=str(apply_url),
            salary=_adzuna_salary(raw),
            # Adzuna's own redirect page, not the employer's site — so it is
            # named as the publisher rather than left to look like the company.
            publisher="Adzuna",
            # SPARSE in the live India response: present on some rows, absent
            # on others. (A first probe read one row, saw no contract field and
            # wrongly concluded it was never returned — corrected by checking
            # across rows.) When absent this stays None and the UI omits the
            # chip; defaulting to "Full-time" would invent a term of employment.
            #
            # Deliberately NOT falling back to `contract_type`. That field
            # answers a different question — permanent vs contract, not
            # full-time vs part-time — and folding the two together would
            # render "permanent" where the UI promises hours.
            job_type=raw.get("contract_time"),
            posted_at=_parse_ts(raw.get("created")),
            description=raw.get("description"),
        )


def _adzuna_salary(raw: dict[str, Any]) -> str | None:
    """Adzuna PREDICTS salaries. A predicted number is not a published one.

    `salary_is_predicted` is "1" when Adzuna inferred it from similar listings.
    Showing that as the job's pay would be inventing a fact about someone's
    livelihood, so it is dropped entirely.
    """
    if str(raw.get("salary_is_predicted", "0")) == "1":
        return None
    lo, hi = raw.get("salary_min"), raw.get("salary_max")
    if not lo and not hi:
        return None
    if lo and hi:
        return f"INR {int(lo):,} – {int(hi):,} per year"
    return f"INR {int(lo or hi or 0):,} per year"
