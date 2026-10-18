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

#: REAL now, not a fixed date. The feed folds postings with no sign of life
#: for three weeks into `older`, measured from the moment the request is
#: served — so a fixture dated in August is, correctly, history by October.
NOW = datetime.now(UTC).replace(microsecond=0)
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
    # The shape nine live rows actually carry, measured from the table rather
    # than invented: V1 stored a whole CV, a gap list and an outreach block.
    # The web declared `cv_id`, `ats` and a differently-named `outreach`, and
    # the CV tab threw on `data.ats.coverage_pct`.
    "apply_kit": {
        "cv": {
            "name": "Ravi Kumar",
            "email": "ravi@example.com",
            "phone": "+91 XXXXX XXXXX",
            "location": "Delhi, India",
            "tagline": "Data Analyst | Power BI & SQL",
            "summary": "Delhi-based analyst turning operational data into decisions.",
            "coreSkills": ["SQL", "Power BI"],
            # OBJECTS, measured across all nine stored kits. This fixture had
            # strings, so the recorded contract described a CV nobody stores —
            # and the web's page, typed from that contract, rendered an object
            # as a React child and blanked `/jobs/{}/cv` for every job that
            # HAS a kit. The nine rows that work were the nine that crashed.
            "experience": [
                {
                    "title": "Data Analyst",
                    "company": "Zomato",
                    "location": "Gurugram",
                    "duration": "2021 — now",
                    "bullets": ["Built the cost dashboard that changed a budget line."],
                    "keyImpact": "Cut reporting time 40%.",
                }
            ],
            # `grade` is "" on every live row — a real empty string, not a
            # missing key, and the surface has to render around it.
            "education": [
                {
                    "degree": "B.Tech, Computer Science",
                    "institution": "Delhi Technological University",
                    "year": "2014 — 2018",
                    "grade": "",
                }
            ],
            "certifications": [],
            "keyAchievements": ["Cut reporting time 40%"],
        },
        "gaps": ["dbt", "Airflow", "Looker"],
        "outreach": {
            "bestChannel": "LinkedIn",
            "messageTemplate": "Hi — I saw the Data Analyst role...",
            "followUpTip": "Follow up after four working days.",
        },
        "template": "classic",
    },
    "kit_created_at": TODAY,
    "first_seen": TODAY,
    "last_seen": TODAY,
}

DISMISSED: dict[str, Any] = {**V1_ONLY, "id": 3, "status": "dismissed", "match_score": 40}


class FakeTable:
    def __init__(self, db: FakeDB, table: str = "jobs_feed"):
        self._db = db
        self._table = table
        self._filters: dict[str, Any] = {}
        self._in: tuple[str, list[Any]] | None = None
        self._gte: tuple[str, Any] | None = None
        self._op: str | None = None
        self._payload: dict[str, Any] | None = None

    def select(self, *_a: Any, **_k: Any) -> FakeTable:
        self._op = "select"
        return self

    def update(self, payload: dict[str, Any]) -> FakeTable:
        self._op, self._payload = "update", payload
        return self

    def insert(self, payload: Any) -> FakeTable:
        # A scan INSERTS new postings. Without this the fake raised
        # AttributeError inside the run, which `runs.execute` reports as a
        # failed run — so the success path could not be tested at all.
        self._op, self._payload = "insert", payload
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def in_(self, column: str, values: list[Any]) -> FakeTable:
        self._in = (column, values)
        return self

    def order(self, *_a: Any, **_k: Any) -> FakeTable:
        return self

    def gte(self, column: str, value: Any) -> FakeTable:
        # The quota count filters on `fetched_at >= midnight`. Without this the
        # chain raised, `fetches_today` read that as "cannot count", and the
        # cap test passed on the WRONG refusal — right status, wrong reason.
        self._gte = (column, value)
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        if any(str(row.get(k)) != str(v) for k, v in self._filters.items()):
            return False
        if self._in and row.get(self._in[0]) not in self._in[1]:
            return False
        if self._gte and str(row.get(self._gte[0], "")) < str(self._gte[1]):
            return False
        return True

    def execute(self) -> Any:
        if self._db.fail:
            raise RuntimeError("db down")
        # The call log and the run history are kept apart from the feed, so a
        # scan's bookkeeping is never mistaken for a posting.
        source = (
            self._db.ledger
            if self._table == "jobs_provider_calls"
            else self._db.runs
            if self._table == "agent_runs"
            else self._db.rows
        )
        if self._op == "insert":
            added = self._payload if isinstance(self._payload, list) else [self._payload]
            # Echoed back WITH an id, as Postgres does — a scan names its new
            # rows by the id the database gave them.
            echoed = []
            for row in added:
                self._db.next_id += 1
                echoed.append({"id": self._db.next_id, **row})
            source.extend(dict(r) for r in echoed)
            self._db.inserted.extend(dict(r) for r in added)
            return type("Res", (), {"data": [dict(r) for r in echoed]})()
        hits = [r for r in source if self._matches(r)]
        if self._op == "update":
            for row in hits:
                row.update(self._payload or {})
        return type("Res", (), {"data": [dict(r) for r in hits]})()


