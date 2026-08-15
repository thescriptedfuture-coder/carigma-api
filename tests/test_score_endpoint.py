"""Profile Analyst end-to-end through a real endpoint.

The P2 deliverable: verified JWT → stateless service → run protocol → credit
rule → persisted result, exercised through HTTP.

Supabase and Anthropic are stubbed at the boundary; everything between the
request and those boundaries is the real code path — real JWT verification,
real routing, real credit arithmetic.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.services.repository import profile_to_db
from tests.conftest import USER_ID, auth, make_token


class _NullRunStore:
    """Runs are persisted through this in production; these tests are about the
    scoring path, so it records nothing and finds nothing. Notably it returns
    None for every idempotency lookup, which means "no replay" — the replay
    behaviour itself is covered in test_deferred.py."""

    def save(self, run: object) -> None: ...

    def get(self, run_id: str) -> None:
        return None

    def remember_idempotency(self, user_id: str, key: str, run_id: str) -> None: ...

    def find_by_idempotency_key(self, user_id: str, key: str) -> None:
        return None


SAMPLE_PROFILE = {
    "name": "Ravi Kumar",
    "linkedinHeadline": "Business Analyst at DTS",
    "aboutSection": "I work with data.",
    "experience": "5 years in analytics",
    "skills": "SQL, Power BI",
    "targetRoles": "Data Analyst",
}

ANALYST_PAYLOAD = {
    "profileScore": 71,
    "scoreBreakdown": {"headline": 14, "about": 18, "experience": 19, "skills": 11, "activity": 9},
    "scoreSummary": "Solid foundation, undersold headline.",
    "optimizedHeadline": "Data Analyst | SQL, Power BI | Turning ops data into decisions",
    "optimizedAbout": "A much better about section.",
    "topKeywordsToAdd": ["dbt", "Python", "A/B testing", "SQL", "dashboards", "stakeholders"],
    "featuredSectionIdea": "Pin your dashboard case study.",
    "quickWins": [],
    "weeklyGoal": "Rewrite your headline.",
    "recruitersWouldSay": "Capable, but I can't tell what they want to do next.",
}


class FakeProfiles:
    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        self.profile = dict(profile) if profile is not None else dict(SAMPLE_PROFILE)
        self.saved: dict[str, Any] | None = None
        #: What would actually land in the table.
        self.columns: dict[str, Any] = {}

    def load(self, user_id: str) -> dict[str, Any]:
        return dict(self.profile)

    def save(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        """Runs the REAL mapping, so an unmapped key fails here as it would in
        production. A fake simpler than the thing it replaces hides exactly the
        bugs that live in the difference — which is how a profile save that
        silently dropped its entire payload passed for a whole phase."""
        self.columns = profile_to_db(profile)
        self.profile = dict(profile)
        self.saved = dict(profile)
        return dict(profile)


class FakeScores:
    def __init__(self) -> None:
        self.recorded: list[int] = []

    def record(self, user_id: str, score: int) -> None:
        self.recorded.append(score)

    def history(self, user_id: str, limit: int = 60) -> list[dict[str, Any]]:
        return [{"score": s, "created_at": "2026-08-04T09:04:00Z"} for s in self.recorded]


class FakeCredits:
    def __init__(self, balance: int | None = 100) -> None:
        self.balance = balance
        self.ledger: list[tuple[int, str]] = []

    def read_balance(self, user_id: str) -> int | None:
        return self.balance

    def apply_delta(self, user_id: str, delta: int, reason: str, balance_after: int) -> None:
        self.balance = balance_after
        self.ledger.append((delta, reason))


@pytest.fixture
def wired(client: TestClient, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Wire the route's collaborators to fakes, leaving the route logic real."""
    from carigma_api.routes import score as score_routes

    profiles, scores, creds = FakeProfiles(), FakeScores(), FakeCredits()
    monkeypatch.setattr(
        score_routes,
        "_deps",
        lambda request, user, settings: (profiles, scores, creds, _NullRunStore()),
    )
    return client, profiles, scores, creds


