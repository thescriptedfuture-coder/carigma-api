"""The jobs layer: source-agnostic by construction, honest by construction.

Two things are being proven here.

1. **A third provider needs an adapter and nothing else.** `GreenhouseStub`
   below is written the way a real Greenhouse adapter would be, and is dropped
   into the gateway with no change to ranking, dedupe or the honesty rules. If
   this test ever needs a change elsewhere to pass, the layer has stopped being
   source-agnostic.

2. **The honesty rules survive normalization.** Salary appears only when the
   source published it; apply links point at real postings. Both were V1
   production bugs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from carigma_api.services.jobs import (
    JobsGateway,
    NormalizedJob,
    ProviderUnavailable,
    QuotaExhausted,
    dedupe_across_sources,
)
from carigma_api.services.jobs.providers import _adzuna_salary, _jsearch_salary


def job(**kw: object) -> NormalizedJob:
    base = {
        "source": "test",
        "source_id": "1",
        "title": "Data Analyst",
        "company": "Zomato",
        "location": "Gurugram, India",
        "apply_url": "https://example.com/jobs/1",
    }
    base.update(kw)
    return NormalizedJob(**base)  # type: ignore[arg-type]


class StubProvider:
    """A provider that returns whatever it is told to."""

    def __init__(
        self,
        name: str,
        jobs: list[NormalizedJob] | None = None,
        error: Exception | None = None,
        configured: bool = True,
    ) -> None:
        self.name = name
        self._jobs = jobs or []
        self._error = error
        self._configured = configured
        self.calls = 0

    def available(self) -> bool:
        return self._configured

    def search(self, *, role: str, city: str, limit: int) -> list[NormalizedJob]:
        self.calls += 1
        if self._error:
            raise self._error
        return self._jobs


class GreenhouseStub:
    """Written as a REAL third adapter would be — the point of the exercise.

    Greenhouse is a public ATS: free, legal, and the most direct apply links
    available. If adding this required changing anything outside `providers`,
    the interface would have failed.
    """

    name = "greenhouse"

    def available(self) -> bool:
        return True

    def search(self, *, role: str, city: str, limit: int) -> list[NormalizedJob]:
        return [
            NormalizedJob(
                source=self.name,
                source_id="gh-77",
                title=role,
                company="Postman",
                location=city or "Bangalore",
                apply_url="https://boards.greenhouse.io/postman/jobs/77",
                # A public ATS board usually publishes no salary. Correct
                # behaviour is to say nothing, not to guess.
                salary=None,
            )
        ]


# ── The interface takes a new provider without rework ──────────────────────


def test_a_third_provider_needs_only_an_adapter() -> None:
    gateway = JobsGateway([GreenhouseStub()], min_results=5)
    jobs, notes = gateway.search(role="Data Analyst", city="Bangalore")

    assert len(jobs) == 1
    assert jobs[0].source == "greenhouse"
    assert jobs[0].apply_url.startswith("https://boards.greenhouse.io/")
    assert "greenhouse: 1" in notes


def test_providers_are_interchangeable_in_any_order() -> None:
    a = StubProvider("a", [job(source="a", source_id="a1", title="BI Analyst")])
    b = StubProvider("b", [job(source="b", source_id="b1", title="Product Analyst")])

    forward, _ = JobsGateway([a, b], min_results=99).search(role="x", city="y")
    backward, _ = JobsGateway([b, a], min_results=99).search(role="x", city="y")

    assert {j.title for j in forward} == {j.title for j in backward}


# ── Fallback behaviour is a property of order, not of identity ─────────────


def test_second_provider_tops_up_when_the_first_is_thin() -> None:
    thin = StubProvider("thin", [job(source="thin", source_id="t1")])
    deep = StubProvider(
        "deep", [job(source="deep", source_id=f"d{i}", title=f"Role {i}") for i in range(6)]
    )

    jobs, _ = JobsGateway([thin, deep], min_results=5).search(role="x", city="y")

    assert deep.calls == 1, "the second provider should have been consulted"
    assert len(jobs) > 1


def test_a_full_first_provider_stops_the_fan_out() -> None:
    """Not calling a provider we don't need protects a shared quota."""
    deep = StubProvider(
        "deep", [job(source="deep", source_id=f"d{i}", title=f"Role {i}") for i in range(8)]
    )
    spare = StubProvider("spare", [job(source="spare", source_id="s1")])

    JobsGateway([deep, spare], min_results=5).search(role="x", city="y")

    assert spare.calls == 0