class _Rpc:
    def __init__(self, value: Any) -> None:
        self._value = value

    def execute(self) -> Any:
        return type("Res", (), {"data": self._value})()


class FakeDB:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = [dict(r) for r in (rows if rows is not None else [V1_ONLY, NORMALISED])]
        #: What `jsearch_calls_today()` returns. The real function adds V1's
        #: fetches to V2's call log; here it is just the number.
        self.calls_today: Any = 0
        self.ledger: list[dict[str, Any]] = []
        # Previous scans, for the six-hour gate.
        self.runs: list[dict[str, Any]] = []
        self.inserted: list[dict[str, Any]] = []
        self.next_id = 500
        self.fail = False

    def table(self, name: str) -> FakeTable:
        # Production refuses a user-scoped read of `jobs_cache` by returning
        # NOTHING, and a fake that answered it is how the cap passed every
        # test while enforcing nothing. Nothing on the user path may name it.
        assert name != "jobs_cache", "jobs_cache is service-role only (V2_007)"
        return FakeTable(self, name)

    def rpc(self, name: str, _params: dict[str, Any] | None = None) -> _Rpc:
        assert name == "jsearch_calls_today", name
        return _Rpc(self.calls_today)


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

    summary = scan_summary([{"title": "x"}], now=NOW)

    assert summary["last_scan_at"] is None


# ── One job, and the apply kit ─────────────────────────────────────────────


def test_a_job_from_before_the_skills_analysis_sends_null_not_an_empty_list(
    wired,  # type: ignore[no-untyped-def]
) -> None:
    """Twenty-seven of the ninety-three live rows have no `matched_skills` key
    inside `details` at all — they predate that analysis. Measured, not assumed.

    The payload must carry `null` for those, and `[]` only when the comparison
    RAN and found nothing (seventeen rows are in that state). Collapsing the two
    would tell a user we compared their skills and found no overlap, when we
    never compared.

    This is also the case the web blanked on: `null.map(...)` unmounts the
    detail page. The contract records the nullability here, and the web's
    fixtures guard requires a fixture in this state.
    """
    client, _db = wired

    body = client.get("/jobs/1", headers=auth(make_token())).json()

    assert body["matched_skills"] is None, "an unanalysed row must not claim an empty comparison"
    assert body["missing_skills"] is None
    # The one key every generation carries.
    assert body["key_requirements"] == ["Government ERP", "Public finance workflows"]


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


# ── What may charge, and what may not ──────────────────────────────────────


