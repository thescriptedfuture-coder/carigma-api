"""Signal shows the last reading, with the day it was taken.

It refused: "The Profile Analyst hasn't read your profile in this session. We
won't show a score we haven't just measured" — to a user with twenty-two
readings. V2 kept only the number; V1 kept the whole analysis in
`results.profile_data` all along. A stored reading with its date is a record.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import score as score_routes
from tests.conftest import USER_ID, auth, make_token
from tests.fake_supabase import FakeDB
from tests.run_fakes import RecordingRunStore
from tests.test_score_endpoint import (
    ANALYST_PAYLOAD,
    FakeCredits,
    FakeProfiles,
    FakeScores,
    _stub_analyst,
)

#: Monday 19 Oct 2026, 21:05 IST.
NOW = datetime(2026, 10, 19, 15, 35, tzinfo=UTC)

#: Ravi's row, as V1 wrote it: last measured on 11 Aug.
V1_ROW = {
    "user_id": USER_ID,
    "posts": [{"id": "p1", "body": "A post V1 drafted."}],
    "jobs": [{"id": "j3", "title": "Business Analyst"}],
    "profile_data": {
        "profileScore": 62,
        "scoreBreakdown": {
            "about": 58,
            "skills": 60,
            "activity": 20,
            "headline": 65,
            "experience": 70,
        },
        "scoreSummary": "A solid foundation, undermined by zero quantified metrics.",
        "optimizedHeadline": "Finance & MIS Analyst | ERP | Government finance",
        "optimizedAbout": "A better About.",
        "quickWins": [
            {"action": "Add two hard numbers to your About", "impact": "high", "effort": "low"}
        ],
        "topKeywordsToAdd": ["SAP"],
        "weeklyGoal": "Quantify one bullet.",
    },
    "last_run": json.dumps(
        {
            "content": "05 Jul 2026, 08:51 AM",
            "jobs": "11 Aug 2026, 04:33 PM",
            "profile": "11 Aug 2026, 09:42 AM",
        }
    ),
}


@pytest.fixture
def at_now(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return NOW

    monkeypatch.setattr(score_routes, "datetime", _Clock)


def _latest(client: TestClient) -> dict[str, Any]:
    res = client.get("/score/latest", headers=auth(make_token()))
    assert res.status_code == 200, res.text
    return dict(res.json())


def test_a_v1_reading_is_shown_with_the_day_it_was_taken(
    client: TestClient, _kept_analyses: FakeDB, at_now: None
) -> None:
    _kept_analyses.seed("results", V1_ROW)

    body = _latest(client)

    analysis = body["analysis"]
    assert analysis["profileScore"] == 62
    assert analysis["scoreBreakdown"]["activity"] == 20
    assert analysis["quickWins"] == [
        {"action": "Add two hard numbers to your About", "impact": "high", "effort": "low"}
    ]
    # V1 never stored these; a V1 reading must render exactly as a V2 one.
    assert analysis["band"] == "CLEAR"
    assert analysis["variance_note"]
    # V1's stamp: the day is trustworthy, its hour's timezone is not.
    assert body["measured_on"] == "2026-08-11"
    assert body["measured_at"] is None
    assert body["age_days"] == 69
    assert body["stale"] is True
    assert body["run_cost"] == 10


def test_keys_signal_does_not_render_are_not_sent(
    client: TestClient, _kept_analyses: FakeDB
) -> None:
    _kept_analyses.seed("results", V1_ROW)

    assert set(_latest(client)["analysis"]) == {
        "profileScore",
        "band",
        "scoreBreakdown",
        "scoreSummary",
        "optimizedHeadline",
        "optimizedAbout",
        "quickWins",
        "variance_note",
    }


def test_never_scored_is_null_not_a_zero(client: TestClient, _kept_analyses: FakeDB) -> None:
    body = _latest(client)

    assert body["analysis"] is None
    assert (body["measured_on"], body["age_days"], body["stale"]) == (None, None, False)


def test_a_results_row_from_another_agent_is_not_a_reading(
    client: TestClient, _kept_analyses: FakeDB
) -> None:
    """V1 creates the row for ANY agent; `profile_data` defaults to `{}`."""
    _kept_analyses.seed("results", {**V1_ROW, "profile_data": {}})

    assert _latest(client)["analysis"] is None


def test_an_unreadable_reading_is_a_503_not_never_scored(
    client: TestClient, _kept_analyses: FakeDB
) -> None:
    _kept_analyses.failing.add("results")

    res = client.get("/score/latest", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"


def test_an_unrecorded_day_claims_neither_fresh_nor_stale(
    client: TestClient, _kept_analyses: FakeDB
) -> None:
    _kept_analyses.seed("results", {**V1_ROW, "last_run": "not a stamp"})

    body = _latest(client)

    assert body["analysis"]["profileScore"] == 62
    assert (body["measured_on"], body["age_days"], body["stale"]) == (None, None, False)


# ── A V2 run keeps the reading ─────────────────────────────────────────────


@pytest.fixture
def run_wired(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        score_routes,
        "_deps",
        lambda request, user, settings: (
            FakeProfiles(),
            FakeScores(),
            FakeCredits(),
            RecordingRunStore(),
        ),
    )
    _stub_analyst(monkeypatch, ANALYST_PAYLOAD)
    return client


def test_a_run_keeps_the_whole_reading_and_leaves_v1s_work_alone(
    run_wired: TestClient, _kept_analyses: FakeDB
) -> None:
    """V2 stored only the number. And `results` is V1's row: its posts, its
    jobs and its other agents' stamps must survive a V2 run."""
    _kept_analyses.seed("results", V1_ROW)

    assert run_wired.post("/score/compute", json={}, headers=auth(make_token())).status_code == 200

    row = _kept_analyses.rows("results")[0]
    assert row["profile_data"]["profileScore"] == 71
    assert row["profile_data"]["optimizedHeadline"] == ANALYST_PAYLOAD["optimizedHeadline"]
    assert row["profile_data"]["measuredAt"]
    assert row["posts"] == V1_ROW["posts"]
    assert row["jobs"] == V1_ROW["jobs"]
    stamps = json.loads(row["last_run"])
    assert stamps["content"] == "05 Jul 2026, 08:51 AM"
    assert stamps["jobs"] == "11 Aug 2026, 04:33 PM"
    assert stamps["profile"] != "11 Aug 2026, 09:42 AM"
    # In V1's own format, so V1 reads it as it reads its own.
    datetime.strptime(stamps["profile"], "%d %b %Y, %I:%M %p")


def test_the_next_visit_shows_that_reading_measured_today(
    run_wired: TestClient, _kept_analyses: FakeDB
) -> None:
    run_wired.post("/score/compute", json={}, headers=auth(make_token()))

    body = _latest(run_wired)

    assert body["analysis"]["profileScore"] == 71
    assert body["measured_at"] is not None
    assert body["age_days"] == 0
    assert body["stale"] is False


def test_a_reading_that_cannot_be_kept_still_reaches_the_user(
    run_wired: TestClient, _kept_analyses: FakeDB
) -> None:
    """Losing the copy costs a record, not the answer on screen."""
    _kept_analyses.failing.add("results")

    res = run_wired.post("/score/compute", json={}, headers=auth(make_token()))

    assert res.status_code == 200
    assert res.json()["result"]["profileScore"] == 71
