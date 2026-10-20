"""The run protocol, end to end: start, poll, and the retry that must not pay twice.

## What happened (UAT, 2026-10-19)

Three Profile Analyst runs succeeded in production, were charged 10 credits
each, and kept their reading — and the page received none of the answers.
Each was one request held open for 30–40 seconds. Signal now starts the run,
gets its id back at once, and polls `GET /agents/runs/{id}`.

## What these tests hold

- **A retry while the run is in flight is the same run, charged once.** The
  run used to be saved only when it FINISHED, so an Idempotency-Key retry
  sent mid-run found nothing and started a second, charged run.
- **Two requests racing under one key** — both looking before either has
  written — still make one run: the table's unique index decides, through the
  real `SupabaseRunStore`, against a fake that models that index.
- **Another user's run is a 404**, like an id never issued.
- **Every terminal state reads back** with its charge, and a failure reads
  back as plain language, never the exception the row keeps.

Supabase and Anthropic are faked at the boundary; the route, the run
machinery, the store and the credit arithmetic are the real code.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.services.run_store import SupabaseRunStore
from tests.conftest import USER_ID, auth, make_token
from tests.fake_supabase import FakeDB
from tests.test_score_endpoint import ANALYST_PAYLOAD, FakeCredits, FakeProfiles, FakeScores

OTHER_USER = "22222222-2222-2222-2222-222222222222"


class Wired:
    """One table for every request, as production has — each request builds
    its own `SupabaseRunStore` over it, exactly as `_deps` builds one over its
    own client."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.db = FakeDB()
        self.profiles = FakeProfiles()
        self.scores = FakeScores()
        self.credits = FakeCredits()

    def runs(self) -> list[dict[str, Any]]:
        return self.db.rows("agent_runs")

    def start(self, key: str | None = None, **headers: str) -> Any:
        sent = {**auth(make_token()), **headers}
        if key:
            sent["Idempotency-Key"] = key
        return self.client.post("/score/compute", json={"background": True}, headers=sent)

    def poll(self, run_id: str, *, sub: str = USER_ID) -> Any:
        return self.client.get(f"/agents/runs/{run_id}", headers=auth(make_token(sub=sub)))