def _stub_analyst(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    """Stub at the CLAUDE boundary, not at `profile_analyst.run`.

    Replacing `run` would skip the real prompt assembly, score coercion, band
    derivation and variance note — i.e. most of what the service actually does.
    Patching the model call keeps all of that under test.
    """
    from carigma_api.services.agents import profile_analyst

    monkeypatch.setattr(
        profile_analyst,
        "call_claude_json",
        lambda system, user, max_tokens=4096, *, api_key: (
            dict(payload) if isinstance(payload, dict) else payload
        ),
    )


def _stub_analyst_raising(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    from carigma_api.services.agents import profile_analyst

    def boom(system: str, user: str, max_tokens: int = 4096, *, api_key: str) -> Any:
        raise exc

    monkeypatch.setattr(profile_analyst, "call_claude_json", boom)


# ── Happy path ─────────────────────────────────────────────────────────────


def test_compute_score_end_to_end(wired, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    client, _profiles, scores, creds = wired
    _stub_analyst(monkeypatch, ANALYST_PAYLOAD)

    r = client.post("/score/compute", json={}, headers=auth(make_token()))

    assert r.status_code == 200
    body = r.json()
    assert body["result"]["profileScore"] == 71
    # V2's five-band ladder from the locked design system, not V1's four.
    assert body["result"]["band"] == "CLEAR"
    assert body["credits"]["charged"] == 10
    assert body["credits"]["balance_after"] == 90
    assert body["provenance"]["agent_label"] == "Profile Analyst"
    assert scores.recorded == [71]
    assert creds.balance == 90


def test_score_payload_carries_the_variance_note(wired, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """The score is a contextual judgement, not a formula. Saying so in the
    payload is the guardrail against implying false precision."""
    client, *_ = wired
    _stub_analyst(monkeypatch, ANALYST_PAYLOAD)

    r = client.post("/score/compute", json={}, headers=auth(make_token()))
    assert "±5–10" in r.json()["result"]["variance_note"]


# ── THE failure path: no credits charged ───────────────────────────────────


def test_failed_run_charges_nothing_through_the_endpoint(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """The deliverable the brief asks for: prove the failure path bills nobody."""
    from carigma_api.services.ai import UpstreamError

    client, _profiles, scores, creds = wired
    _stub_analyst_raising(monkeypatch, UpstreamError("Claude is overloaded"))

    r = client.post("/score/compute", json={}, headers=auth(make_token()))

    assert r.status_code == 503
    assert creds.balance == 100, "credits were charged for a failed run"
    assert creds.ledger == [], "a ledger entry was written for a failed run"
    assert scores.recorded == [], "a score was persisted for a failed run"


def test_failure_shows_friendly_copy_not_a_raw_exception(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from carigma_api.services.ai import UpstreamError

    client, *_ = wired
    _stub_analyst_raising(monkeypatch, UpstreamError("anthropic.RateLimitError: 429 overloaded"))

    detail = client.post("/score/compute", json={}, headers=auth(make_token())).json()["detail"]

    assert "429" not in detail
    assert "anthropic" not in detail.lower()
    assert "busy right now" in detail
    # Error copy carries no emoji (Bible §18.1).
    assert all(ord(ch) < 0x2190 for ch in detail)


def test_unexpected_error_also_charges_nothing(wired, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    client, _profiles, _scores, creds = wired
    _stub_analyst_raising(monkeypatch, ValueError("nobody predicted this"))

    r = client.post("/score/compute", json={}, headers=auth(make_token()))
    assert r.status_code == 503
    assert creds.balance == 100


def test_non_numeric_score_is_a_failure_not_a_zero(wired, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """A garbled score must not silently become 0 — 0 is itself a real score,
    and persisting a fake one would be fabricating data."""
    client, _profiles, scores, creds = wired
    _stub_analyst(monkeypatch, {**ANALYST_PAYLOAD, "profileScore": "high"})

    r = client.post("/score/compute", json={}, headers=auth(make_token()))

    assert r.status_code == 503
    assert creds.balance == 100
    assert scores.recorded == []


def test_out_of_range_score_is_clamped_not_rejected(wired, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """A model slip past 100 is still a judgement we can report honestly;
    clamping keeps it truthful where inventing a default would not."""
    client, *_ = wired
    _stub_analyst(monkeypatch, {**ANALYST_PAYLOAD, "profileScore": 140})

    r = client.post("/score/compute", json={}, headers=auth(make_token()))
    assert r.json()["result"]["profileScore"] == 100
    assert r.json()["result"]["band"] == "COMMANDING"


# ── Insufficient credits ───────────────────────────────────────────────────


def test_insufficient_credits_blocks_before_the_work_runs(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    client, _profiles, _scores, creds = wired
    creds.balance = 3

    called = {"n": 0}

    def counting(system: str, user: str, max_tokens: int = 4096, *, api_key: str) -> Any:
        called["n"] += 1
        return dict(ANALYST_PAYLOAD)

    from carigma_api.services.agents import profile_analyst

    monkeypatch.setattr(profile_analyst, "call_claude_json", counting)

    r = client.post("/score/compute", json={}, headers=auth(make_token()))

    assert r.status_code == 402
    assert called["n"] == 0, "the agent ran despite an unaffordable balance"
    assert "3" in r.json()["detail"]
    assert creds.balance == 3


# ── Onboarding is free ─────────────────────────────────────────────────────


def test_onboarding_score_is_free(wired, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    client, _profiles, _scores, creds = wired
    creds.balance = 0
    _stub_analyst(monkeypatch, ANALYST_PAYLOAD)

    r = client.post("/score/compute", json={"onboarding": True}, headers=auth(make_token()))

    assert r.status_code == 200
    assert r.json()["credits"]["charged"] == 0
    assert creds.balance == 0


# ── Auth ───────────────────────────────────────────────────────────────────


def test_score_endpoints_require_a_token(client: TestClient) -> None:
    assert client.post("/score/compute", json={}).status_code == 401
    assert (
        client.post("/score/fix", json={"fix_key": "headline", "rewrite": "x"}).status_code == 401
    )
    assert client.get("/score/history").status_code == 401


# ── /score/fix — the "score never improves" fix ────────────────────────────


def test_apply_fix_writes_the_rewrite_into_the_stored_profile(wired) -> None:  # type: ignore[no-untyped-def]
    """V1's bug 1.1: a flag alone left the next run scoring stale data. The
    rewrite must be PERSISTED, or the score can never climb."""
    client, profiles, _scores, _creds = wired

    r = client.post(
        "/score/fix",
        json={"fix_key": "headline", "rewrite": "Data Analyst | SQL, Power BI"},
        headers=auth(make_token()),
    )

    assert r.status_code == 200
    assert r.json()["applied"] is True
    assert r.json()["field"] == "linkedinHeadline"
    assert profiles.saved is not None, "nothing was written"
    assert profiles.saved["linkedinHeadline"] == "Data Analyst | SQL, Power BI"


def test_a_rerun_after_a_fix_sees_the_improved_profile(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """The end-to-end proof that the fix is real: what the analyst reads on the
    second run is the rewritten text."""
    client, profiles, _scores, _creds = wired

    client.post(
        "/score/fix",
        json={"fix_key": "about", "rewrite": "A far stronger About section."},
        headers=auth(make_token()),
    )

    # Capture the PROMPT the analyst actually sends — the strongest proof the
    # rewrite reached the model, not just the database.
    seen: dict[str, str] = {}

    def capture(system: str, user: str, max_tokens: int = 4096, *, api_key: str) -> Any:
        seen["user"] = user
        return dict(ANALYST_PAYLOAD)

    from carigma_api.services.agents import profile_analyst

    monkeypatch.setattr(profile_analyst, "call_claude_json", capture)
    client.post("/score/compute", json={}, headers=auth(make_token()))

    assert "A far stronger About section." in seen["user"]


def test_apply_fix_is_free(wired) -> None:  # type: ignore[no-untyped-def]
    """Accepting our own suggestion is not a purchase."""
    client, _profiles, _scores, creds = wired
    client.post(
        "/score/fix",
        json={"fix_key": "headline", "rewrite": "x"},
        headers=auth(make_token()),
    )
    assert creds.balance == 100


def test_unapplyable_fix_key_is_rejected(wired) -> None:  # type: ignore[no-untyped-def]
    """'keywords' is copy guidance, not an auto-applied field — say so rather
    than silently doing nothing."""
    client, profiles, _scores, _creds = wired

    r = client.post(
        "/score/fix",
        json={"fix_key": "keywords", "rewrite": "SQL, dbt"},
        headers=auth(make_token()),
    )

    assert r.status_code == 422
    assert profiles.saved is None


def test_empty_profile_is_a_clear_404_not_a_crash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from carigma_api.routes import score as score_routes

    monkeypatch.setattr(
        score_routes,
        "_deps",
        lambda request, user, settings: (
            FakeProfiles({}),
            FakeScores(),
            FakeCredits(),
            _NullRunStore(),
        ),
    )

    r = client.post("/score/compute", json={}, headers=auth(make_token()))
    assert r.status_code == 404
    assert "profile details" in r.json()["detail"]


# ── History ────────────────────────────────────────────────────────────────


def test_score_history_returns_the_series(wired, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    client, _profiles, scores, _creds = wired
    _stub_analyst(monkeypatch, ANALYST_PAYLOAD)
    client.post("/score/compute", json={}, headers=auth(make_token()))

    r = client.get("/score/history", headers=auth(make_token()))
    assert r.status_code == 200
    assert r.json()["items"][0]["score"] == 71


def test_one_users_token_cannot_read_another_users_scores(wired, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The repository is keyed on the VERIFIED sub claim, never on a client-
    supplied id — so there is no id to tamper with."""
    client, _profiles, scores, _creds = wired
    _stub_analyst(monkeypatch, ANALYST_PAYLOAD)

    seen_ids: list[str] = []
    original = scores.record
    scores.record = lambda uid, s: (seen_ids.append(uid), original(uid, s))[1]  # type: ignore[assignment]

    client.post("/score/compute", json={}, headers=auth(make_token(sub=USER_ID)))
    assert seen_ids == [USER_ID]
