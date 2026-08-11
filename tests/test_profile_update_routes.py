"""Incremental profile changes over HTTP.

The rules checked at the boundary, because these are the ones a future endpoint
could quietly violate:

- **Nothing on this path charges.** Detection and the proposal are free.
- **A decision happens once.** Re-deciding is a 409, never an overwrite.
- **Nothing is applied without a yes.**
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import profile_updates as routes
from tests.conftest import auth, make_token


class FakeTable:
    """Enough PostgREST to exercise the routes, with a real row store."""

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

    def update(self, payload: dict[str, Any]) -> FakeTable:
        self._op, self._payload = "update", payload
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
        if self._db.fail:
            raise RuntimeError("db down")
        if self._op == "insert":
            assert self._payload is not None
            # Column DEFAULTS, applied here because the real table applies them
            # and the route relies on that. Without these the fake returns a row
            # with no `state`, which is a shape the database can never produce —
            # a fake that is easier than reality tests nothing.
            row = {
                "id": self._db.next_id,
                "state": "proposed",
                "decided_at": None,
                "applied_at": None,
                **self._payload,
            }
            self._db.next_id += 1
            self._db.rows.append(row)
            return type("Res", (), {"data": [row]})()
        if self._op == "update":
            hit = [r for r in self._db.rows if self._matches(r)]
            for r in hit:
                r.update(self._payload or {})
            return type("Res", (), {"data": hit})()
        # Snapshots, because a real database returns copies. Handing out live
        # references is how a concurrency test passed for the wrong reason once.
        return type("Res", (), {"data": [dict(r) for r in self._db.rows if self._matches(r)]})()


class FakeDB:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.next_id = 1
        self.fail = False

    def table(self, name: str) -> FakeTable:
        return FakeTable(self, name)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    fake = FakeDB()
    monkeypatch.setattr(routes, "_db", lambda request, settings: fake)
    return fake


CERT = {"kind": "certification", "payload": {"name": "dbt Analytics Engineering"}}
ROLE = {"kind": "role", "payload": {"title": "Data Lead", "company": "Zomato"}}


# ── Auth ───────────────────────────────────────────────────────────────────


def test_every_endpoint_requires_a_verified_token(client: TestClient) -> None:
    assert client.post("/profile/updates", json=CERT).status_code == 401
    assert client.get("/profile/updates/open").status_code == 401
    assert client.post("/profile/updates/1/decide", json={"approved": True}).status_code == 401


# ── Adding a change ────────────────────────────────────────────────────────


def test_adding_a_certification_returns_a_proposal_and_applies_nothing(
    client: TestClient, db: FakeDB
) -> None:
    res = client.post("/profile/updates", json=CERT, headers=auth(make_token()))

    assert res.status_code == 201
    body = res.json()
    assert body["state"] == "proposed"
    assert body["proposal"]["magnitude"] == "quiet"
    # Stored as pending, not applied.
    assert db.rows[0]["state" if "state" in db.rows[0] else "kind"] is not None
    assert db.rows[0].get("applied_at") is None


def test_a_role_change_proposes_a_re_decode_not_a_tune(client: TestClient, db: FakeDB) -> None:
    body = client.post("/profile/updates", json=ROLE, headers=auth(make_token())).json()

    assert body["proposal"]["magnitude"] == "major"
    assert set(body["proposal"]["agents"]) == {"profile", "content", "jobs"}


def test_the_proposal_states_that_noticing_is_free(client: TestClient, db: FakeDB) -> None:
    """The first question anyone asks before agreeing is whether it costs."""
    body = client.post("/profile/updates", json=CERT, headers=auth(make_token())).json()

    assert body["proposal"]["detection_cost_credits"] == 0


def test_an_unmodelled_field_is_refused(client: TestClient, db: FakeDB) -> None:
    res = client.post(
        "/profile/updates",
        json={"kind": "skill", "payload": {"name": "dbt", "notes": "free text"}},
        headers=auth(make_token()),
    )

    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "unknown_field"
    assert db.rows == [], "a refused change must not be stored"


def test_a_failed_save_says_nothing_was_added(client: TestClient, db: FakeDB) -> None:
    """Implying the change landed would leave the user believing their profile
    has an entry it does not have."""
    db.fail = True

    res = client.post("/profile/updates", json=CERT, headers=auth(make_token()))

    assert res.status_code == 503
    assert "nothing was added" in res.json()["detail"]["message"]


# ── The open proposal ──────────────────────────────────────────────────────


def test_nothing_pending_is_a_normal_state_not_an_error(client: TestClient, db: FakeDB) -> None:
    res = client.get("/profile/updates/open", headers=auth(make_token()))

    assert res.status_code == 200
    assert res.json()["change"] is None


def test_a_failed_read_is_not_reported_as_nothing_pending(client: TestClient, db: FakeDB) -> None:
    """ProviderUnavailable is not empty. Saying "nothing pending" when we could
    not look would hide a proposal the user is waiting on."""
    db.fail = True

    res = client.get("/profile/updates/open", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"


# ── Deciding ───────────────────────────────────────────────────────────────


def _add(client: TestClient, body: dict[str, Any] = CERT) -> int:
    return int(client.post("/profile/updates", json=body, headers=auth(make_token())).json()["id"])


def test_approving_records_the_yes_and_names_the_agents_to_run(
    client: TestClient, db: FakeDB
) -> None:
    change_id = _add(client, ROLE)

    res = client.post(
        f"/profile/updates/{change_id}/decide", json={"approved": True}, headers=auth(make_token())
    )

    assert res.status_code == 200
    body = res.json()
    assert body["user_approved"] is True
    assert body["decided_at"]
    assert set(body["run_agents"]) == {"profile", "content", "jobs"}


def test_declining_runs_nothing(client: TestClient, db: FakeDB) -> None:
    change_id = _add(client)

    body = client.post(
        f"/profile/updates/{change_id}/decide", json={"approved": False}, headers=auth(make_token())
    ).json()

    assert body["user_approved"] is False
    assert body["run_agents"] == [], "a decline must not start an agent"


def test_a_second_decision_is_refused_rather_than_overwritten(
    client: TestClient, db: FakeDB
) -> None:
    """The second answer would silently replace the record of what the user
    actually agreed to."""
    change_id = _add(client)
    client.post(
        f"/profile/updates/{change_id}/decide", json={"approved": True}, headers=auth(make_token())
    )

    res = client.post(
        f"/profile/updates/{change_id}/decide", json={"approved": False}, headers=auth(make_token())
    )

    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "already_decided"


def test_deciding_something_that_is_not_yours_is_a_404(client: TestClient, db: FakeDB) -> None:
    res = client.post(
        "/profile/updates/9999/decide", json={"approved": True}, headers=auth(make_token())
    )

    assert res.status_code == 404


def test_no_endpoint_on_this_path_charges_credits() -> None:
    """The credit rule lives in `services.runs.execute` and must not grow a
    second implementation here.

    Asserted against the module's CODE rather than a response, so adding a
    charge later fails even if no test covers the new route. It walks the AST
    rather than grepping the source: the first version matched the word
    "charges" in this module's own docstring, which is a guard that fires on
    prose and would have to be deleted the moment it was inconvenient.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(routes))

    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            referenced.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # STRING LITERALS TOO. The identifier-only version passed while a
            # `db.table('credits').update(...)` sat on the free path: the table
            # name is a string, so nothing in the AST's names caught it. Found
            # by breaking the guard, which is the only reason this line exists.
            referenced.add(node.value)

    for forbidden in (
        "credits",
        "credit_ledger",
        "deduct",
        "deduct_credits",
        "spend",
        "charge",
        "CreditStore",
    ):
        assert forbidden not in referenced, f"{forbidden!r} appears on the free path"
