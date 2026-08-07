"""Live provider shape checks — OPT-IN, never run by default.

The `iat` clock-skew bug is the argument for these: some failures are
structurally invisible to fixtures, because a fixture is built from what we
believe the API returns. Only the real API can tell us we believed wrong.

**These are skipped unless `CARIGMA_LIVE_JOB_TESTS=1`.** V1 and V2 share one
JSearch key and live users have first claim on that quota, so CI must never run
them. Each test makes at most ONE call.

Run deliberately:
    CARIGMA_LIVE_JOB_TESTS=1 pytest tests/test_jobs_live.py -v
"""

from __future__ import annotations

import os

import pytest

from carigma_api.config import Settings
from carigma_api.services.jobs.providers import AdzunaProvider, JSearchProvider

LIVE = os.getenv("CARIGMA_LIVE_JOB_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not LIVE,
    reason="live provider calls spend shared quota; set CARIGMA_LIVE_JOB_TESTS=1 to run",
)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings()


def _assert_configured(condition: bool, name: str) -> None:
    """Precondition check (the standing pattern): a live test that silently
    skipped because a key was blank would look like a pass."""
    if not condition:
        pytest.fail(f"{name} is not configured — this test cannot verify anything")


def test_jsearch_live_shape_matches_the_normalizer(settings: Settings) -> None:
    _assert_configured(bool(settings.jsearch_api_key), "JSEARCH_API_KEY")

    provider = JSearchProvider(settings.jsearch_api_key, settings.jsearch_host)
    jobs = provider.search(role="Data Analyst", city="Bangalore", limit=10)

    # If the endpoint moved or `data.jobs` changed shape, this is zero.
    assert jobs, "no rows survived normalization — the response shape has changed"

    for job in jobs:
        # The fields the product cannot function without.
        assert job.title and job.company, "title/company mapping broke"
        assert job.apply_url.startswith("http"), "apply_url mapping broke"
        assert job.source == "jsearch"
        # The honesty rule, verified against real data.
        assert job.salary is None or isinstance(job.salary, str)


def test_jsearch_apply_links_are_postings_not_searches(settings: Settings) -> None:
    """V1 bug 1.2, checked against live data rather than a fixture."""
    _assert_configured(bool(settings.jsearch_api_key), "JSEARCH_API_KEY")

    jobs = JSearchProvider(settings.jsearch_api_key, settings.jsearch_host).search(
        role="Data Analyst", city="Bangalore", limit=10
    )

    for job in jobs:
        assert "/search" not in job.apply_url.lower(), f"search URL as apply link: {job.apply_url}"
        assert "q=" not in job.apply_url.lower(), f"query URL as apply link: {job.apply_url}"


def test_adzuna_live_shape_matches_the_normalizer(settings: Settings) -> None:
    _assert_configured(
        bool(settings.adzuna_app_id and settings.adzuna_app_key), "ADZUNA credentials"
    )

    jobs = AdzunaProvider(settings.adzuna_app_id, settings.adzuna_app_key).search(
        role="Data Analyst", city="Bangalore", limit=5
    )

    assert jobs, "no rows survived normalization — the response shape has changed"

    for job in jobs:
        assert job.title and job.company
        assert job.apply_url.startswith("http")
        assert job.source == "adzuna"


def test_adzuna_contract_time_is_sparse_not_absent(settings: Settings) -> None:
    """Adzuna returns `contract_time` on SOME rows and omits it on others.

    Recorded because a first probe looked at one row, found no contract field,
    and concluded it was missing entirely. Running across several rows showed
    it is simply sparse — the mapping works, the data is just incomplete.

    What matters for the product is the consequence: a job with no contract
    field gets `job_type = None`, and the UI must omit the chip rather than
    default to "Full-time". Inferring a term of employment is the same class of
    fabrication as inventing a salary.
    """
    _assert_configured(
        bool(settings.adzuna_app_id and settings.adzuna_app_key), "ADZUNA credentials"
    )

    jobs = AdzunaProvider(settings.adzuna_app_id, settings.adzuna_app_key).search(
        role="Data Analyst", city="Bangalore", limit=10
    )
    assert jobs, "no rows to inspect"

    # Whatever the mix, every value is either a real string or None — never a
    # substituted default.
    for job in jobs:
        assert job.job_type is None or (isinstance(job.job_type, str) and job.job_type.strip()), (
            f"job_type should be a real value or None, got {job.job_type!r}"
        )
