"""The balance endpoint.

Small, and the one rule it must not break is the one this codebase has broken
most often elsewhere: **cannot read is not zero.**
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import credits as routes
from tests.conftest import auth, make_token


class FakeStore:
    def __init__(self, balance: int | None = 88) -> None:
        self.balance = balance

    def read_balance(self, user_id: str) -> int | None:
        return self.balance


@pytest.fixture
def wired(client: TestClient, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    store = FakeStore()
    monkeypatch.setattr(routes, "_store", lambda request, settings: store)
    return client, store


def test_the_balance_needs_a_verified_token(client: TestClient) -> None:
    assert client.get("/credits").status_code == 401


def test_the_balance_comes_back(wired) -> None:  # type: ignore[no-untyped-def]
    client, _store = wired

    body = client.get("/credits", headers=auth(make_token())).json()

    assert body["balance"] == 88
    assert body["known"] is True


def test_a_balance_we_could_not_read_is_null_not_zero(wired) -> None:  # type: ignore[no-untyped-def]
    """Reporting 0 would tell a user with credits that they have none, and the
    surface would then refuse them work they have already paid for."""
    client, store = wired
    store.balance = None

    body = client.get("/credits", headers=auth(make_token())).json()

    assert body["balance"] is None
    assert body["known"] is False


def test_a_real_zero_is_still_a_real_answer(wired) -> None:  # type: ignore[no-untyped-def]
    """The conflation in the other direction: someone who has genuinely spent
    everything is not someone we failed to read."""
    client, store = wired
    store.balance = 0

    body = client.get("/credits", headers=auth(make_token())).json()

    assert body["balance"] == 0
    assert body["known"] is True


def test_reading_a_balance_never_writes_one() -> None:
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

    for forbidden in ("apply_delta", "grant", "charge_for_result", "update", "insert", "upsert"):
        assert forbidden not in referenced, f"{forbidden!r} appears on a read-only path"


def test_the_fake_could_have_failed_the_no_zero_rule() -> None:
    """Assert the precondition: a store that can only return a number could
    never exercise the branch above."""
    assert FakeStore(None).read_balance("u") is None
    assert FakeStore(0).read_balance("u") == 0