def test_only_the_scan_charges_anything() -> None:
    """Reading a feed is not a run, and neither is downloading a CV already
    paid for. One endpoint in this module costs credits; the rest must not be
    able to.

    Asserted per FUNCTION rather than per module. The whole-module version was
    right until `POST /jobs/scan` existed, and the lazy repair — delete the
    guard — would have taken the protection off five free endpoints to admit
    one priced one.

    Still an AST walk including STRING CONSTANTS, because a table name is a
    literal: the identifier-only version of this guard once passed with
    `db.table('credits').update(...)` sitting on a free path.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(routes))

    #: The priced endpoints, and what prices them. `build_apply_kit` joined in
    #: S7: the CV Architect, 15 credits, V1's price.
    PRICED = {"scan", "build_apply_kit"}
    MONEY = ("credits", "credit_ledger", "charge_for_result", "apply_delta")

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name in PRICED or node.name.startswith("_"):
            continue

        referenced: set[str] = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name):
                referenced.add(inner.id)
            elif isinstance(inner, ast.Attribute):
                referenced.add(inner.attr)
            elif isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                referenced.add(inner.value)

        for forbidden in MONEY:
            if forbidden in referenced:
                offenders.append(f"{node.name} references {forbidden!r}")

    assert offenders == [], "these are free endpoints and must stay free: " + "; ".join(offenders)


def test_the_priced_list_names_an_endpoint_that_exists() -> None:
    """An exemption needs a proof. `scan` naming nothing would quietly exempt
    a function that no longer exists while the real one went unchecked."""
    import inspect

    for name in ("scan", "build_apply_kit"):
        assert inspect.iscoroutinefunction(getattr(routes, name)), name


# ── The CV as a file ───────────────────────────────────────────────────────


def test_the_cv_downloads_as_a_pdf_named_after_the_person(wired) -> None:  # type: ignore[no-untyped-def]
    """V2 could preview a CV and not produce one. `Download PDF` was removed
    when it turned out to have no handler, so the artifact most likely to be
    sent to an employer could be read on screen and nowhere else."""
    client, _db = wired

    res = client.get("/jobs/2/cv.pdf", headers=auth(make_token()))

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert res.content.startswith(b"%PDF-")
    # The name reaches a downloads folder, so it carries the person.
    assert 'filename="Ravi-Kumar-Modern-CV.pdf"' in res.headers["content-disposition"]


def test_a_template_choice_is_honoured_and_reported_back(wired) -> None:  # type: ignore[no-untyped-def]
    client, _db = wired

    res = client.get("/jobs/2/cv.pdf?template=executive", headers=auth(make_token()))

    assert res.status_code == 200
    assert res.headers["x-carigma-template"] == "executive"
    assert "Executive" in res.headers["content-disposition"]


def test_an_unknown_template_returns_the_default_rather_than_an_error(wired) -> None:  # type: ignore[no-untyped-def]
    """A stale link should hand someone a CV, not an error about a layout they
    never chose."""
    client, _db = wired

    res = client.get("/jobs/2/cv.pdf?template=sideways", headers=auth(make_token()))

    assert res.status_code == 200
    assert res.headers["x-carigma-template"] == "modern"


def test_a_job_with_no_kit_is_a_404_not_an_empty_document(wired) -> None:  # type: ignore[no-untyped-def]
    """Eighty-four of ninety-three rows have no kit. A PDF rendered from
    nothing is a page with a name on it and no career, and it would arrive
    looking like a product that works."""
    client, _db = wired

    res = client.get("/jobs/1/cv.pdf", headers=auth(make_token()))

    assert res.status_code == 404
    assert res.json()["detail"]["error"] == "no_kit"


def test_the_download_needs_a_verified_token(client: TestClient) -> None:
    assert client.get("/jobs/2/cv.pdf").status_code == 401


# ── The scan ───────────────────────────────────────────────────────────────


@pytest.fixture
def scanning(wired, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A scan with every outside edge replaced: no provider, no model, no money."""
    client, db = wired

    class FakeGateway:
        provider_names = ("jsearch", "adzuna")

        def __init__(self) -> None:
            self.found: list[Any] = []
            self.notes: list[str] = ["jsearch: 2"]
            self.calls = 0

        def search(self, **_kw: Any) -> tuple[list[Any], list[str]]:
            self.calls += 1
            return self.found, self.notes

    gateway = FakeGateway()
    # `build_gateway` returns the gateway AND the recorded providers, so the
    # scan can say whether it was answered from the cache.
    monkeypatch.setattr(routes, "build_gateway", lambda settings, db, user_id: gateway)
    monkeypatch.setattr(routes, "_scorer", lambda settings: lambda *_a, **_k: {"items": []})
    monkeypatch.setattr(routes, "ProfileRepository", lambda client: _FakeProfiles())
    return client, db, gateway


