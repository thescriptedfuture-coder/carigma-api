"""Drafting in the background — start, poll, and nothing lost to a dropped answer.

Measured in production (2026-10-20): drafting takes 48 s on median and 73 s at
worst — longer than the analyst runs whose answers Ravi's connection dropped
at 30–37 s (0.072). So drafting starts a run and the editor polls it.

And one hole closed on the way: the drafts were saved AFTER the charge, so a
failed save took 3 credits and kept nothing. They are now saved inside the
run, before it settles — a failed save fails the run, and a failed run
charges nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import tests.test_posts_routes as base
from carigma_api.routes import agents as agents_routes
from carigma_api.routes import posts as posts_routes
from carigma_api.services.run_store import SupabaseRunStore
from tests.conftest import USER_ID, auth, make_token
from tests.fake_supabase import FakeDB
from tests.test_posts_routes import _clean, drafting  # noqa: F401 — fixtures, by name


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDB]:
    """One run table for the route that starts a run and the one that reads it."""
    db = FakeDB()
    monkeypatch.setattr(posts_routes, "SupabaseRunStore", lambda client: SupabaseRunStore(db))
    monkeypatch.setattr(agents_routes, "_store", lambda request, settings: SupabaseRunStore(db))
    yield db


def _start(client: TestClient, day: str = "THU", **headers: str) -> Any:
    return client.post(
        f"/posts/week/{day}/regenerate",
        json={"background": True},
        headers={**auth(make_token()), **headers},
    )


def _poll(client: TestClient, run_id: str) -> dict[str, Any]:
    res = client.get(f"/agents/runs/{run_id}", headers=auth(make_token()))
    assert res.status_code == 200, res.text
    return dict(res.json())


def test_a_background_draft_answers_at_once_and_the_poll_finds_the_drafts(
    client: TestClient,
    drafting: Any,  # noqa: F811
    runs: FakeDB,
) -> None:
    credits, _writer = drafting
    base.seed_week()

    res = _start(client)

    assert res.status_code == 202
    started = res.json()
    assert started["status"] == "running"
    # What the run is for — so THIS slot's editor can find it again.
    assert started["subject"] == {"day": "THU", "week_start": base.monday().isoformat()}

    run = _poll(client, started["run_id"])
    assert run["status"] == "succeeded"
    assert [d["note"] for d in run["result"]["slot"]["drafts"]] == ["Story", "Practice", "View"]
    assert run["credits"]["charged"] == 3
    assert credits.charged == [("Regenerate one post", -3)]
    stored = base.STORE.load(USER_ID, base.monday())
    assert stored is not None
    assert len(next(s for s in stored.slots if s.day == "THU").drafts) == 3


def test_drafts_that_cannot_be_kept_are_not_charged_for(
    client: TestClient,
    drafting: Any,  # noqa: F811
    runs: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hole: saved after the charge, a failed save took 3 credits and kept
    nothing. Saved inside the run, it fails the run, and a failed run is free."""
    credits, _writer = drafting
    base.seed_week()

    def refuse(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("content_loop is down")

    monkeypatch.setattr(base.STORE, "save_slot", refuse)

    run = _poll(client, _start(client).json()["run_id"])

    assert run["status"] == "failed"
    assert run["credits"]["charged"] == 0
    assert credits.charged == []


def test_held_open_a_failed_save_is_also_free_now(
    client: TestClient,
    drafting: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credits, _writer = drafting
    base.seed_week()

    def refuse(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("content_loop is down")

    monkeypatch.setattr(base.STORE, "save_slot", refuse)

    res = client.post("/posts/week/THU/regenerate", headers=auth(make_token()))

    assert res.status_code == 503
    assert "weren't charged" in res.json()["detail"]["message"]
    assert credits.charged == []


def test_a_background_draft_with_nothing_honest_is_empty_and_free(
    client: TestClient,
    drafting: Any,  # noqa: F811
    runs: FakeDB,
) -> None:
    credits, writer = drafting
    base.seed_week()
    # The same two the held-open test uses: an invented figure, a blank to fill.
    writer.reply = {
        "drafts": [
            {"angle": "Story", "body": base._post("How we grew revenue", "It rose 63%.")},
            {"angle": "View", "body": base._post("When I joined [Company]")},
        ]
    }

    run = _poll(client, _start(client).json()["run_id"])

    assert run["status"] == "empty"
    assert credits.charged == []
    stored = base.STORE.load(USER_ID, base.monday())
    thursday = next((s for s in (stored.slots if stored else ()) if s.day == "THU"), None)
    assert thursday is None or not thursday.drafts


def test_the_editor_finds_its_own_run_and_not_another_days(
    client: TestClient,
    drafting: Any,  # noqa: F811
    runs: FakeDB,
) -> None:
    """Monday's editor must never pick up Thursday's draft."""
    from datetime import UTC, datetime

    from carigma_api.services.runs import RunStatus, new_run

    run = new_run(USER_ID, "content")
    run.status = RunStatus.RUNNING
    run.started_at = datetime.now(UTC)
    run.subject = {"day": "THU", "week_start": base.monday().isoformat()}
    SupabaseRunStore(runs).save(run)

    def active(day: str) -> Any:
        return client.get(
            "/agents/runs/active",
            params={"agent": "content", "day": day, "week_start": base.monday().isoformat()},
            headers=auth(make_token()),
        ).json()["run"]

    assert active("THU")["run_id"] == run.id
    assert active("MON") is None


def test_a_background_start_on_a_published_slot_is_refused_before_any_run(
    client: TestClient,
    drafting: Any,  # noqa: F811
    runs: FakeDB,
) -> None:
    from carigma_api.services.posts import Slot, mark_published

    thursday = base.monday() + base.timedelta(days=3)
    base.seed_with(
        mark_published(Slot("THU", thursday), at=f"{thursday.isoformat()}T09:00:00+00:00")
    )

    res = _start(client)

    assert res.status_code == 409
    assert runs.rows("agent_runs") == []
