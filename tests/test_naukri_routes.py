"""The Naukri lens over HTTP.

The rules checked at the boundary, because these are the ones an endpoint can
quietly violate even when the service underneath is right:

- **A never-run lens writes nothing.** The absence of a row IS the state.
- **The cycle is charged once.** Starting costs 10; every step after is free.
- **A run that cannot be stored is a failure**, and a failure charges nothing.
- **Reading is not a run**, so the GET never touches credits.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import naukri as routes
from carigma_api.services.naukri import CycleState
from tests.conftest import auth, make_token

# camelCase, because that is what `db_to_profile` hands the route. Reading
# `linkedin_headline` here once produced "No headline on the profile yet" for
# every user who had one — see `test_a_real_headline_is_not_reported_as_empty`.
PROFILE = {
    "name": "Ravi Kumar",
    "linkedinHeadline": "Business Analyst | 5 years | SQL, Power BI | Ops analytics",
    "skills": "SQL, Power BI, dbt",
    "location": "Bengaluru",
    "education": "B.Tech",
}


class FakeTable:
    def __init__(self, store: FakeDB, name: str):
        self._db = store
        self._name = name
        self._filters: dict[str, Any] = {}
        self._op: str | None = None
        self._payload: dict[str, Any] | None = None

    def insert(self, payload: dict[str, Any]) -> FakeTable:
        self._op, self._payload = "insert", payload
        return self

    def select(self, *_a: Any, **_k: Any) -> FakeTable:
        self._op = "select"
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def order(self, *_a: Any, **_k: Any) -> FakeTable:
        return self

    def limit(self, *_a: Any, **_k: Any) -> FakeTable:
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        return all(str(row.get(k)) == str(v) for k, v in self._filters.items())

    def execute(self) -> Any:
        if self._db.fail_reads and self._op == "select":
            raise RuntimeError("db down")
        if self._op == "insert":
            if self._db.fail_writes:
                raise RuntimeError("insert rejected")
            assert self._payload is not None
            # Column DEFAULTS the real table applies. A fake that skips them
            # returns rows the database can never produce.
            row = {"id": self._db.next_id, "optimized_at": None, **self._payload}
            self._db.next_id += 1
            self._db.rows.append(row)
            return type("Res", (), {"data": [row]})()
        # Newest first, matching `.order("created_at", desc=True).limit(1)`.
        hits = [dict(r) for r in reversed(self._db.rows) if self._matches(r)]
        return type("Res", (), {"data": hits[:1]})()


class FakeDB:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.next_id = 1
        self.fail_reads = False
        self.fail_writes = False

    def table(self, name: str) -> FakeTable:
        return FakeTable(self, name)


class FakeProfiles:
    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        self.profile = dict(PROFILE if profile is None else profile)
        self.saved: dict[str, Any] | None = None

    def load(self, user_id: str) -> dict[str, Any]:
        return dict(self.profile)

    def save(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        self.profile.update(profile)
        self.saved = dict(profile)
        return dict(self.profile)


class FakeCredits:
    def __init__(self, balance: int = 100) -> None:
        self.balance = balance
        self.ledger: list[tuple[int, str]] = []

    def read_balance(self, user_id: str) -> int | None:
        return self.balance

    def apply_delta(self, user_id: str, delta: int, reason: str, balance_after: int) -> None:
        self.balance = balance_after
        self.ledger.append((delta, reason))


class _NullRunStore:
    def save(self, run: object) -> None: ...

    def get(self, run_id: str) -> None:
        return None

    def remember_idempotency(self, user_id: str, key: str, run_id: str) -> None: ...

    def find_by_idempotency_key(self, user_id: str, key: str) -> None:
        return None


@pytest.fixture
def wired(client: TestClient, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    db, profiles, creds = FakeDB(), FakeProfiles(), FakeCredits()
    monkeypatch.setattr(
        routes, "_deps", lambda request, settings: (db, profiles, creds, _NullRunStore())
    )
    return client, db, profiles, creds


# ── Auth ───────────────────────────────────────────────────────────────────


def test_every_naukri_endpoint_requires_a_verified_token(client: TestClient) -> None:
    assert client.get("/naukri/score").status_code == 401
    assert client.post("/naukri/score").status_code == 401


# ── The never-run read: the path every existing user is on ─────────────────


def test_never_run_returns_the_state_and_writes_nothing(wired) -> None:  # type: ignore[no-untyped-def]
    """`naukri_scores` is empty and V1 never had Naukri scoring, so this is the
    primary path, not an edge case. A read must not manufacture a row."""
    client, db, _profiles, _creds = wired

    res = client.get("/naukri/score", headers=auth(make_token()))

    assert res.status_code == 200
    body = res.json()
    assert body["cycle_state"] == str(CycleState.NEVER_RUN)
    assert body["state"] == "never_run"
    assert body["score"] is None, "a never-run lens must not report a number"
    assert db.rows == [], "reading the lens wrote a row"


def test_the_never_run_note_does_not_talk_in_percentages(wired) -> None:  # type: ignore[no-untyped-def]
    """ "Scored on the 0% of the model we could assess" is accurate and reads
    like a bug — a percentage invites zero to be read as a result."""
    client, *_ = wired

    body = client.get("/naukri/score", headers=auth(make_token())).json()

    assert "%" not in body["coverage_note"]
    assert "hasn't run" in body["coverage_note"]


def test_a_never_run_lens_offers_no_fixes(wired) -> None:  # type: ignore[no-untyped-def]
    """Nothing measured means nothing to fix. Inventing one would be a finding
    about a profile we have never assessed."""
    client, *_ = wired

    body = client.get("/naukri/score", headers=auth(make_token())).json()

    assert body["fixes"] == []


def test_every_unavailable_dimension_offers_somewhere_to_go(wired) -> None:  # type: ignore[no-untyped-def]
    """An absence without a next action is a dead end with good grammar."""
    client, *_ = wired

    body = client.get("/naukri/score", headers=auth(make_token())).json()
    unavailable = [d for d in body["dimensions"] if d["confidence"] == "unavailable"]

    assert unavailable, "the fixture should leave something unmeasurable"
    for dimension in unavailable:
        assert dimension["unlocked_by"], f"{dimension['key']} states no way out"
        assert dimension["unlock_action"]["route"], f"{dimension['key']} names nowhere to go"


def test_a_dimension_we_have_not_built_offers_the_user_nothing_to_do(wired) -> None:  # type: ignore[no-untyped-def]
    """The two kinds of absence are different statements.

    `key_skills` and `parseability` used to render "Run Career Scout" and
    "Upload your resume" — buttons for data nothing in the product collects.
    An action the product cannot honour is a lie with a click target.
    """
    client, *_ = wired

    body = client.get("/naukri/score", headers=auth(make_token())).json()
    not_built = [d for d in body["dimensions"] if d["confidence"] == "not_built"]

    assert not_built, "nothing is marked as not-yet-built"
    for dimension in not_built:
        assert dimension["unlock_action"] is None, f"{dimension['key']} offers a dead button"
        assert dimension["unlocked_by"] is None
        assert dimension["needs"], f"{dimension['key']} is absent with no explanation"
        assert dimension["weight"] == 0, "an unbuilt dimension is inside the denominator"


def test_the_lens_says_what_it_covers_before_it_says_a_number(wired) -> None:  # type: ignore[no-untyped-def]
    client, *_ = wired

    body = client.get("/naukri/score", headers=auth(make_token())).json()

    assert "2 of the 7 dimensions" in body["scope_note"]
    assert body["total_weight"] == 30


def test_the_never_run_screen_states_what_the_first_run_costs(wired) -> None:  # type: ignore[no-untyped-def]
    """The first question anyone asks before pressing a button."""
    client, *_ = wired

    body = client.get("/naukri/score", headers=auth(make_token())).json()

    assert body["next_run_costs"] == 10


def test_a_failed_read_is_not_reported_as_never_run(wired) -> None:  # type: ignore[no-untyped-def]
    """Could-not-look is not never-ran. Showing the never-run screen would tell
    a user to start a tune-up they may already be halfway through."""
    client, db, _profiles, _creds = wired
    db.fail_reads = True

    res = client.get("/naukri/score", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"


def test_reading_the_lens_never_charges(wired) -> None:  # type: ignore[no-untyped-def]
    client, _db, _profiles, creds = wired

    client.get("/naukri/score", headers=auth(make_token()))

    assert creds.balance == 100
    assert creds.ledger == []


# ── The false finding a key mismatch produced ──────────────────────────────


def test_there_is_no_write_endpoint_left_on_this_router() -> None:
    """`/naukri/fix` merged confirmed skills into a profile. The only producer
    of a confirmable skill was `score_key_skills`, which is gone — so the
    endpoint became a live profile-write path with nothing user-facing in front
    of it, which is worse than dead code."""
    paths = {r.path for r in routes.router.routes}  # type: ignore[attr-defined]

    assert paths == {"/naukri/score"}


def test_a_real_headline_is_not_reported_as_empty(wired) -> None:  # type: ignore[no-untyped-def]
    """The repository hands back camelCase; the route read snake_case, so every
    user with a headline was told they had none. A missing key and an empty
    field are indistinguishable downstream, which is what made it silent."""
    client, *_ = wired

    body = client.post("/naukri/score", headers=auth(make_token())).json()
    headline = next(d for d in body["dimensions"] if d["key"] == "headline")

    assert headline["score"] is not None
    assert headline["score"] > 0, "a full headline scored zero — the key is wrong again"
    assert "No headline" not in headline["receipt"]


# ── The cycle charge ───────────────────────────────────────────────────────


def test_starting_a_cycle_costs_ten_and_stores_the_result(wired) -> None:  # type: ignore[no-untyped-def]
    client, db, _profiles, creds = wired

    res = client.post("/naukri/score", headers=auth(make_token()))

    assert res.status_code == 200
    body = res.json()
    assert body["credits"]["charged"] == 10
    assert creds.balance == 90
    assert body["cycle_state"] == str(CycleState.NEEDS_TUNEUP)
    assert len(db.rows) == 1
    assert db.rows[0]["cycle_state"] == str(CycleState.NEEDS_TUNEUP)


def test_a_second_run_inside_the_cycle_is_free(wired) -> None:  # type: ignore[no-untyped-def]
    """You pay to start the tune-up; finishing it costs nothing more. Per-step
    billing is what produces mid-cycle abandonment."""
    client, _db, _profiles, creds = wired
    client.post("/naukri/score", headers=auth(make_token()))

    body = client.post("/naukri/score", headers=auth(make_token())).json()

    assert body["credits"]["charged"] == 0
    assert creds.balance == 90, "the cycle was charged twice"
    assert [entry[0] for entry in creds.ledger] == [-10]


def test_a_run_after_the_cycle_closed_starts_a_new_paid_one(wired) -> None:  # type: ignore[no-untyped-def]
    client, db, _profiles, creds = wired
    client.post("/naukri/score", headers=auth(make_token()))
    db.rows[-1]["cycle_state"] = str(CycleState.OPTIMIZED)

    body = client.post("/naukri/score", headers=auth(make_token())).json()

    assert body["credits"]["charged"] == 10
    assert creds.balance == 80


def test_the_stored_row_reports_the_next_run_as_free(wired) -> None:  # type: ignore[no-untyped-def]
    """Read back what an open cycle costs, since that is what the button says."""
    client, *_ = wired
    client.post("/naukri/score", headers=auth(make_token()))

    body = client.get("/naukri/score", headers=auth(make_token())).json()

    assert body["cycle_state"] == str(CycleState.NEEDS_TUNEUP)
    assert body["next_run_costs"] == 0


def test_an_optimized_row_reports_the_next_run_as_paid(wired) -> None:  # type: ignore[no-untyped-def]
    client, db, *_ = wired
    client.post("/naukri/score", headers=auth(make_token()))
    db.rows[-1]["cycle_state"] = str(CycleState.OPTIMIZED)

    body = client.get("/naukri/score", headers=auth(make_token())).json()

    assert body["next_run_costs"] == 10


def test_insufficient_credits_blocks_before_any_work(wired) -> None:  # type: ignore[no-untyped-def]
    client, db, _profiles, creds = wired
    creds.balance = 4

    res = client.post("/naukri/score", headers=auth(make_token()))

    assert res.status_code == 402
    assert "4" in res.json()["detail"]
    assert db.rows == [], "a row was written for a run the user could not afford"
    assert creds.balance == 4


def test_a_broke_user_can_still_continue_an_open_cycle(wired) -> None:  # type: ignore[no-untyped-def]
    """The charge bought the whole cycle. Running out mid-way must not strand
    someone inside a task they already paid to start."""
    client, _db, _profiles, creds = wired
    client.post("/naukri/score", headers=auth(make_token()))
    creds.balance = 0

    res = client.post("/naukri/score", headers=auth(make_token()))

    assert res.status_code == 200
    assert res.json()["credits"]["charged"] == 0


# ── A run that cannot be stored is a failure ───────────────────────────────


def test_a_run_that_cannot_save_charges_nothing(wired) -> None:  # type: ignore[no-untyped-def]
    """Saving is part of the work, deliberately.

    The alternative — return the score with a warning — charged 10 for a cycle
    that left no row, so the next run would find no row, call itself a cycle
    start, and charge 10 again. Refunding would have meant a second credit
    path. Folding the save into the work means the existing no-charge-on-
    failure rule covers it with no new mechanism.
    """
    client, db, _profiles, creds = wired
    db.fail_writes = True

    res = client.post("/naukri/score", headers=auth(make_token()))

    assert res.status_code == 503
    assert creds.balance == 100, "a run that stored nothing still charged"
    assert creds.ledger == []
    assert db.rows == []


def test_a_failed_run_shows_copy_not_an_exception(wired) -> None:  # type: ignore[no-untyped-def]
    client, db, *_ = wired
    db.fail_writes = True

    detail = client.post("/naukri/score", headers=auth(make_token())).json()["detail"]

    assert "insert rejected" not in str(detail)
    assert "Nothing charged" in detail["message"]
    # Error copy carries no emoji (Bible §18.1).
    assert all(ord(ch) < 0x2190 for ch in detail["message"])


def test_nothing_assessable_stores_nothing_and_charges_nothing(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Two rules composing into one answer, proved rather than asserted.

    `_build_score` cannot currently produce this — headline and filter fields
    always score, even at zero — so the state is reached by making the only
    weighted dimension unavailable. Without the honest empty the work would return a
    full dict of unavailables, which is non-empty by len(), so `execute` would
    bill for a run that stores nothing.
    """
    from carigma_api.services.naukri import Dimension, NaukriScore, UnlockAction

    def nothing_measurable(profile: dict[str, Any]) -> NaukriScore:
        return NaukriScore(
            dimensions=[
                Dimension.unavailable(
                    "headline",
                    "Resume headline",
                    "Nothing to read.",
                    unlocked_by="Add a headline and we can score it.",
                    action=UnlockAction(label="Edit profile", route="/settings"),
                )
            ],
            fixes=[],
        )

    monkeypatch.setattr(routes, "_build_score", nothing_measurable)
    client, db, _profiles, creds = wired

    res = client.post("/naukri/score", headers=auth(make_token()))

    assert res.status_code == 200
    assert res.json()["cycle_state"] == str(CycleState.NEVER_RUN)
    assert creds.balance == 100, "an unassessable run was charged"
    assert db.rows == [], "an unassessable run was stored"