class _FakeProfiles:
    profile: dict[str, Any] = {"targetRoles": "Data Analyst", "location": "Delhi"}

    def load(self, _user_id: str) -> dict[str, Any]:
        return dict(self.profile)


def test_the_scan_needs_a_verified_token(client: TestClient) -> None:
    assert client.post("/jobs/scan").status_code == 401


def test_a_profile_with_no_target_role_is_refused_before_any_quota_is_spent(scanning) -> None:  # type: ignore[no-untyped-def]
    """Scanning for "" returns whatever the provider feels like, and finding
    that out costs a search that belongs to live V1 users."""
    client, _db, gateway = scanning
    _FakeProfiles.profile = {"targetRoles": "", "location": "Delhi"}
    try:
        res = client.post("/jobs/scan", headers=auth(make_token()))
    finally:
        _FakeProfiles.profile = {"targetRoles": "Data Analyst", "location": "Delhi"}

    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "no_target_role"
    assert gateway.calls == 0


def test_the_shared_quota_is_checked_before_credits_and_before_the_provider(scanning) -> None:  # type: ignore[no-untyped-def]
    """V1 and V2 draw on one JSearch key and live V1 users have first claim.

    You should not have to be able to afford something to be told it is
    impossible — and a scan that would exceed the cap must not reach a
    provider at all.
    """
    client, db, gateway = scanning
    db.calls_today = 25

    res = client.post("/jobs/scan", headers=auth(make_token()))

    assert res.status_code == 429
    assert res.json()["detail"]["error"] == "quota_reached"
    # The NUMBER, not just the refusal. Both refusals — "cap reached" and
    # "could not count" — say nothing was charged, so asserting that alone
    # would pass on a fake whose cache read simply threw.
    assert "25 of 25" in res.json()["detail"]["message"]
    assert gateway.calls == 0


class _FakeCredits:
    """Enough of a credit store to let a run settle, so the SUCCESS path is
    exercised rather than only its refusals.

    A surface covered entirely by its refusals looks better tested than one
    covered by its successes, because refusal tests are easy and there are more
    of them. This is the one that records what a scan actually returns.
    """

    def __init__(self) -> None:
        self.charged: list[tuple[str, int]] = []
        self.balance = 500

    def read_balance(self, _user_id: str) -> int:
        return self.balance

    def apply_delta(
        self, user_id: str, delta: int, reason: str, balance_after: int | None = None, **_kw: Any
    ) -> int:
        # The real signature. This took three positional arguments while
        # `charge_for_result` passes four, so every charge raised, was caught
        # and logged "delivering unbilled" — and the test named "charges once"
        # passed without ever asserting a charge. Found in S7.
        self.charged.append((reason, delta))
        self.balance += delta
        return self.balance


def test_a_scan_that_finds_jobs_saves_them_and_charges_once(scanning, monkeypatch) -> None:  # type: ignore[no-untyped-def]
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
    monkeypatch.setattr(
        routes,
        "_scorer",
        lambda settings: (
            lambda *_a, **_k: {"items": [{"id": 0, "match_score": 82, "why_match": "SQL."}]}
        ),
    )

    res = client.post("/jobs/scan", headers=auth(make_token()))

    assert res.status_code == 200
    body = res.json()
    assert body["found"] == 1
    assert body["new"] == 1
    assert body["seen_again"] == 0
    assert body["scored"] == 1
    assert body["sources"] == ["jsearch", "adzuna"]
    # The half of the name this test never checked.
    assert credits.charged == [("Career Scout run", -15)]