@pytest.fixture
def wired(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Wired:
    from carigma_api.routes import agents as agents_routes
    from carigma_api.routes import score as score_routes

    w = Wired(client)
    monkeypatch.setattr(
        score_routes,
        "_deps",
        lambda request, user, settings: (w.profiles, w.scores, w.credits, SupabaseRunStore(w.db)),
    )
    monkeypatch.setattr(agents_routes, "_store", lambda request, settings: SupabaseRunStore(w.db))
    return w


def _analyst(monkeypatch: pytest.MonkeyPatch, answer: Any) -> dict[str, int]:
    """Stub at the Claude boundary; count the calls, because a second call is
    a second run — and a second charge."""
    from carigma_api.services.agents import profile_analyst

    calls = {"n": 0}

    def call(system: str, user: str, max_tokens: int = 4096, *, api_key: str) -> Any:
        calls["n"] += 1
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer()
        return dict(answer)

    monkeypatch.setattr(profile_analyst, "call_claude_json", call)
    return calls


def _until(condition: Any, *, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("the condition never held")


# ── Start, then poll ───────────────────────────────────────────────────────


def test_a_background_start_answers_with_the_run_and_the_poll_finds_it_finished(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    _analyst(monkeypatch, ANALYST_PAYLOAD)

    started = wired.start()

    assert started.status_code == 202
    body = started.json()
    assert body["status"] == "running"
    assert body["agent"] == "profile"
    # Nothing is charged at the start — the charge comes after a success.
    assert body["credits"]["charged"] == 0

    polled = wired.poll(body["run_id"])
    assert polled.status_code == 200
    run = polled.json()
    assert run["status"] == "succeeded"
    assert run["result"]["profileScore"] == 71
    assert run["credits"]["charged"] == 10
    assert run["error"] is None
    assert [(s["step"], s["status"]) for s in run["steps"]] == [
        ("analyse", "done"),
        ("keep", "done"),
    ]
    assert run["finished_at"] is not None
    assert [delta for delta, _reason in wired.credits.ledger] == [-10]


def test_the_reading_is_kept_before_the_run_reads_as_finished(
    wired: Wired, monkeypatch: pytest.MonkeyPatch, _kept_analyses: FakeDB
) -> None:
    """A page polling the run refetches the score the moment it reads
    `succeeded`. Kept after that moment, the reading would be missed."""
    seen_at_settle: dict[str, Any] = {}
    from carigma_api.services import runs as runs_service

    real_finish = runs_service._finish

    def finish(run: Any, store: Any) -> None:
        seen_at_settle["scores"] = list(wired.scores.recorded)
        seen_at_settle["kept"] = len(_kept_analyses.rows("results"))
        real_finish(run, store)

    monkeypatch.setattr(runs_service, "_finish", finish)
    _analyst(monkeypatch, ANALYST_PAYLOAD)

    wired.start()

    assert seen_at_settle == {"scores": [71], "kept": 1}


def test_a_reading_that_could_not_be_kept_says_so_in_its_step(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run still succeeds — the user is looking at the reading — but the
    step does not claim a copy that was not made."""
    _analyst(monkeypatch, ANALYST_PAYLOAD)

    def broken(user_id: str, score: int) -> None:
        raise RuntimeError("score_history is down")

    wired.scores.record = broken  # type: ignore[method-assign]

    run = wired.poll(wired.start().json()["run_id"]).json()

    assert run["status"] == "succeeded"
    assert run["steps"][-1] == {"step": "keep", "label": "Keeping the reading", "status": "failed"}


# ── THE retry: in flight, it is the same run, charged once ────────────────


def test_a_retry_while_the_run_is_in_flight_is_the_same_run_and_charged_once(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect 0.072 recorded: `_finish` was the only save, so a retry
    under the same Idempotency-Key, sent while the run was still going, found
    nothing — and started a second run that charged a second time."""
    release = threading.Event()
    calls = _analyst(monkeypatch, lambda: (release.wait(5), dict(ANALYST_PAYLOAD))[1])

    first: dict[str, Any] = {}
    worker = threading.Thread(target=lambda: first.update(r=wired.start("tap-1")))
    worker.start()
    try:
        # The run is on the table while it works, with its step showing.
        _until(lambda: any(r.get("status") == "running" and r.get("steps") for r in wired.runs()))
        in_flight = wired.runs()[0]
        assert in_flight["steps"][0]["status"] == "active"
        polled = wired.poll(str(in_flight["id"])).json()
        assert polled["status"] == "running"
        assert polled["steps"][0]["label"] == "Reading your profile"

        retry = wired.start("tap-1")
    finally:
        release.set()
        worker.join(10)

    assert retry.status_code == 200
    replay = retry.json()
    assert replay["replayed"] is True
    assert replay["status"] == "running"
    assert replay["run_id"] == first["r"].json()["run_id"]
    assert replay["credits"]["charged"] == 0
    # No claim about a charge that has not happened yet.
    assert "charged once" not in replay["note"]

    assert calls["n"] == 1, "the retry ran the analyst a second time"
    assert len(wired.credits.ledger) == 1, f"charged {len(wired.credits.ledger)} times"
    assert wired.credits.balance == 90
    assert len(wired.runs()) == 1


def test_a_retry_after_the_run_finished_is_its_answer_and_not_a_charge(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _analyst(monkeypatch, ANALYST_PAYLOAD)
    run_id = wired.start("tap-2").json()["run_id"]

    replay = wired.start("tap-2").json()

    assert replay["run_id"] == run_id
    assert replay["status"] == "succeeded"
    assert replay["result"]["profileScore"] == 71
    assert replay["credits"]["charged"] == 0
    assert calls["n"] == 1
    assert len(wired.credits.ledger) == 1


def test_two_starts_racing_under_one_key_make_one_run(wired: Wired) -> None:
    """Both requests look for the key before either has written it — so both
    find nothing. The unique index on (user_id, idempotency_key) lets one
    INSERT through; the other is answered with the run that won."""
    from carigma_api.services import runs as runs_service

    first, second = (runs_service.new_run(USER_ID, "profile") for _ in range(2))
    store_a, store_b = SupabaseRunStore(wired.db), SupabaseRunStore(wired.db)
    store_a.remember_idempotency(USER_ID, "tap-3", first.id)
    store_b.remember_idempotency(USER_ID, "tap-3", second.id)

    assert runs_service.begin(first, store_a) is None
    holder = runs_service.begin(second, store_b)

    assert holder is not None and holder.id == first.id
    assert [r["id"] for r in wired.runs()] == [first.id]


def test_runs_without_a_key_never_collide(wired: Wired) -> None:
    """NULLs are distinct in a unique index. A fake that matched `None` to
    `None` would refuse every second keyless run."""
    from carigma_api.services import runs as runs_service

    for _ in range(2):
        assert (
            runs_service.begin(runs_service.new_run(USER_ID, "profile"), SupabaseRunStore(wired.db))
            is None
        )
    assert len(wired.runs()) == 2


# ── Refusals before any work ───────────────────────────────────────────────


def test_a_start_that_cannot_be_recorded_is_refused_and_runs_nothing(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row IS a background run's answer: an id nobody can read back is a
    run the page can never see finish. So it does not start."""
    calls = _analyst(monkeypatch, ANALYST_PAYLOAD)
    wired.db.failing.add("agent_runs")

    r = wired.start()

    assert r.status_code == 503
    assert r.headers["X-Error-Code"] == "run_not_started"
    assert calls["n"] == 0
    assert wired.credits.ledger == []


def test_a_token_about_to_expire_does_not_start_a_background_run(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run writes with this token after the answer has gone. One that
    expires mid-run turns every write — the charge, the reading, the finish —
    into a 401, and the page polls a row that never finishes."""
    calls = _analyst(monkeypatch, ANALYST_PAYLOAD)

    r = wired.client.post(
        "/score/compute",
        json={"background": True},
        headers=auth(make_token(exp_delta=120)),
    )

    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "session_expiring"
    assert calls["n"] == 0
    assert wired.runs() == []


def test_insufficient_credits_refuse_a_background_start_before_any_row(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _analyst(monkeypatch, ANALYST_PAYLOAD)
    wired.credits.balance = 3

    r = wired.start()

    assert r.status_code == 402
    assert calls["n"] == 0
    assert wired.runs() == []


# ── Terminal states, read back ─────────────────────────────────────────────


def test_a_failed_run_reads_back_as_plain_language_and_free(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    from carigma_api.services.ai import UpstreamError

    _analyst(monkeypatch, UpstreamError("anthropic.RateLimitError: 429 overloaded_error"))

    run = wired.poll(wired.start().json()["run_id"]).json()

    assert run["status"] == "failed"
    assert run["credits"]["charged"] == 0
    assert run["result"] is None
    assert run["error"] == "Things are busy right now. Please wait a few seconds and try again."
    # The text a page shows — not ids or timestamps, where "429" can occur
    # by chance (a run id once held "42997").
    for text in (run["error"], *(step["label"] for step in run["steps"])):
        assert "429" not in text and "anthropic" not in text.lower(), text
    # The step it stopped on says so, rather than spinning for ever.
    assert run["steps"] == [
        {"step": "analyse", "label": "Reading your profile", "status": "failed"}
    ]
    assert wired.credits.ledger == []
    assert wired.scores.recorded == []
    # The row keeps what happened, for us; the client is told what to do.
    assert "429" in wired.runs()[0]["error"]


@pytest.mark.parametrize("status", ["empty", "cancelled"])
def test_the_other_terminal_states_read_back_free(wired: Wired, status: str) -> None:
    run_id = "33333333-3333-3333-3333-333333333333"
    wired.db.seed(
        "agent_runs",
        {
            "id": run_id,
            "user_id": USER_ID,
            "agent": "profile",
            "status": status,
            "steps": [{"step": "analyse", "label": "Reading your profile", "status": "done"}],
            "result": None,
            "error": None,
            "credits_charged": 0,
            "idempotency_key": None,
            "started_at": "2026-10-19T17:11:00+00:00",
            "finished_at": "2026-10-19T17:11:40+00:00",
        },
    )

    run = wired.poll(run_id).json()

    assert run["status"] == status
    assert run["credits"] == {"charged": 0, "balance_after": None, "reason": ""}
    assert run["error"] is None


# ── Ownership ──────────────────────────────────────────────────────────────


def test_another_users_run_is_not_found(wired: Wired, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 exactly like an id never issued — a 403 would confirm the id is
    real. RLS returns no row for it in production; the route checks too, so
    the answer does not rest on a policy alone."""
    _analyst(monkeypatch, ANALYST_PAYLOAD)
    run_id = wired.start().json()["run_id"]

    theirs = wired.poll(run_id, sub=OTHER_USER)
    never = wired.poll("44444444-4444-4444-4444-444444444444", sub=OTHER_USER)

    assert theirs.status_code == 404
    assert theirs.json() == never.json()
    assert "profileScore" not in theirs.text


def test_an_id_that_cannot_exist_is_not_found_not_a_validation_error(wired: Wired) -> None:
    r = wired.poll("run_not-a-uuid")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "no_such_run"


def test_a_run_that_could_not_be_read_is_not_called_missing(wired: Wired) -> None:
    """A poll told "no such run" stops watching. A blip is a 503 it retries."""
    wired.db.failing.add("agent_runs")
    r = wired.poll("55555555-5555-5555-5555-555555555555")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "read_failed"


def test_polling_requires_a_token(client: TestClient) -> None:
    assert client.get("/agents/runs/55555555-5555-5555-5555-555555555555").status_code == 401


# ── The browser can send the key at all ────────────────────────────────────


def test_the_browser_may_send_an_idempotency_key(client: TestClient) -> None:
    """`allow_headers` listed only Authorization and Content-Type, so a
    browser preflight for `Idempotency-Key` was refused and the retry that
    makes a dropped start safe could never have left the page."""
    r = client.options(
        "/score/compute",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type, idempotency-key",
        },
    )

    assert r.status_code == 200
    assert "idempotency-key" in r.headers["access-control-allow-headers"].lower()


# ── Picking a run back up (UAT, 20 Oct) ────────────────────────────────────


def _seed_run(
    w: Wired,
    *,
    status: str = "running",
    minutes_ago: float = 1,
    user: str = USER_ID,
    agent: str = "profile",
) -> str:
    from datetime import UTC, datetime, timedelta

    from carigma_api.services.runs import RunStatus, new_run

    run = new_run(user, agent)
    run.status = RunStatus(status)
    run.started_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    SupabaseRunStore(w.db).save(run)
    return run.id


def _active(w: Wired, *, agent: str = "profile", sub: str = USER_ID) -> Any:
    return w.client.get(
        "/agents/runs/active", params={"agent": agent}, headers=auth(make_token(sub=sub))
    )


def test_a_page_that_lost_its_run_finds_it_again(wired: Wired) -> None:
    """The run id lived only in the page. A reload mid-run, or a visit to
    Today and back, and the page showed the old reading while the run
    finished and charged out of sight."""
    older = _seed_run(wired, minutes_ago=3)
    newest = _seed_run(wired, minutes_ago=0.5)

    res = _active(wired)

    assert res.status_code == 200
    assert res.json()["run"]["run_id"] == newest != older
    assert res.json()["run"]["status"] == "running"


def test_a_finished_run_is_not_in_flight(wired: Wired) -> None:
    _seed_run(wired, status="succeeded", minutes_ago=0.5)

    assert _active(wired).json() == {"run": None}


def test_a_run_stuck_running_past_ten_minutes_is_not_watched_forever(wired: Wired) -> None:
    """A restart mid-run leaves its row `running`. Offered to the page, it
    would be watched forever."""
    _seed_run(wired, minutes_ago=11)

    assert _active(wired).json() == {"run": None}


def test_another_users_run_in_flight_is_not_offered(wired: Wired) -> None:
    _seed_run(wired, user=OTHER_USER)

    assert _active(wired).json() == {"run": None}


def test_only_the_agent_asked_about(wired: Wired) -> None:
    _seed_run(wired, agent="content")

    assert _active(wired).json() == {"run": None}
    # An agent that never runs in the background has nothing to pick up.
    assert _active(wired, agent="naukri").status_code == 422


def test_could_not_look_is_not_no_run(wired: Wired) -> None:
    """ "No run in flight" for a failed read would offer a second run beside
    the first."""
    wired.db.failing.add("agent_runs")

    res = _active(wired)

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"


def test_active_is_not_read_as_a_run_id(wired: Wired) -> None:
    """Routes match in order: declared after `/agents/runs/{run_id}`, the
    word "active" was a run id — and a 404."""
    assert _active(wired).status_code == 200
