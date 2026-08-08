"""Admin over HTTP.

Admin reads with the SERVICE key — it exists precisely to see across users,
which per-user RLS is designed to prevent. That makes `ADMIN_EMAILS` the only
thing between a normal account and everyone's data, so most of this file is
about that boundary.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.config import Settings, get_settings
from carigma_api.routes import admin as admin_routes
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, auth, make_token

ADMIN_PATHS = [
    "/admin/overview",
    "/admin/users",
    "/admin/credit-requests",
    "/admin/feedback",
    "/admin/runs",
    "/admin/system",
    "/admin/analytics",
]


class FakeTable:
    def __init__(self, data: dict[str, list[dict[str, Any]]]):
        self._data = data
        self._name = ""
        self._filters: dict[str, Any] = {}
        self.inserted: list[tuple[str, dict[str, Any]]] = []
        self.upserted: list[tuple[str, dict[str, Any]]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []

    def table(self, name: str) -> FakeTable:
        self._name = name
        self._filters = {}
        return self

    def select(self, *_: Any) -> FakeTable:
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def limit(self, _: int) -> FakeTable:
        return self

    def insert(self, row: dict[str, Any]) -> FakeTable:
        self.inserted.append((self._name, row))
        return self

    def upsert(self, row: dict[str, Any]) -> FakeTable:
        self.upserted.append((self._name, row))
        return self

    def update(self, row: dict[str, Any]) -> FakeTable:
        self.updated.append((self._name, row))
        return self

    def execute(self) -> Any:
        rows = self._data.get(self._name, [])
        for col, val in self._filters.items():
            rows = [r for r in rows if str(r.get(col)) == str(val)]
        return type("Res", (), {"data": rows})()


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeTable:
    fake = FakeTable(
        {
            "credits": [
                {"user_id": "u1", "balance": 0},
                {"user_id": "u2", "balance": 6},
                {"user_id": "u3", "balance": 400},
            ],
            "profiles": [
                {"id": "u1", "email": "blocked@x.com", "name": "Blocked"},
                {"id": "u2", "email": "low@x.com", "name": "Low"},
                {"id": "u3", "email": "fine@x.com", "name": "Fine"},
            ],
            "credit_requests": [
                {
                    "id": 1,
                    "user_id": "u2",
                    "note": "ran out",
                    "resolved_at": None,
                    "asked_at": "2026-08-07T10:00:00Z",
                },
            ],
            "agent_runs": [
                {
                    "id": "r1",
                    "user_id": "u1",
                    "agent": "profile",
                    "status": "succeeded",
                    "credits_charged": 10,
                    "started_at": "2026-08-07T09:00:00Z",
                    "finished_at": "2026-08-07T09:00:12Z",
                },
                {
                    "id": "r2",
                    "user_id": "u2",
                    "agent": "jobs",
                    "status": "failed",
                    "credits_charged": 0,
                    "started_at": "2026-08-07T09:05:00Z",
                },
            ],
            "credit_ledger": [
                {"delta": 100, "kind": "signup"},
                {"delta": -10, "kind": "spend"},
            ],
            "feedback": [{"id": 1, "user_id": "u1", "message": "Loved the decode"}],
            "score_history": [{"user_id": "u1", "score": 71}],
            "admin_notes": [],
        }
    )
    monkeypatch.setattr(admin_routes, "service_client", lambda settings: fake)
    return fake


def admin_auth() -> dict[str, str]:
    return auth(make_token(email=ADMIN_EMAIL))


# ── The boundary ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ADMIN_PATHS)
def test_every_admin_route_refuses_an_anonymous_caller(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", ADMIN_PATHS)
def test_every_admin_route_refuses_a_normal_user(
    client: TestClient, db: FakeTable, path: str
) -> None:
    """A verified token is not enough — admin reads across everyone."""
    res = client.get(path, headers=auth(make_token(email=USER_EMAIL)))
    assert res.status_code == 403, f"{path} let a normal user in"


def test_an_empty_allow_list_means_nobody_is_admin(
    client: TestClient, settings: Settings, db: FakeTable
) -> None:
    """The safe default. A misconfigured deploy locks admin out rather than
    opening everyone's data."""
    client.app.dependency_overrides[get_settings] = lambda: settings.model_copy(  # type: ignore[attr-defined]
        update={"admin_emails": ""}
    )
    assert client.get("/admin/overview", headers=admin_auth()).status_code == 403


def test_the_mutating_routes_are_gated_too(client: TestClient, db: FakeTable) -> None:
    normal = auth(make_token(email=USER_EMAIL))
    assert (
        client.post(
            "/admin/users/u1/credits", json={"delta": 100, "reason": "hi"}, headers=normal
        ).status_code
        == 403
    )
    assert (
        client.post("/admin/users/u1/notes", json={"note": "hi"}, headers=normal).status_code == 403
    )