def test_a_search_that_matched_nothing_is_an_answer_and_is_free(scanning, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A source ANSWERED and found nothing. That is "no postings for this role
    in this city today" — a real result, said as one, and free.

    It used to raise, and the surface said "We couldn't search for roles just
    now", which is a different fact and a false one.
    """
    client, _db, gateway = scanning
    credits = _FakeCredits()
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: credits)
    gateway.found = []
    gateway.notes = ["jsearch: 0", "adzuna: 0"]

    res = client.post("/jobs/scan", headers=auth(make_token()))

    assert res.status_code == 200
    body = res.json()
    assert body["found"] == 0
    assert body["searched_for"] == {"role": "Data Analyst", "city": "Delhi"}
    assert credits.charged == []


def test_a_search_nobody_answered_is_a_failure_and_says_so(scanning, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Every source down is NOT "no jobs". The ProviderUnavailable rule:
    "nothing to report" and "we couldn't look" must never render the same."""
    client, _db, gateway = scanning
    credits = _FakeCredits()
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: credits)
    gateway.found = []
    gateway.notes = ["jsearch: unavailable", "adzuna: failed"]

    res = client.post("/jobs/scan", headers=auth(make_token()))

    assert res.status_code == 503
    assert "couldn't reach the job sources" in res.json()["detail"]["message"]
    assert credits.charged == []


