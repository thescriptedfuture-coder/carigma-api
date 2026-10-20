"""Scans and tailored CVs in the background (S12).

Measured in production (2026-10-20): a scan takes ~42 s and a CV ~26 s — runs
whose answers a network can drop on the way back, as it dropped the analyst's
at 30–37 s (0.072). Both now start a run and the page polls it.

And one hole closed on the way: the CV was saved with an UPDATE whose answer
nobody read. RLS refuses an update by touching zero rows, without an error,
so a refused save "succeeded" and 15 credits bought a CV that was never kept.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import agents as agents_routes
from carigma_api.routes import jobs as routes
from carigma_api.services.run_store import SupabaseRunStore
from tests.conftest import auth, make_token
from tests.fake_supabase import FakeDB as RunsDB
from tests.test_jobs_routes import (  # noqa: F401 — fixtures, by name
    _FakeCredits,
    scanning,
    tailoring,
    wired,
)


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[RunsDB]:
    """One run table for the routes that start runs and the one that reads them."""
    db = RunsDB()
    monkeypatch.setattr(routes, "SupabaseRunStore", lambda client: SupabaseRunStore(db))
    monkeypatch.setattr(agents_routes, "_store", lambda request, settings: SupabaseRunStore(db))
    yield db


def _poll(client: TestClient, run_id: str) -> dict[str, Any]:
    res = client.get(f"/agents/runs/{run_id}", headers=auth(make_token()))
    assert res.status_code == 200, res.text
    return dict(res.json())


def _start(client: TestClient, path: str) -> Any:
    return client.post(path, json={"background": True}, headers=auth(make_token()))


# ── The scan ───────────────────────────────────────────────────────────────


def test_a_background_scan_answers_at_once_and_the_poll_finds_what_it_saved(
    scanning: Any,  # noqa: F811
    runs: RunsDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from carigma_api.services.jobs.models import NormalizedJob

    client, db, gateway = scanning
    credits = _FakeCredits()
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: credits)
    gateway.found = [
        NormalizedJob(
            source="jsearch",
            source_id="1",
            title="Data Analyst",
            company="Zomato",
            location="Gurugram, India",
            apply_url="https://example.com/1",
            description="SQL and Power BI.",
        )
    ]

    res = _start(client, "/jobs/scan")

    assert res.status_code == 202
    started = res.json()
    # What was searched for — kept even when a scan that matches nothing has
    # to return `{}` to be free.
    assert started["subject"] == {"role": "Data Analyst", "city": "Delhi"}

    run = _poll(client, started["run_id"])
    assert run["status"] == "succeeded"
    assert run["result"]["new"] == 1
    assert run["result"]["new_jobs"][0]["title"] == "Data Analyst"
    assert credits.charged == [("Career Scout run", -15)]


def test_a_background_scan_that_matched_nothing_is_empty_free_and_says_what_it_sought(
    scanning: Any,  # noqa: F811
    runs: RunsDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _db, gateway = scanning
    credits = _FakeCredits()
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: credits)
    gateway.found, gateway.notes = [], ["jsearch: 0"]

    run = _poll(client, _start(client, "/jobs/scan").json()["run_id"])

    assert run["status"] == "empty"
    assert run["subject"] == {"role": "Data Analyst", "city": "Delhi"}
    assert credits.charged == []


def test_the_jobs_page_finds_a_scan_still_going(
    scanning: Any,  # noqa: F811
    runs: RunsDB,
) -> None:
    from datetime import UTC, datetime

    from carigma_api.services.runs import RunStatus, new_run

    client, _db, _gateway = scanning
    run = new_run("11111111-1111-1111-1111-111111111111", "jobs")
    run.status, run.started_at = RunStatus.RUNNING, datetime.now(UTC)
    SupabaseRunStore(runs).save(run)

    found = client.get(
        "/agents/runs/active", params={"agent": "jobs"}, headers=auth(make_token())
    ).json()["run"]

    assert found is not None and found["run_id"] == run.id


# ── The tailored CV ────────────────────────────────────────────────────────


def test_a_background_cv_answers_at_once_and_the_poll_finds_the_kit(
    tailoring: Any,  # noqa: F811
    runs: RunsDB,
) -> None:
    client, db, credits, _calls, _state = tailoring

    res = _start(client, "/jobs/1/apply-kit")

    assert res.status_code == 202
    assert res.json()["subject"] == {"job_id": "1"}
    run = _poll(client, res.json()["run_id"])
    assert run["status"] == "succeeded"
    assert run["result"]["cv"]["name"] == "Ravi Kumar"
    assert credits.charged == [("Tailored CV", -15)]
    stored = next(r for r in db.rows if r["id"] == 1)
    assert stored["apply_kit"]["cv"]["summary"]


def test_a_cv_the_database_refused_to_keep_is_not_charged(
    tailoring: Any,  # noqa: F811
    runs: RunsDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RLS refuses an UPDATE by touching zero rows. Unchecked, that "saved"
    and charged 15 credits for a CV nobody could open."""
    client, db, credits, _calls, _state = tailoring
    real_table = db.table

    class ZeroRowUpdate:
        def __init__(self, table: Any) -> None:
            self._table = table

        def update(self, _payload: dict[str, Any]) -> Any:
            class Refused:
                def eq(self, *_a: Any) -> Any:
                    return self

                def execute(self) -> Any:
                    return type("Res", (), {"data": []})()

            return Refused()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._table, name)

    monkeypatch.setattr(db, "table", lambda name: ZeroRowUpdate(real_table(name)))

    run = _poll(client, _start(client, "/jobs/1/apply-kit").json()["run_id"])

    assert run["status"] == "failed"
    assert credits.charged == []


def test_a_kit_already_built_is_refused_before_any_run(
    tailoring: Any,  # noqa: F811
    runs: RunsDB,
) -> None:
    client, _db, credits, _calls, _state = tailoring

    res = _start(client, "/jobs/2/apply-kit")

    assert res.status_code == 409
    assert runs.rows("agent_runs") == []
    assert credits.charged == []
