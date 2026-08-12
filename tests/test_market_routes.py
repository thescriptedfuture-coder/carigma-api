"""Market intelligence over HTTP.

The boundary is where the honesty guarantee has to hold, because it is the only
place a user-facing surface can reach:

- **Publishing refuses an unsourced or empty batch.** The human-in-the-loop is
  only a guarantee if the door actually closes.
- **Saving and publishing are separate acts.** An upsert never goes live.
- **No live batch means an honest nothing** — never last week's, on either the
  wire or the email gist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import market as routes
from tests.conftest import auth, make_token

NOW = datetime.now(UTC)

SOURCED = {
    "headline": "Analytics hiring up 12%",
    "detail": "Across Bengaluru and Pune.",
    "source_name": "Some Report",
    "source_url": "https://example.com/report",
}
UNSOURCED = {**SOURCED, "source_url": "trust me"}


class FakeTable:
    def __init__(self, store: FakeDB):
        self._db = store
        self._filters: dict[str, Any] = {}
        self._op: str | None = None
        self._payload: dict[str, Any] | None = None

    def select(self, *_a: Any, **_k: Any) -> FakeTable:
        self._op = "select"
        return self

    def upsert(self, payload: dict[str, Any], **_k: Any) -> FakeTable:
        self._op, self._payload = "upsert", payload
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
        if self._op == "upsert":
            assert self._payload is not None
            key = self._payload["batch_key"]
            existing = next((r for r in self._db.rows if r["batch_key"] == key), None)
            if existing:
                existing.update(self._payload)
            else:
                # Defaults the real table supplies. published_at is NULL on
                # insert — a saved batch is a DRAFT, and a fake that skipped
                # this would let the "upsert never publishes" test pass for the
                # wrong reason.
                self._db.rows.append({"published_at": None, "next_refresh": None, **self._payload})
            return type("Res", (), {"data": [self._payload]})()
        if self._op == "update":
            hit = [r for r in self._db.rows if self._matches(r)]
            for r in hit:
                r.update(self._payload or {})
            return type("Res", (), {"data": hit})()
        return type("Res", (), {"data": [dict(r) for r in self._db.rows if self._matches(r)]})()


class FakeDB:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.fail = False

    def table(self, _name: str) -> FakeTable:
        return FakeTable(self)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    fake = FakeDB()
    monkeypatch.setattr(routes, "service_client", lambda settings: fake)
    monkeypatch.setattr(routes, "user_client", lambda settings, token: fake)
    return fake


def admin_headers() -> dict[str, str]:
    from carigma_api.config import get_settings

    admins = get_settings().admin_emails
    return auth(make_token(email=admins[0] if admins else "admin@carigma.in"))


# ── The admin gate ─────────────────────────────────────────────────────────


def test_curation_is_admin_only(client: TestClient, db: FakeDB) -> None:
    plain = auth(make_token(email="someone@example.com"))

    assert client.get("/admin/market", headers=plain).status_code == 403
    assert (
        client.post(
            "/admin/market", json={"batch_key": "x1", "items": []}, headers=plain
        ).status_code
        == 403
    )
    assert client.post("/admin/market/x1/publish", headers=plain).status_code == 403


def test_the_wire_requires_a_verified_token(client: TestClient, db: FakeDB) -> None:
    assert client.get("/market/wire").status_code == 401


# ── Saving is not publishing ───────────────────────────────────────────────


def test_saving_a_batch_leaves_it_a_draft(client: TestClient, db: FakeDB) -> None:
    """The whole guarantee rests on a human choosing to publish deliberately."""
    res = client.post(
        "/admin/market",
        json={"batch_key": "mkt_w33", "items": [SOURCED]},
        headers=admin_headers(),
    )

    assert res.status_code == 201
    assert res.json()["state"] == "draft"
    assert db.rows[0]["published_at"] is None


def test_a_saved_draft_does_not_reach_the_wire(client: TestClient, db: FakeDB) -> None:
    client.post(
        "/admin/market",
        json={"batch_key": "mkt_w33", "items": [SOURCED]},
        headers=admin_headers(),
    )

    wire = client.get("/market/wire", headers=auth(make_token())).json()

    assert wire["available"] is False
    assert wire["items"] == []


# ── The publish gate ───────────────────────────────────────────────────────


def test_publishing_an_unsourced_batch_is_refused(client: TestClient, db: FakeDB) -> None:
    """A claim a reader cannot check is indistinguishable from a fabricated
    one, so it cannot go live."""
    client.post(
        "/admin/market",
        json={"batch_key": "mkt_w33", "items": [UNSOURCED]},
        headers=admin_headers(),
    )

    res = client.post("/admin/market/mkt_w33/publish", headers=admin_headers())

    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "not_publishable"
    assert db.rows[0]["published_at"] is None, "a refused publish must not go live"


def test_publishing_an_empty_batch_is_refused(client: TestClient, db: FakeDB) -> None:
    client.post(
        "/admin/market", json={"batch_key": "mkt_w33", "items": []}, headers=admin_headers()
    )

    res = client.post("/admin/market/mkt_w33/publish", headers=admin_headers())

    assert res.status_code == 422
    assert "no items" in res.json()["detail"]["message"]


def test_publishing_a_sourced_batch_makes_it_live(client: TestClient, db: FakeDB) -> None:
    client.post(
        "/admin/market",
        json={"batch_key": "mkt_w33", "items": [SOURCED]},
        headers=admin_headers(),
    )

    res = client.post("/admin/market/mkt_w33/publish", headers=admin_headers())

    assert res.status_code == 200
    assert res.json()["state"] == "published"

    wire = client.get("/market/wire", headers=auth(make_token())).json()
    assert wire["available"] is True
    assert wire["items"][0]["source_url"] == "https://example.com/report"


def test_publishing_something_that_does_not_exist_is_a_404(client: TestClient, db: FakeDB) -> None:
    assert client.post("/admin/market/nope/publish", headers=admin_headers()).status_code == 404


# ── No live batch means nothing, never last week's ─────────────────────────


def test_an_expired_batch_yields_an_honest_nothing_not_stale_items(
    client: TestClient, db: FakeDB
) -> None:
    """The failure mode P6-3 inherits. A stale digest resent is manufactured
    contact, and in the lapsed-user email it costs the channel permanently."""
    db.rows.append(
        {
            "batch_key": "mkt_w20",
            "items": [SOURCED],
            "published_at": (NOW - timedelta(days=60)).isoformat(),
            "next_refresh": None,
        }
    )

    wire = client.get("/market/wire", headers=auth(make_token())).json()

    assert wire["available"] is False
    assert wire["items"] == []
    assert set(wire["empty"]) == {"happened", "why", "next"}


def test_a_failed_read_is_not_reported_as_no_research(client: TestClient, db: FakeDB) -> None:
    """ProviderUnavailable is not empty. "No market read this week" when we
    could not look is a claim we have not earned."""
    db.fail = True

    res = client.get("/market/wire", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"


def test_the_admin_list_states_what_is_live_rather_than_making_it_inferred(
    client: TestClient, db: FakeDB
) -> None:
    client.post(
        "/admin/market",
        json={"batch_key": "mkt_w33", "items": [SOURCED]},
        headers=admin_headers(),
    )
    assert client.get("/admin/market", headers=admin_headers()).json()["live"] is None

    client.post("/admin/market/mkt_w33/publish", headers=admin_headers())
    assert client.get("/admin/market", headers=admin_headers()).json()["live"] == "mkt_w33"