def test_a_dead_model_still_saves_the_real_postings(scanning, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """V1's answer here was a fabricated 70% on every job. A real posting is
    worth showing without the fit copy; the absence must look like absence."""
    from carigma_api.services.jobs.models import NormalizedJob

    client, db, gateway = scanning
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: _FakeCredits())
    gateway.found = [
        NormalizedJob(
            source="jsearch",
            source_id="1",
            title="Data Analyst",
            company="Zomato",
            location="Gurugram, India",
            apply_url="https://example.com/1",
        )
    ]

    def dead(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("anthropic is down")

    monkeypatch.setattr(routes, "_scorer", lambda settings: dead)

    body = client.post("/jobs/scan", headers=auth(make_token())).json()

    assert body["found"] == 1
    assert body["new"] == 1
    # Saved, and honestly unscored.
    assert body["scored"] == 0


# ── One scan per six hours (spec 3.4) ──────────────────────────────────────


def _scan_run(hours_ago: float, status: str = "succeeded") -> dict[str, Any]:
    return {
        "user_id": USER_ID,
        "agent": "jobs",
        "status": status,
        "finished_at": (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(),
    }


def test_a_rescan_inside_six_hours_is_refused_free_with_the_time_it_opens(
    scanning, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """V1 enforced this; the first V2 scan did not. The second real scan ran
    six minutes after the first, cost 15 credits, and found one posting."""
    client, db, gateway = scanning
    credits = _FakeCredits()
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: credits)
    db.runs = [_scan_run(hours_ago=0.1)]

    res = client.post("/jobs/scan", headers=auth(make_token()))

    assert res.status_code == 429
    detail = res.json()["detail"]
    assert detail["error"] == "scanned_recently"
    assert detail["next_scan_at"]
    assert "Nothing was charged" in detail["message"]
    assert gateway.calls == 0
    assert credits.charged == []


def test_after_six_hours_the_scan_opens_again(scanning, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from carigma_api.services.jobs.models import NormalizedJob

    client, db, gateway = scanning
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: _FakeCredits())
    db.runs = [_scan_run(hours_ago=7)]
    gateway.found = [
        NormalizedJob(
            source="jsearch",
            source_id="1",
            title="Data Analyst",
            company="Zomato",
            location="Gurugram",
            apply_url="https://example.com/1",
        )
    ]

    assert client.post("/jobs/scan", headers=auth(make_token())).status_code == 200


def test_a_failed_scan_does_not_start_the_clock(scanning, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """It charged nothing and found nothing. Making someone wait six hours
    after a provider timeout would punish them for our bad day."""
    from carigma_api.services.jobs.models import NormalizedJob

    client, db, gateway = scanning
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: _FakeCredits())
    db.runs = [_scan_run(hours_ago=0.1, status="failed"), _scan_run(hours_ago=0.2, status="empty")]
    gateway.found = [
        NormalizedJob(
            source="jsearch",
            source_id="1",
            title="Data Analyst",
            company="Zomato",
            location="Gurugram",
            apply_url="https://example.com/1",
        )
    ]

    assert client.post("/jobs/scan", headers=auth(make_token())).status_code == 200


# ── New today, and older postings ──────────────────────────────────────────


def _posting(id_: int, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        **NORMALISED,
        "id": id_,
        "apply_kit": None,
        "kit_created_at": None,
        "details": {"source": "jsearch", "apply_url": f"https://example.com/{id_}"},
    }
    row.update(kw)
    return row


def _ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def test_new_today_means_first_seen_in_the_last_24_hours() -> None:
    """Spec 3.4 and V1. The first V2 version compared UTC calendar dates, so a
    scan at 04:30 IST (23:00 UTC the day before) was never "new today"."""
    from carigma_api.services.jobs.feed import present_feed

    now = datetime(2026, 10, 18, 1, 0, tzinfo=UTC)
    rows = [
        _posting(1, first_seen="2026-10-17T23:00:00+00:00", last_seen="2026-10-17T23:00:00+00:00"),
        _posting(2, first_seen="2026-10-16T23:30:00+00:00", last_seen="2026-10-17T23:00:00+00:00"),
    ]
    flags = {i["id"]: i["is_new_today"] for i in present_feed(rows, now=now)["items"]}

    assert flags == {1: True, 2: False}


def test_postings_with_no_sign_of_life_for_three_weeks_fold_into_older(wired) -> None:  # type: ignore[no-untyped-def]
    """Ravi's feed held 58 active postings, 45 of them unseen since July and
    August, and the thirteen live ones were scattered through it."""
    client, db = wired
    db.rows = [
        # The shape most folded rows really have: V1-era, board searches only.
        _posting(
            1,
            first_seen=_ago(days=60),
            last_seen=_ago(days=60),
            details={"applyLinks": {"naukri": "https://www.naukri.com/business-analyst-jobs"}},
        ),
        _posting(2, first_seen=_ago(days=60), last_seen=_ago(days=1)),
        _posting(3, first_seen=_ago(hours=2), last_seen=_ago(hours=2)),
    ]

    body = client.get("/jobs/feed", headers=auth(make_token())).json()

    assert [i["id"] for i in body["older"]] == [1]
    assert sorted(i["id"] for i in body["items"]) == [2, 3], "seen yesterday is still alive"
    assert body["older_after_days"] == 21
    assert body["scan"]["surfaced"] == 2, "the header must agree with the list on screen"


def test_a_recent_posting_date_keeps_a_posting_the_scans_have_not_returned(wired) -> None:  # type: ignore[no-untyped-def]
    client, db = wired
    db.rows = [
        _posting(
            1,
            first_seen=_ago(days=40),
            last_seen=_ago(days=40),
            details={"source": "jsearch", "posted_at": _ago(days=5)},
        )
    ]

    body = client.get("/jobs/feed", headers=auth(make_token())).json()

    assert [i["id"] for i in body["items"]] == [1]


def test_the_sources_closing_date_decides_when_it_gave_one(wired) -> None:  # type: ignore[no-untyped-def]
    """Ravi's rule: when we know how long a posting is open, keep it exactly
    that long — in both directions."""
    client, db = wired
    db.rows = [
        # Not seen for two months, but the source says it is open another month.
        _posting(
            1,
            last_seen=_ago(days=60),
            first_seen=_ago(days=60),
            details={"expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
        ),
        # Seen an hour ago, but the source says it closed yesterday.
        _posting(
            2,
            last_seen=_ago(hours=1),
            first_seen=_ago(hours=1),
            details={"expires_at": _ago(days=1)},
        ),
    ]

    body = client.get("/jobs/feed", headers=auth(make_token())).json()

    assert [i["id"] for i in body["items"]] == [1]
    assert [i["id"] for i in body["older"]] == [2]


def test_saved_postings_and_paid_kits_never_fold(wired) -> None:  # type: ignore[no-untyped-def]
    client, db = wired
    db.rows = [
        _posting(1, status="saved", first_seen=_ago(days=90), last_seen=_ago(days=90)),
        _posting(
            2,
            kit_created_at=_ago(days=80),
            apply_kit={"cv": {"name": "x"}},
            first_seen=_ago(days=90),
            last_seen=_ago(days=90),
        ),
    ]

    body = client.get("/jobs/feed", headers=auth(make_token())).json()

    assert sorted(i["id"] for i in body["items"]) == [1, 2]
    assert body["older"] == []


def test_a_posting_with_no_dates_at_all_stays_because_hiding_needs_a_reason(wired) -> None:  # type: ignore[no-untyped-def]
    client, db = wired
    db.rows = [_posting(1, first_seen=None, last_seen=None)]

    body = client.get("/jobs/feed", headers=auth(make_token())).json()

    assert [i["id"] for i in body["items"]] == [1]


# ── The scan names what is new ─────────────────────────────────────────────


def test_a_scan_names_the_new_postings_so_the_surface_can_point_at_them(
    scanning, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """ "3 new roles — marked New today below" pointed at three rows scored
    18, 45 and 48, which the score sort had put mid-list. A name is where to
    look; a count is only that something happened."""
    from carigma_api.services.jobs.models import NormalizedJob

    client, db, gateway = scanning
    db.rows = []
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: _FakeCredits())
    gateway.found = [
        NormalizedJob(
            source="jsearch",
            source_id="1",
            title="Data Analyst",
            company="Zomato",
            location="Gurugram",
            apply_url="https://example.com/1",
        )
    ]
    monkeypatch.setattr(
        routes,
        "_scorer",
        lambda settings: lambda *_a, **_k: {"items": [{"id": 0, "match_score": 61}]},
    )

    body = client.post("/jobs/scan", headers=auth(make_token())).json()

    assert body["new_jobs"] == [
        {"id": db.rows[0]["id"], "title": "Data Analyst", "company": "Zomato", "match_score": 61}
    ]
    # And the posting kept its apply link through the scoring write.
    assert db.rows[0]["details"]["apply_url"] == "https://example.com/1"
    assert db.rows[0]["match_score"] == 61


def test_the_six_hour_wait_is_said_as_a_wait_not_a_utc_clock(scanning, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """ "Opens at 10:47 UTC" is a sum an IST reader has to do, and gets wrong."""
    client, db, _gateway = scanning
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: _FakeCredits())
    db.runs = [_scan_run(hours_ago=2)]

    message = client.post("/jobs/scan", headers=auth(make_token())).json()["detail"]["message"]

    assert "finished about 2 hours ago" in message
    assert "opens in about 4 hours" in message
    assert "UTC" not in message


# ── CV Architect: building a kit ───────────────────────────────────────────

KIT_PROFILE: dict[str, Any] = {
    "name": "Ravi Kumar",
    "location": "Delhi NCR",
    "experience": "Controller General Of Defence Accounts (CGDA) | Senior Auditor. "
    "Led the PFMS rollout.",
    "skills": "SQL, Power BI, Python",
    "certifications": "Google Data Analytics",
    "cvTemplate": "classic",
}


def _cv_reply(**cv: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "tagline": "Audit-trained analyst",
        "summary": "Years of reconciling government accounts, now applied to analytics.",
        "coreSkills": ["SQL", "Power BI"],
        "experience": [
            {
                "title": "Senior Auditor",
                "company": "CGDA",
                "duration": "2016 — now",
                "bullets": ["Led the PFMS rollout"],
                "keyImpact": "Made reconciliation a report.",
            }
        ],
        "education": [],
        "certifications": ["Google Data Analytics"],
        "keyAchievements": ["Led the PFMS rollout"],
    }
    body.update(cv)
    return {"cv": body, "gaps": ["Government ERP"], "outreach": {"bestChannel": "LinkedIn"}}


class _KitProfiles:
    profile: dict[str, Any] = KIT_PROFILE
    fails = False

    def __init__(self, _client: Any) -> None: ...

    def load(self, _user_id: str) -> dict[str, Any]:
        if _KitProfiles.fails:
            raise RuntimeError("profiles is down")
        return dict(_KitProfiles.profile)


@pytest.fixture
def tailoring(wired, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    client, db = wired
    credits = _FakeCredits()
    calls: list[str] = []
    state: dict[str, Any] = {"reply": _cv_reply(), "dead": False}

    def writer(_system: str, user: str, _max: int) -> Any:
        calls.append(user)
        if state["dead"]:
            from carigma_api.services.ai import UpstreamError

            raise UpstreamError("anthropic is down")
        return state["reply"]

    _KitProfiles.profile, _KitProfiles.fails = KIT_PROFILE, False
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: credits)
    monkeypatch.setattr(routes, "ProfileRepository", _KitProfiles)
    monkeypatch.setattr(routes, "_cv_writer", lambda settings: writer)
    return client, db, credits, calls, state


def test_a_job_with_no_kit_gets_one_built_saved_and_charged_once(tailoring) -> None:  # type: ignore[no-untyped-def]
    """Every job a V2 scan found ended at "Nothing in this build creates one"."""
    client, db, credits, calls, _state = tailoring

    res = client.post("/jobs/1/apply-kit", headers=auth(make_token()))

    assert res.status_code == 200
    body = res.json()
    assert body["cv"]["name"] == "Ravi Kumar"
    assert body["cv"]["phone"] == ""
    assert body["gaps"] == ["Government ERP"]
    assert body["template"] == "classic"
    assert body["disclaimer"]
    assert credits.charged == [("Tailored CV", -15)]
    # Saved to the job, so opening it again is free.
    stored = next(r for r in db.rows if r["id"] == 1)
    assert stored["apply_kit"]["cv"]["summary"] == body["cv"]["summary"]
    assert stored["kit_created_at"]
    # Tailored to THIS posting's requirements.
    assert "Government ERP" in calls[0]


def test_a_job_that_already_has_a_kit_is_not_built_or_charged_again(tailoring) -> None:  # type: ignore[no-untyped-def]
    client, _db, credits, calls, _state = tailoring

    res = client.post("/jobs/2/apply-kit", headers=auth(make_token()))

    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "kit_exists"
    assert credits.charged == []
    assert calls == []


def test_a_cv_that_invents_is_not_kept_or_charged(tailoring) -> None:  # type: ignore[no-untyped-def]
    client, db, credits, _calls, state = tailoring
    state["reply"] = _cv_reply(summary="Grew revenue 300% at every employer.")

    res = client.post("/jobs/1/apply-kit", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "nothing_usable"
    assert "weren't charged" in res.json()["detail"]["message"]
    assert credits.charged == []
    assert next(r for r in db.rows if r["id"] == 1)["apply_kit"] is None


def test_a_dead_model_charges_nothing_and_saves_nothing(tailoring) -> None:  # type: ignore[no-untyped-def]
    client, db, credits, _calls, state = tailoring
    state["dead"] = True

    res = client.post("/jobs/1/apply-kit", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "upstream_unavailable"
    assert credits.charged == []
    assert next(r for r in db.rows if r["id"] == 1)["apply_kit"] is None


def test_no_experience_is_refused_before_credits_or_the_model(tailoring) -> None:  # type: ignore[no-untyped-def]
    client, _db, credits, calls, _state = tailoring
    _KitProfiles.profile = {"name": "New User", "skills": "SQL"}

    res = client.post("/jobs/1/apply-kit", headers=auth(make_token()))

    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "nothing_to_tailor_from"
    assert credits.charged == []
    assert calls == []


def test_an_unreadable_profile_is_not_an_empty_one_for_a_cv_either(tailoring) -> None:  # type: ignore[no-untyped-def]
    client, _db, credits, calls, _state = tailoring
    _KitProfiles.fails = True

    res = client.post("/jobs/1/apply-kit", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "profile_unavailable"
    assert credits.charged == []
    assert calls == []


def test_another_users_job_cannot_be_tailored(tailoring) -> None:  # type: ignore[no-untyped-def]
    client, _db, credits, calls, _state = tailoring

    res = client.post("/jobs/999/apply-kit", headers=auth(make_token()))

    assert res.status_code == 404
    assert credits.charged == []
    assert calls == []


def test_building_a_kit_needs_a_verified_token(client: TestClient) -> None:
    assert client.post("/jobs/1/apply-kit").status_code == 401