# ── Low-credit surfacing ───────────────────────────────────────────────────


def test_the_overview_leads_with_who_needs_action(client: TestClient, db: FakeTable) -> None:
    body = client.get("/admin/overview", headers=admin_auth()).json()

    # First key in the payload, and the client renders it first.
    assert next(iter(body)) == "low_credit"
    assert body["low_credit"]["blocked"] == 1
    assert body["low_credit"]["nearly_out"] == 1
    assert body["low_credit"]["needs_action"] == 2


def test_the_blocked_user_is_first_in_the_list(client: TestClient, db: FakeTable) -> None:
    users = client.get("/admin/overview", headers=admin_auth()).json()["low_credit"]["users"]

    assert users[0]["email"] == "blocked@x.com"
    assert users[0]["urgency"] == "blocked"


def test_a_healthy_user_is_absent_from_the_action_list(client: TestClient, db: FakeTable) -> None:
    users = client.get("/admin/overview", headers=admin_auth()).json()["low_credit"]["users"]
    assert "fine@x.com" not in [u["email"] for u in users]


def test_an_open_ask_is_flagged_on_the_row(client: TestClient, db: FakeTable) -> None:
    users = client.get("/admin/overview", headers=admin_auth()).json()["low_credit"]["users"]
    low = next(u for u in users if u["email"] == "low@x.com")

    assert low["has_open_request"] is True
    assert "asked for more" in low["line"]


def test_the_low_credit_filter_works_on_the_user_list(client: TestClient, db: FakeTable) -> None:
    all_users = client.get("/admin/users", headers=admin_auth()).json()
    filtered = client.get("/admin/users?low_credit=true", headers=admin_auth()).json()

    assert all_users["total"] == 3
    assert filtered["total"] == 2


def test_search_matches_email_and_name(client: TestClient, db: FakeTable) -> None:
    assert client.get("/admin/users?q=blocked", headers=admin_auth()).json()["total"] == 1
    assert client.get("/admin/users?q=Fine", headers=admin_auth()).json()["total"] == 1


# ── Manual adjustment ──────────────────────────────────────────────────────


def test_adding_credits_writes_the_balance_and_an_auditable_ledger_row(
    client: TestClient, db: FakeTable
) -> None:
    res = client.post(
        "/admin/users/u1/credits",
        json={"delta": 200, "reason": "ran out mid-tune-up"},
        headers=admin_auth(),
    )

    assert res.status_code == 200
    assert res.json()["balance"] == 200

    ledger = [row for table, row in db.inserted if table == "credit_ledger"]
    assert len(ledger) == 1
    assert ledger[0]["kind"] == "admin_adjustment"
    assert "ran out mid-tune-up" in ledger[0]["reason"]
    assert ADMIN_EMAIL in ledger[0]["reason"], "the ledger must record who did it"


def test_an_adjustment_without_a_reason_is_refused_by_the_schema(
    client: TestClient, db: FakeTable
) -> None:
    res = client.post(
        "/admin/users/u1/credits", json={"delta": 100, "reason": ""}, headers=admin_auth()
    )
    assert res.status_code == 422
    assert db.inserted == [], "nothing may be written on a refused adjustment"


def test_a_zero_adjustment_writes_nothing(client: TestClient, db: FakeTable) -> None:
    res = client.post(
        "/admin/users/u1/credits",
        json={"delta": 0, "reason": "typo"},
        headers=admin_auth(),
    )
    assert res.status_code == 400
    assert db.inserted == []


def test_a_removal_below_zero_clamps_and_says_so(client: TestClient, db: FakeTable) -> None:
    res = client.post(
        "/admin/users/u2/credits",
        json={"delta": -100, "reason": "reversing a duplicate grant"},
        headers=admin_auth(),
    ).json()

    assert res["balance"] == 0
    assert res["clamped"] is True, "the founder must know the full delta wasn't applied"


# ── The willingness-to-pay signal ──────────────────────────────────────────


def test_a_user_can_ask_for_credits_and_it_is_recorded(client: TestClient, db: FakeTable) -> None:
    """The highest-value datum of the beta. It must not die in WhatsApp."""
    res = client.post(
        "/credits/request",
        json={"note": "out of credits, mid tune-up"},
        headers=auth(make_token()),
    )

    assert res.status_code == 200
    saved = [row for table, row in db.inserted if table == "credit_requests"]
    assert len(saved) == 1
    assert saved[0]["note"] == "out of credits, mid tune-up"


