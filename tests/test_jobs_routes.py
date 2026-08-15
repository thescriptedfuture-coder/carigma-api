"""The jobs feed over HTTP.

The fixtures are the THREE ROW GENERATIONS actually in `jobs_feed`, measured
from the live table rather than invented:

    v1_only     applyLinks, keyRequirements, cvTweaks — 10 rows have only this
    partial     + details.apply_url, details.source  — 17 more
    normalised  + apply_options, matched_skills, ...  — 66

A fixture that only carries the newest shape would test a table that does not
exist, and every honesty rule here is about what the OLDER rows must not be
made to claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import jobs as routes
from tests.conftest import USER_ID, auth, make_token

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
YESTERDAY = (NOW - timedelta(days=2)).isoformat()
TODAY = NOW.isoformat()

#: The oldest generation. No apply_url anywhere — only board searches.
V1_ONLY: dict[str, Any] = {
    "id": 1,
    "user_id": USER_ID,
    "title": "Business Analyst - ERP",
    "company": "TCS",
    "location": "Delhi",
    "job_type": "Full-time",
    "salary": "10-16 LPA",
    "match_score": 91,
    "why_match": "Five years of PFMS exposure maps onto their public-finance work.",
    "details": {
        "urgency": "high",
        "deadline": "Rolling",
        "keyRequirements": ["Government ERP", "Public finance workflows"],
        "applyLinks": {
            "shine": "https://www.shine.com/job-search/business-analyst-erp",
            "naukri": "https://www.naukri.com/business-analyst-jobs",
        },
    },
    "status": "active",
    "apply_kit": None,
    "first_seen": YESTERDAY,
    "last_seen": YESTERDAY,
}

#: The newest generation, fully normalised.
NORMALISED: dict[str, Any] = {
    "id": 2,
    "user_id": USER_ID,
    "title": "Data Analyst",
    "company": "Zomato",
    "location": "Gurugram",
    "job_type": None,
    "salary": None,
    "match_score": 82,
    "why_match": "SQL and Power BI depth maps onto their stack.",
    "details": {
        "source": "jsearch",
        "apply_url": "https://www.simplyhired.co.in/job/data-analyst-4412",
        "publisher": "SimplyHired",
        "posted_at": "2026-08-05T00:00:00Z",
        "salary_predicted": False,
        "matched_skills": ["SQL", "Power BI"],
        "missing_skills": ["dbt"],
        "apply_options": [
            {
                "publisher": "SimplyHired",
                "url": "https://www.simplyhired.co.in/job/data-analyst-4412",
                "is_direct": False,
            }
        ],
        "applyLinks": {"linkedin": "https://www.linkedin.com/jobs/search?q=data+analyst"},
    },
    "status": "active",
    "apply_kit": {"cv": {"name": "Ravi Kumar"}, "template": "classic"},
    "kit_created_at": TODAY,
    "first_seen": TODAY,
    "last_seen": TODAY,
}

DISMISSED: dict[str, Any] = {**V1_ONLY, "id": 3, "status": "dismissed", "match_score": 40}


class FakeTable:
    def __init__(self, db: FakeDB):
        self._db = db
        self._filters: dict[str, Any] = {}
        self._in: tuple[str, list[Any]] | None = None
        self._op: str | None = None
        self._payload: dict[str, Any] | None = None

    def select(self, *_a: Any, **_k: Any) -> FakeTable:
        self._op = "select"
        return self

    def update(self, payload: dict[str, Any]) -> FakeTable:
        self._op, self._payload = "update", payload
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def in_(self, column: str, values: list[Any]) -> FakeTable:
        self._in = (column, values)
        return self

    def order(self, *_a: Any, **_k: Any) -> FakeTable:
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        if any(str(row.get(k)) != str(v) for k, v in self._filters.items()):
            return False
        if self._in and row.get(self._in[0]) not in self._in[1]:
            return False
        return True

    def execute(self) -> Any:
        if self._db.fail:
            raise RuntimeError("db down")
        hits = [r for r in self._db.rows if self._matches(r)]
        if self._op == "update":
            for row in hits:
                row.update(self._payload or {})
        return type("Res", (), {"data": [dict(r) for r in hits]})()


class FakeDB:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = [dict(r) for r in (rows if rows is not None else [V1_ONLY, NORMALISED])]
        self.fail = False

    def table(self, _name: str) -> FakeTable:
        return FakeTable(self)


@pytest.fixture
def wired(client: TestClient, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    db = FakeDB()
    monkeypatch.setattr(routes, "_db", lambda request, settings: db)
    return client, db


# ── Auth ───────────────────────────────────────────────────────────────────


def test_every_jobs_endpoint_requires_a_verified_token(client: TestClient) -> None:
    assert client.get("/jobs/feed").status_code == 401
    assert client.get("/jobs/1").status_code == 401
    assert client.get("/jobs/1/apply-kit").status_code == 401
    assert client.post("/jobs/1/not-for-me", json={"reason": "x"}).status_code == 401


# ── The feed ───────────────────────────────────────────────────────────────


def test_the_feed_returns_this_users_stored_jobs(wired) -> None:  # type: ignore[no-untyped-def]
    client, _db = wired

    res = client.get("/jobs/feed", headers=auth(make_token()))

    assert res.status_code == 200
    body = res.json()
    assert [item["id"] for item in body["items"]] == [1, 2], "best match first"
    assert body["scan"]["surfaced"] == 2


def test_a_dismissed_job_does_not_come_back(wired) -> None:  # type: ignore[no-untyped-def]
    """ "Not for me" is a decision. Re-showing it makes the feedback pointless."""
    client, db = wired
    db.rows.append(dict(DISMISSED))

    body = client.get("/jobs/feed", headers=auth(make_token())).json()

    assert 3 not in [item["id"] for item in body["items"]]


def test_a_failed_read_is_not_reported_as_an_empty_feed(wired) -> None:  # type: ignore[no-untyped-def]
    """An empty feed is a real answer — no matches yet. Showing it when we
    could not look tells the user their scan found nothing."""
    client, db = wired
    db.fail = True

    res = client.get("/jobs/feed", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"


def test_an_unscored_job_sorts_last_rather_than_as_zero(wired) -> None:  # type: ignore[no-untyped-def]
    client, db = wired
    db.rows.append({**V1_ONLY, "id": 9, "match_score": None})

    items = client.get("/jobs/feed", headers=auth(make_token())).json()["items"]

    assert items[-1]["id"] == 9
    assert items[-1]["match_score"] is None


# ── The honesty rules the OLD rows depend on ───────────────────────────────


def test_a_board_search_is_never_served_as_an_apply_link(wired) -> None:  # type: ignore[no-untyped-def]
    """V1's bug 1.2. `applyLinks` is a dict keyed by job board for ONE job —
    six boards do not host six postings of one role, so those are searches.
    Serving them as `apply_url` would reproduce the bug on real user data."""
    client, _db = wired

    old = next(
        i
        for i in client.get("/jobs/feed", headers=auth(make_token())).json()["items"]
        if i["id"] == 1
    )

    assert old["apply_url"] is None, "a search was served as the apply link"
    assert [link["publisher"] for link in old["search_links"]] == ["shine", "naukri"]


def test_a_row_never_analysed_for_skills_says_so_rather_than_showing_none(wired) -> None:  # type: ignore[no-untyped-def]
    """`[]` means "we compared and found no overlap". `None` means "this row
    predates that analysis". Twenty-seven real rows are entitled to the second."""
    client, _db = wired
    items = {
        i["id"]: i for i in client.get("/jobs/feed", headers=auth(make_token())).json()["items"]
    }

    assert items[1]["matched_skills"] is None
    assert items[1]["missing_skills"] is None
    assert items[2]["matched_skills"] == ["SQL", "Power BI"]