# ── One bad provider must not take the scan down ───────────────────────────


@pytest.mark.parametrize(
    "error",
    [ProviderUnavailable("down"), QuotaExhausted("cap"), RuntimeError("nobody predicted this")],
    ids=["unavailable", "quota", "unexpected"],
)
def test_a_failing_provider_does_not_kill_the_scan(error: Exception) -> None:
    broken = StubProvider("broken", error=error)
    working = StubProvider("working", [job(source="working", source_id="w1")])

    jobs, notes = JobsGateway([broken, working], min_results=5).search(role="x", city="y")

    assert len(jobs) == 1
    assert any("broken" in n for n in notes), "the failure should be reported, not hidden"


def test_unconfigured_providers_are_skipped_silently() -> None:
    off = StubProvider("off", configured=False)
    on = StubProvider("on", [job(source="on", source_id="o1")])

    jobs, notes = JobsGateway([off, on], min_results=5).search(role="x", city="y")

    assert off.calls == 0
    assert len(jobs) == 1
    assert not any("off" in n for n in notes)


def test_no_providers_configured_says_so() -> None:
    """Distinct from "no jobs matched" — the user deserves the difference."""
    jobs, notes = JobsGateway([StubProvider("off", configured=False)]).search(role="x", city="y")

    assert jobs == []
    assert any("configured" in n for n in notes)


# ── Cross-source dedupe ────────────────────────────────────────────────────


def test_same_job_from_two_sources_collapses_to_one() -> None:
    """Showing it twice makes a 12-match day look like 20."""
    deduped = dedupe_across_sources(
        [
            job(source="jsearch", source_id="j1"),
            job(source="adzuna", source_id="a1"),
        ],
        preferred_order=("jsearch", "adzuna"),
    )

    assert len(deduped) == 1
    assert deduped[0].source == "jsearch", "the preferred source should win"


def test_dedupe_survives_cosmetic_location_differences() -> None:
    """Providers disagree about the same city; a miss shows a duplicate."""
    deduped = dedupe_across_sources(
        [
            job(source="a", location="Bengaluru, India"),
            job(source="b", location="Bangalore"),
        ]
    )
    assert len(deduped) == 1


def test_dedupe_keeps_genuinely_different_jobs() -> None:
    deduped = dedupe_across_sources(
        [
            job(title="Data Analyst"),
            job(title="Senior Data Analyst"),
            job(company="Swiggy"),
        ]
    )
    assert len(deduped) == 3


# ── Honesty: salary ────────────────────────────────────────────────────────


def test_adzuna_predicted_salary_is_dropped() -> None:
    """A model's guess about someone's pay is not a fact about their pay."""
    assert (
        _adzuna_salary({"salary_is_predicted": "1", "salary_min": 1200000, "salary_max": 1800000})
        is None
    )


def test_adzuna_published_salary_is_kept() -> None:
    out = _adzuna_salary({"salary_is_predicted": "0", "salary_min": 1200000, "salary_max": 1800000})
    assert out is not None
    assert "12,00,000" in out or "1,200,000" in out


def test_missing_salary_is_none_not_a_placeholder() -> None:
    """None means the chip is hidden. A string like 'Competitive' would be an
    invention, and would render as though the source had said something."""
    assert _jsearch_salary({}) is None
    assert _adzuna_salary({}) is None


def test_normalized_job_defaults_salary_to_none() -> None:
    assert job().salary is None


# ── Honesty: apply links ───────────────────────────────────────────────────


def test_a_row_without_a_real_apply_link_is_dropped() -> None:
    """V1 bug 1.2: a keyword search dressed up as "Apply". Rather than
    substitute a search, the row does not ship."""
    from carigma_api.services.jobs.providers import JSearchProvider

    provider = JSearchProvider("key")
    assert provider._normalize({"job_title": "Data Analyst", "employer_name": "Zomato"}) is None