def test_the_ask_gets_a_real_commitment_not_a_platitude(client: TestClient, db: FakeTable) -> None:
    body = client.post("/credits/request", json={}, headers=auth(make_token())).json()

    assert "few hours" in body["message"]
    assert "be in touch" not in body["message"].lower()


def test_asking_needs_a_verified_token(client: TestClient, db: FakeTable) -> None:
    assert client.post("/credits/request", json={}).status_code == 401


def test_open_asks_are_listed_for_the_founder(client: TestClient, db: FakeTable) -> None:
    body = client.get("/admin/credit-requests", headers=admin_auth()).json()

    assert body["open_count"] == 1
    assert body["items"][0]["email"] == "low@x.com"
    assert body["items"][0]["note"] == "ran out"


# ── The runs log ───────────────────────────────────────────────────────────


def test_the_runs_log_puts_charged_next_to_status(client: TestClient, db: FakeTable) -> None:
    """Where never-charge-on-failure is VISIBLY true — no query required."""
    items = client.get("/admin/runs", headers=admin_auth()).json()["items"]

    failed = next(i for i in items if i["status"] == "failed")
    assert failed["charged"] == 0


def test_the_runs_log_computes_charge_rule_violations(client: TestClient, db: FakeTable) -> None:
    body = client.get("/admin/runs", headers=admin_auth()).json()

    assert body["charge_rule_violations"] == []
    assert body["charge_rule_ok"] is True


def test_a_charged_failure_is_reported_as_a_violation(client: TestClient, db: FakeTable) -> None:
    """Verified by breaking it: a failed run with a non-zero charge must be
    surfaced, not silently listed among the others."""
    db._data["agent_runs"].append(
        {
            "id": "bad",
            "user_id": "u1",
            "agent": "jobs",
            "status": "failed",
            "credits_charged": 15,
            "started_at": "2026-08-07T10:00:00Z",
        }
    )
    body = client.get("/admin/runs", headers=admin_auth()).json()

    assert body["charge_rule_ok"] is False
    assert [v["id"] for v in body["charge_rule_violations"]] == ["bad"]


def test_run_duration_is_computed_when_both_timestamps_exist(
    client: TestClient, db: FakeTable
) -> None:
    items = client.get("/admin/runs", headers=admin_auth()).json()["items"]
    done = next(i for i in items if i["id"] == "r1")
    unfinished = next(i for i in items if i["id"] == "r2")

    assert done["duration_ms"] == 12_000
    # Not zero — an unfinished run has no duration, and 0 would read as instant.
    assert unfinished["duration_ms"] is None


# ── System ─────────────────────────────────────────────────────────────────


def test_the_env_check_never_prints_a_secret(client: TestClient, db: FakeTable) -> None:
    """The operational question is "is it set?". Printing the value would put
    a secret in a browser tab and every screen-share."""
    body = client.get("/admin/system", headers=admin_auth()).json()
    raw = str(body)

    assert body["env"]["SUPABASE_SERVICE_KEY"]["set"] is True
    for secret in ("service-role-key", "test-anon-key", "test-anthropic-key"):
        assert secret not in raw, f"{secret} leaked into the system payload"


def test_the_env_check_gives_a_fingerprint_not_a_value(client: TestClient, db: FakeTable) -> None:
    """Enough to tell two keys apart when checking which is deployed; not
    enough to be a key."""
    hint = client.get("/admin/system", headers=admin_auth()).json()["env"]["ANTHROPIC_API_KEY"][
        "hint"
    ]

    assert hint is not None
    assert len(hint) <= 5


def test_an_unset_key_reports_false_rather_than_an_empty_string(
    client: TestClient, db: FakeTable
) -> None:
    body = client.get("/admin/system", headers=admin_auth()).json()
    assert body["env"]["RAZORPAY_KEY_ID"] == {"set": False, "hint": None}


def test_the_payment_flag_is_visible_in_system(client: TestClient, db: FakeTable) -> None:
    flags = client.get("/admin/system", headers=admin_auth()).json()["flags"]
    assert flags["payments_enabled"] is False
    assert flags["payments_live"] is False


# ── Resilience ─────────────────────────────────────────────────────────────


def test_one_unreadable_table_does_not_blank_the_whole_page(
    client: TestClient, db: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(FakeTable):
        def execute(self) -> Any:
            if self._name == "feedback":
                raise RuntimeError("permission denied")
            return super().execute()

    broken = Broken(db._data)
    monkeypatch.setattr(admin_routes, "service_client", lambda settings: broken)

    assert client.get("/admin/overview", headers=admin_auth()).status_code == 200
    assert client.get("/admin/feedback", headers=admin_auth()).json()["items"] == []