def test_an_empty_skill_list_survives_as_an_empty_list(wired) -> None:  # type: ignore[no-untyped-def]
    """The conflation in the other direction. A real "no overlap" result must
    not be flattened into "never analysed"."""
    client, db = wired
    db.rows.append(
        {**NORMALISED, "id": 7, "details": {**NORMALISED["details"], "matched_skills": []}}
    )

    items = {
        i["id"]: i for i in client.get("/jobs/feed", headers=auth(make_token())).json()["items"]
    }

    assert items[7]["matched_skills"] == []


def test_a_missing_salary_is_absent_not_invented(wired) -> None:  # type: ignore[no-untyped-def]
    client, _db = wired
    items = {
        i["id"]: i for i in client.get("/jobs/feed", headers=auth(make_token())).json()["items"]
    }

    assert items[2]["salary"] is None
    assert items[2]["job_type"] is None


def test_a_row_with_no_prediction_flag_is_not_declared_published(wired) -> None:  # type: ignore[no-untyped-def]
    """Absence of `salary_predicted` is not evidence the figure was published."""
    client, _db = wired
    items = {
        i["id"]: i for i in client.get("/jobs/feed", headers=auth(make_token())).json()["items"]
    }

    assert items[1]["salary_is_estimated"] is None
    assert items[2]["salary_is_estimated"] is False