def test_apply_options_carry_real_publisher_links() -> None:
    from carigma_api.services.jobs.providers import JSearchProvider

    normalized = JSearchProvider("key")._normalize(
        {
            "job_id": "1",
            "job_title": "Data Analyst",
            "employer_name": "Zomato",
            "job_apply_link": "https://real.example/apply/1",
            "apply_options": [
                {"publisher": "LinkedIn", "apply_link": "https://linkedin.example/1"},
                {"publisher": "Broken", "apply_link": ""},
            ],
        }
    )

    assert normalized is not None
    assert normalized.apply_url == "https://real.example/apply/1"
    # The option with no link is dropped rather than pointed somewhere vague.
    assert len(normalized.apply_options) == 1
    assert normalized.apply_options[0].publisher == "LinkedIn"


# ── Who is on the other end of "Apply" ─────────────────────────────────────
# The live check found job_apply_is_direct false for 10/10 real results — every
# link went to an aggregator. Real postings, so §25 holds, but the user is
# entitled to know they are not reaching the employer.


def test_the_host_of_the_posting_is_named() -> None:
    from carigma_api.services.jobs.providers import JSearchProvider

    normalized = JSearchProvider("key")._normalize(
        {
            "job_id": "1",
            "job_title": "Data Analyst",
            "employer_name": "Zomato",
            "job_apply_link": "https://simplyhired.example/apply/1",
            "job_publisher": "SimplyHired",
        }
    )
    assert normalized is not None
    assert normalized.publisher == "SimplyHired"


def test_directness_is_carried_not_assumed() -> None:
    """An aggregator link must not read as a direct application, and a direct
    one must not be understated."""
    from carigma_api.services.jobs.providers import JSearchProvider

    normalized = JSearchProvider("key")._normalize(
        {
            "job_id": "1",
            "job_title": "Data Analyst",
            "employer_name": "Zomato",
            "job_apply_link": "https://real.example/apply/1",
            "apply_options": [
                {
                    "publisher": "Zomato Careers",
                    "apply_link": "https://zomato.example/jobs/1",
                    "is_direct": True,
                },
                {"publisher": "SimplyHired", "apply_link": "https://sh.example/1"},
            ],
        }
    )
    assert normalized is not None
    assert normalized.apply_options[0].is_direct is True
    # Absent means false — never optimistically true.
    assert normalized.apply_options[1].is_direct is False


def test_adzuna_names_itself_rather_than_posing_as_the_employer() -> None:
    """redirect_url is Adzuna's own interstitial, not the company's site."""
    normalized = _adzuna_row(redirect_url="https://adzuna.in/details/123")
    assert normalized is not None
    assert normalized.publisher == "Adzuna"


def test_absent_contract_time_stays_none_rather_than_full_time() -> None:
    """Adzuna India omits `contract_time` on many rows. Inferring a term of
    employment is the same class of fabrication as inventing a salary."""
    assert _adzuna_row().job_type is None
    assert _adzuna_row(contract_time="part_time").job_type == "part_time"


def test_contract_type_is_not_substituted_for_contract_time() -> None:
    """`contract_type` is permanent-vs-contract; `contract_time` is full-vs-part
    time. Folding them together would render "permanent" where the UI promises
    hours."""
    assert _adzuna_row(contract_type="permanent").job_type is None


def _adzuna_row(**overrides: object) -> NormalizedJob:
    from carigma_api.services.jobs.providers import AdzunaProvider

    raw: dict[str, object] = {
        "id": "1",
        "title": "Data Analyst",
        "company": {"display_name": "Zomato"},
        "redirect_url": "https://adzuna.in/details/1",
        **overrides,
    }
    normalized = AdzunaProvider("id", "key")._normalize(raw)
    assert normalized is not None
    return normalized


# ── The shape stays opaque to the ranking path ─────────────────────────────


def test_provider_specific_data_is_quarantined_in_extra() -> None:
    """If ranking could read provider fields, the layer would silently become
    source-dependent again."""
    fields = set(NormalizedJob.__dataclass_fields__)
    assert "extra" in fields
    # Nothing provider-named leaks into the shape itself.
    assert not any(name in fields for name in ("jsearch_id", "adzuna_id", "redirect_url"))


def test_posted_at_is_a_datetime_or_none_never_a_string() -> None:
    """Freshness maths must not depend on each provider's date format."""
    assert job(posted_at=datetime.now(UTC)).posted_at is not None
    assert job().posted_at is None
