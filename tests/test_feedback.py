"""The feedback widget (S15). Spec 9.3; V1's sidebar 💬.

The finding underneath: V1 asked for the row back on every insert that carried
a user id, `feedback` has no SELECT policy, and Postgres refuses an
`INSERT … RETURNING` that SELECT would not allow. None of the four notes V1
ever stored says who sent it. The fake now refuses the same (`WRITE_ONLY`).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import feedback as routes
from tests.conftest import USER_EMAIL, USER_ID, auth, make_token
from tests.fake_supabase import FakeDB


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDB]:
    fake = FakeDB()
    monkeypatch.setattr(routes, "user_client", lambda settings, token: fake)
    yield fake


def _send(client: TestClient, **body: Any) -> Any:
    return client.post("/feedback", json=body, headers=auth(make_token()))


def test_it_requires_a_verified_token(client: TestClient) -> None:
    assert client.post("/feedback", json={"kind": "bug", "message": "x"}).status_code == 401


def test_a_bug_is_stored_as_v1_stored_it_with_its_sender(client: TestClient, db: FakeDB) -> None:
    res = _send(client, kind="bug", message="  The Save button does nothing  ", page="/settings")

    assert res.status_code == 201
    assert res.json() == {"received": True}
    (row,) = db.rows("feedback")
    # V1's encoding, so the admin list reads one history.
    assert row["message"] == "[bug] The Save button does nothing"
    assert row["page"] == "/settings|bug"
    assert row["rating"] is None
    # From the verified token — what V1 never managed to store.
    assert row["user_id"] == USER_ID
    assert row["email"] == USER_EMAIL


def test_a_rating_keeps_its_stars_and_other_kinds_do_not(client: TestClient, db: FakeDB) -> None:
    _send(client, kind="rating", message="Clear page", rating=4, page="/progress")
    _send(client, kind="idea", message="Add a dark mode toggle", rating=5)

    rating, idea = db.rows("feedback")
    assert rating["rating"] == 4 and rating["page"] == "/progress|rating"
    assert idea["rating"] is None


def test_the_sender_is_never_taken_from_the_body(client: TestClient, db: FakeDB) -> None:
    """The INSERT policy is `with check (true)`: the database would store any
    id it was handed."""
    _send(client, kind="idea", message="x", user_id="someone-else", email="spoof@example.com")

    (row,) = db.rows("feedback")
    assert row["user_id"] == USER_ID
    assert row["email"] == USER_EMAIL


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "bug", "message": "   "},
        {"kind": "complaint", "message": "x"},
        {"kind": "rating", "message": "x", "rating": 6},
        {"kind": "idea", "message": "x" * 2001},
    ],
)
def test_what_is_not_a_note_is_refused(client: TestClient, db: FakeDB, body: Any) -> None:
    assert _send(client, **body).status_code == 422
    assert db.rows("feedback") == []


def test_a_note_that_could_not_be_stored_says_so_and_where_else_to_send_it(
    client: TestClient, db: FakeDB
) -> None:
    db.failing.add("feedback")

    res = _send(client, kind="bug", message="Broken")

    assert res.status_code == 503
    assert "support@carigma.in" in res.json()["detail"]["message"]


def test_asking_for_the_row_back_is_refused_as_production_refuses_it() -> None:
    """The fake models the finding, so a route that drops `returning="minimal"`
    fails here before it fails in production."""
    from tests.fake_supabase import RlsRefused

    fake = FakeDB()
    with pytest.raises(RlsRefused):
        fake.table("feedback").insert({"message": "x"}).execute()