def test_the_scan_line_says_nothing_when_no_row_carries_a_timestamp() -> None:
    """ "Never scanned" and "scanned long ago" are different, and a surface
    reading a default date would say the second."""
    from carigma_api.services.jobs.feed import scan_summary

    summary = scan_summary([{"title": "x"}], today=NOW.date())

    assert summary["last_scan_at"] is None


# ── One job, and the apply kit ─────────────────────────────────────────────


def test_one_job_returns_the_same_shape_as_the_feed(wired) -> None:  # type: ignore[no-untyped-def]
    """A success case, deliberately.

    The first version of this file tested only the 404, which left the success
    payload unasserted and absent from the contract manifest — a fresh instance
    of the UNVERIFIED category the audit had just found two of.
    """
    client, _db = wired

    res = client.get("/jobs/2", headers=auth(make_token()))

    assert res.status_code == 200
    body = res.json()
    assert body["id"] == 2
    assert body["apply_url"].startswith("https://")
    assert body["matched_skills"] == ["SQL", "Power BI"]


def test_another_users_job_is_a_404_not_a_403(wired) -> None:  # type: ignore[no-untyped-def]
    """Telling a caller an id exists but is not theirs is itself a disclosure."""
    client, db = wired
    db.rows.append({**V1_ONLY, "id": 55, "user_id": "22222222-2222-2222-2222-222222222222"})

    assert client.get("/jobs/55", headers=auth(make_token())).status_code == 404


def test_the_apply_kit_returns_what_was_stored(wired) -> None:  # type: ignore[no-untyped-def]
    client, _db = wired

    res = client.get("/jobs/2/apply-kit", headers=auth(make_token()))

    assert res.status_code == 200
    assert res.json()["template"] == "classic"
    assert "you verify" in res.json()["disclaimer"]


def test_a_job_with_no_kit_says_so_rather_than_generating_one(wired) -> None:  # type: ignore[no-untyped-def]
    """Nine of ninety-three rows have a kit. Conjuring one on read would be
    unpaid work presented as a record of work already done."""
    client, _db = wired

    res = client.get("/jobs/1/apply-kit", headers=auth(make_token()))

    assert res.status_code == 404
    assert res.json()["detail"]["error"] == "no_kit"


# ── Not for me ─────────────────────────────────────────────────────────────


def test_the_dismissal_reason_is_actually_stored(wired) -> None:  # type: ignore[no-untyped-def]
    """V1's bug B: the reason did not save, which made the gesture theatre.

    The response is read back from the ROW, not echoed from the request — only
    the row can prove the difference between "recorded" and "accepted and
    dropped".
    """
    client, db = wired

    res = client.post(
        "/jobs/1/not-for-me", json={"reason": "Salary too low"}, headers=auth(make_token())
    )

    assert res.status_code == 200
    assert res.json()["dismiss_reason"] == "Salary too low"
    assert db.rows[0]["dismiss_reason"] == "Salary too low"
    assert db.rows[0]["status"] == "dismissed"


def test_a_failed_dismissal_says_the_job_is_unchanged(wired) -> None:  # type: ignore[no-untyped-def]
    client, db = wired

    original = FakeDB.table

    def fail_on_update(self: FakeDB, name: str) -> Any:
        table = original(self, name)
        real_execute = table.execute

        def execute() -> Any:
            if table._op == "update":
                raise RuntimeError("write failed")
            return real_execute()

        table.execute = execute  # type: ignore[method-assign]
        return table

    FakeDB.table = fail_on_update  # type: ignore[method-assign]
    try:
        res = client.post(
            "/jobs/1/not-for-me", json={"reason": "Salary too low"}, headers=auth(make_token())
        )
    finally:
        FakeDB.table = original  # type: ignore[method-assign]

    assert res.status_code == 503
    assert "unchanged" in res.json()["detail"]["message"]
    assert db.rows[0]["status"] == "active"


def test_a_blank_reason_is_refused(wired) -> None:  # type: ignore[no-untyped-def]
    client, db = wired

    res = client.post("/jobs/1/not-for-me", json={"reason": ""}, headers=auth(make_token()))

    assert res.status_code == 422
    assert db.rows[0]["status"] == "active"


# ── Nothing on this path charges ───────────────────────────────────────────


def test_no_jobs_endpoint_charges_credits() -> None:
    """Reading a feed is not a run. Asserted against the module's AST — string
    constants included, because a table name is a literal and the
    identifier-only version of this guard once passed with
    `db.table('credits').update(...)` sitting on a free path."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(routes))
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            referenced.add(node.value)

    for forbidden in ("credits", "credit_ledger", "charge_for_result", "apply_delta", "execute"):
        if forbidden == "execute":
            continue  # PostgREST's `.execute()`, not the run protocol's
        assert forbidden not in referenced, f"{forbidden!r} appears on the free jobs path"