def test_no_row_is_ever_written_carrying_never_run(wired, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`CycleState.NEVER_RUN` documents itself as a value that never appears in
    a row. A row carrying it would make the absence of a row and the presence
    of one mean the same thing."""
    client, db, *_ = wired
    client.post("/naukri/score", headers=auth(make_token()))

    assert db.rows
    assert all(r["cycle_state"] != str(CycleState.NEVER_RUN) for r in db.rows)


# ── Reading back a stored row ──────────────────────────────────────────────


def test_the_stored_payload_is_what_the_run_returned(wired) -> None:  # type: ignore[no-untyped-def]
    """Persist what the user was shown and charged for. Re-deriving it would be
    a second chance to disagree with the first."""
    client, db, *_ = wired

    ran = client.post("/naukri/score", headers=auth(make_token())).json()
    read = client.get("/naukri/score", headers=auth(make_token())).json()

    assert read["score"] == ran["score"] == db.rows[0]["score"]
    assert [d["key"] for d in read["dimensions"]] == [d["key"] for d in ran["dimensions"]]


def test_the_model_note_comes_from_the_constant_not_the_stored_blob(wired) -> None:  # type: ignore[no-untyped-def]
    """A blob written under older weights would keep asserting provenance for
    weights that have since changed."""
    from carigma_api.services.naukri import MODEL_NOTE

    client, db, *_ = wired
    client.post("/naukri/score", headers=auth(make_token()))
    db.rows[0]["dimensions"]["model_note"] = "Naukri's own official formula."

    body = client.get("/naukri/score", headers=auth(make_token())).json()

    assert body["model_note"] == MODEL_NOTE
    assert "official" not in body["model_note"]


def test_an_unreadable_cycle_state_resolves_to_the_free_reading(wired) -> None:  # type: ignore[no-untyped-def]
    """The error directions are not symmetric: guessing free costs us a run,
    guessing paid charges someone twice for one cycle."""
    client, db, _profiles, creds = wired
    client.post("/naukri/score", headers=auth(make_token()))
    db.rows[0]["cycle_state"] = "something_a_later_version_wrote"

    body = client.post("/naukri/score", headers=auth(make_token())).json()

    assert body["credits"]["charged"] == 0
    assert creds.balance == 90


def test_the_read_endpoint_never_charges_credits() -> None:
    """Same guard on the GET. Reading is not a run."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(routes.get_naukri_score))

    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            referenced.add(node.value)

    # `cost_of` is allowed — quoting a price is not charging one.
    for forbidden in ("apply_delta", "charge_for_result", "credit_ledger", "insert", "upsert"):
        assert forbidden not in referenced, f"{forbidden!r} appears on the free read path"


def test_the_charge_is_a_pure_function_of_the_action() -> None:
    """No `cost_override` anywhere. Per-cycle pricing looked like it needed a
    state-dependent price, which would have been a second charging path in a
    different costume; starting a cycle and continuing one are different WORK,
    so they are different actions instead."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(routes))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                assert kw.arg != "cost_override", "a per-call price override was introduced"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value != "cost_override"
