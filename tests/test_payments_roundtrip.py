"""The money path, end to end, through the routes that actually run it.

## Why this file exists

`test_payments.py` covers `verify_topup` thoroughly — against a `FakeStore`
seeded with a hand-written `a_payment()` row. `test_payments_routes.py` covers
the routes thoroughly — and every one of its live cases is a REFUSAL: dormant
404, unknown pack 400, missing token 401, forged signature 400.

Between them, nothing had ever executed a successful `POST /payments/topup`.
The row the route writes had never been read by the code that credits it, and
the seeded row in the service tests was written by hand — so any disagreement
between what `record_payment` stores and what `verify_topup` reads would pass
both suites and fail in production as **"I paid and got nothing."**

So this file runs the real `SupabasePaymentStore` and the real
`SupabaseCreditStore` against an in-memory table, and does the whole loop over
HTTP: create the link, let Razorpay say paid, verify, check the ledger.

## The fake models the compare-and-swap, deliberately

`claim_payment` is a conditional update whose AFFECTED-ROW COUNT is the entire
idempotency mechanism. A fake where `update()` always reports success would let
the double-credit bug through while looking green, so `FakeTable.update`
applies its `eq` filters and returns only the rows it actually changed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.config import Settings, get_settings
from carigma_api.routes import payments as routes
from carigma_api.services import payments as pay
from carigma_api.services.repository import SupabaseCreditStore
from tests.conftest import auth, make_token

USER = "user-123"


# ── An in-memory Supabase good enough to be wrong in the same ways ─────────


class FakeTable:
    def __init__(self, db: FakeDB, name: str) -> None:
        self._db = db
        self._name = name
        self._filters: dict[str, Any] = {}
        self._op: str | None = None
        self._payload: dict[str, Any] | None = None
        self._conflict: str | None = None
        self._single = False

    def select(self, *_a: Any, **_k: Any) -> FakeTable:
        self._op = "select"
        return self

    def insert(self, payload: dict[str, Any]) -> FakeTable:
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload: dict[str, Any]) -> FakeTable:
        self._op, self._payload = "update", payload
        return self

    def upsert(self, payload: dict[str, Any], **kw: Any) -> FakeTable:
        """Merge on the conflict target, or insert.

        This method did not exist. `apply_delta` used `.update()`, so nothing
        exercised an upsert and the fake was never asked for one — and the
        moment the real code needed it, the fake raised AttributeError and
        three payment tests failed for a reason that had nothing to do with
        payments.

        The behaviour that matters is the CONFLICT: a second write for the
        same user must merge, not append. A fake that appended would let a
        double-credit bug pass here.
        """
        self._op, self._payload = "upsert", payload
        self._conflict = kw.get("on_conflict") or "user_id"
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def limit(self, _n: int) -> FakeTable:
        return self

    def maybe_single(self) -> FakeTable:
        self._single = True
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        return all(str(row.get(k)) == str(v) for k, v in self._filters.items())

    def __getattr__(self, name: str) -> Any:
        """Say WHICH method is missing, and that the real client has it.

        Two fakes have now been absent rather than simplified. `_NullRunStore`
        accepted every run and rejected none. This class's sibling in
        `test_payments_roundtrip` had no `upsert` at all — and the day
        `apply_delta` stopped using `update`, three payment tests failed with a
        bare AttributeError, in a suite about payments, for a reason that had
        nothing to do with payments.

        A STATIC "implements everything production calls" check was written and
        thrown away: most fakes stand in for two or three tables and would fail
        it for methods they will never be asked for, and a guard that fires on
        correct code gets deleted. The honest version is this — it cannot fire
        early, and when it does fire it names the cause.
        """
        raise AttributeError(
            f"{type(self).__name__} has no `{name}`, and production code just called it on a "
            "table chain. The real client implements it; this stand-in does not. Add it — and "
            "model what it REFUSES, not only what it accepts."
        )

    def execute(self) -> Any:
        rows = self._db.tables.setdefault(self._name, [])
        if self._name in self._db.failing:
            raise RuntimeError(f"{self._name} is down")

        if self._op == "insert":
            assert self._payload is not None
            rows.append(dict(self._payload))
            return _Res([dict(self._payload)])

        if self._op == "upsert":
            assert self._payload is not None
            keys = [k.strip() for k in str(self._conflict).split(",")]
            ident = tuple(str(self._payload.get(k)) for k in keys)
            for row in rows:
                if tuple(str(row.get(k)) for k in keys) == ident:
                    row.update(self._payload)
                    return _Res([dict(row)])
            rows.append(dict(self._payload))
            return _Res([dict(self._payload)])

        if self._op == "update":
            assert self._payload is not None
            # Only the rows the filters actually select are changed, and only
            # those are reported. This is what makes `claim_payment` testable.
            hit = [r for r in rows if self._matches(r)]
            for row in hit:
                row.update(self._payload)
            return _Res([dict(r) for r in hit])

        found = [dict(r) for r in rows if self._matches(r)]
        if self._single:
            return _Res(found[0] if found else None)
        return _Res(found)


class _Res:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeDB:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.failing: set[str] = set()

    def table(self, name: str) -> FakeTable:
        return FakeTable(self, name)

    def rows(self, name: str) -> list[dict[str, Any]]:
        return self.tables.get(name, [])


class FakeRazorpay:
    """Only the two calls the routes make, and a settable link status."""

    def __init__(self) -> None:
        self.link_status = "created"
        self.created: list[dict[str, Any]] = []
        self.payment_link = self._Links(self)
        self.fail_create = False

    class _Links:
        def __init__(self, outer: FakeRazorpay) -> None:
            self._o = outer

        def create(self, spec: dict[str, Any]) -> dict[str, Any]:
            if self._o.fail_create:
                raise RuntimeError("razorpay is down")
            self._o.created.append(spec)
            return {"id": "plink_test_1", "short_url": "https://rzp.io/i/test1"}

        def fetch(self, link_id: str) -> dict[str, Any]:
            return {
                "id": link_id,
                "status": self._o.link_status,
                "payments": [{"payment_id": "pay_test_1"}],
            }


@pytest.fixture
def live(client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Payments on, Razorpay faked, storage in memory — real store code between."""
    live_settings = settings.model_copy(
        update={
            "payments_enabled": True,
            "razorpay_key_id": "rzp_test_abc123",
            "razorpay_key_secret": "rzp_test_secret",
        }
    )
    client.app.dependency_overrides[get_settings] = lambda: live_settings  # type: ignore[attr-defined]

    db, gateway = FakeDB(), FakeRazorpay()
    # The user starts with a balance row, as every real user does.
    db.tables["credits"] = [{"user_id": USER, "balance": 20}]

    monkeypatch.setattr(routes, "_razorpay", lambda _s: gateway)
    monkeypatch.setattr(
        routes,
        "_deps",
        lambda request, settings: (
            routes.SupabasePaymentStore(db),
            routes._CreditGranter(SupabaseCreditStore(db)),
        ),
    )
    yield client, db, gateway
    client.app.dependency_overrides.pop(get_settings, None)  # type: ignore[attr-defined]


def token() -> dict[str, str]:
    return auth(make_token(sub=USER))


def balance(db: FakeDB) -> int:
    return int(db.rows("credits")[0]["balance"])


# ── The whole loop ─────────────────────────────────────────────────────────


def test_a_topup_returns_a_checkout_url_and_records_what_verify_will_need(live: Any) -> None:
    """The success shape, asserted for the first time.

    Both halves matter: the client cannot start a checkout without
    `checkout_url`, and `verify` cannot credit the right amount without the
    `credits` and `pack_key` this row carries.
    """
    client, db, _ = live

    res = client.post("/payments/topup", json={"pack_key": "booster"}, headers=token())

    assert res.status_code == 200
    body = res.json()
    assert body["link_id"] == "plink_test_1"
    assert body["checkout_url"].startswith("https://")

    (row,) = db.rows("payments")
    assert row["user_id"] == USER
    assert row["status"] == pay.PaymentStatus.CREATED.value
    assert row["credits"] == 300, "the Booster pack's credits, or verify grants the wrong number"
    assert row["pack_key"] == "booster"
    assert row["amount_inr"] == 399


def test_paying_credits_exactly_the_pack_the_row_recorded(live: Any) -> None:
    """The round trip that neither suite could do alone.

    `verify` reads the row `topup` wrote. A disagreement between the two — a
    renamed key, a missing amount — is invisible to a hand-seeded fixture and
    reaches the user as a payment that credited nothing.
    """
    client, db, gateway = live
    client.post("/payments/topup", json={"pack_key": "booster"}, headers=token())

    gateway.link_status = "paid"
    res = client.post("/payments/verify", json={"link_id": "plink_test_1"}, headers=token())

    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["outcome"] == "granted"
    assert body["credits_added"] == 300
    assert balance(db) == 320
    assert db.rows("payments")[0]["status"] == pay.PaymentStatus.PAID.value
    assert db.rows("payments")[0]["payment_id"] == "pay_test_1"


def test_verifying_twice_credits_once(live: Any) -> None:
    """The refresh case. The second call must report success and add nothing —
    saying "failed" to someone who paid sends them to pay again."""
    client, db, gateway = live
    client.post("/payments/topup", json={"pack_key": "starter"}, headers=token())
    gateway.link_status = "paid"

    first = client.post("/payments/verify", json={"link_id": "plink_test_1"}, headers=token())
    second = client.post("/payments/verify", json={"link_id": "plink_test_1"}, headers=token())

    assert first.json()["credits_added"] == 100
    assert second.json()["credits_added"] == 0
    assert second.json()["success"] is True, "a replay is a success from the user's side"
    assert second.json()["outcome"] == "already_credited"
    assert balance(db) == 120


def test_a_concurrent_verify_loses_the_claim_and_credits_nothing(live: Any) -> None:
    """Two requests read `created` and both try the swap. Exactly one wins.

    This is the V1 double-credit bug, reproduced at the level it actually
    happened: not two calls in sequence, but two that both saw the same row.
    """
    client, db, gateway = live
    client.post("/payments/topup", json={"pack_key": "power"}, headers=token())
    gateway.link_status = "paid"

    store = routes.SupabasePaymentStore(db)
    first_won = store.claim_payment(USER, "plink_test_1", "pay_a")
    second_won = store.claim_payment(USER, "plink_test_1", "pay_b")

    assert first_won is True
    assert second_won is False, "the second claim must not win, or both callers credit"
    assert db.rows("payments")[0]["payment_id"] == "pay_a"


def test_an_unpaid_link_credits_nothing_and_is_not_an_error(live: Any) -> None:
    """The user opened the checkout and walked away."""
    client, db, _ = live
    client.post("/payments/topup", json={"pack_key": "booster"}, headers=token())

    body = client.post("/payments/verify", json={"link_id": "plink_test_1"}, headers=token()).json()

    assert body["outcome"] == "not_paid_yet"
    assert body["success"] is False
    assert balance(db) == 20
    assert db.rows("payments")[0]["status"] == pay.PaymentStatus.CREATED.value


@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [
        ("expired", pay.PaymentStatus.EXPIRED),
        ("cancelled", pay.PaymentStatus.CANCELLED),
        ("failed", pay.PaymentStatus.FAILED),
    ],
)
def test_a_dead_payment_writes_its_real_outcome(
    live: Any, provider_status: str, expected: pay.PaymentStatus
) -> None:
    """The three terminal states, reached through HTTP for the first time.

    Before this, they were only ever produced by handing the service a status
    string directly. Nothing proved the ROUTE could reach them, and a payment
    stuck at `created` forever is a support ticket nobody can answer.
    """
    client, db, gateway = live
    client.post("/payments/topup", json={"pack_key": "booster"}, headers=token())

    gateway.link_status = provider_status
    body = client.post("/payments/verify", json={"link_id": "plink_test_1"}, headers=token()).json()

    assert body["success"] is False
    assert "haven't been charged" in body["message"]
    assert balance(db) == 20
    assert db.rows("payments")[0]["status"] == expected.value


def test_a_status_we_do_not_recognise_is_not_written(live: Any) -> None:
    """Razorpay adding a status must not put an unknown value in our column.

    `mark_payment` used to write whatever the provider said. The row is read
    back by `verify_topup` and compared against `PaymentStatus`, so an
    unrecognised value there is a payment that can never be resolved either
    way — and it would be written by the code path meant to resolve it.
    """
    client, db, gateway = live
    client.post("/payments/topup", json={"pack_key": "booster"}, headers=token())

    gateway.link_status = "partially_paid"
    body = client.post("/payments/verify", json={"link_id": "plink_test_1"}, headers=token()).json()

    assert body["outcome"] == "not_paid_yet"
    assert db.rows("payments")[0]["status"] == pay.PaymentStatus.CREATED.value


def test_a_provider_failure_records_no_payment_at_all(live: Any) -> None:
    """A link we never created must leave no row — a `created` row for a
    checkout that does not exist is a payment nobody can ever resolve."""
    client, db, gateway = live
    gateway.fail_create = True

    res = client.post("/payments/topup", json={"pack_key": "booster"}, headers=token())

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "provider_unavailable"
    assert "haven't been charged" in res.json()["detail"]["message"]
    assert db.rows("payments") == []


def test_the_link_carries_the_user_and_pack_razorpay_will_send_back(live: Any) -> None:
    """The `notes` are how a support query about a stray payment gets answered."""
    client, _db, gateway = live
    client.post("/payments/topup", json={"pack_key": "power"}, headers=token())

    (spec,) = gateway.created
    assert spec["notes"] == {"user_id": USER, "pack_key": "power"}
    assert spec["amount"] == 899 * 100, "Razorpay takes paise, not rupees"
    assert spec["currency"] == "INR"


def test_a_payment_that_cannot_be_credited_is_never_reported_as_success(live: Any) -> None:
    """The one case where the user IS charged and gets nothing.

    Found by writing this test. `credits.grant` returns None when the balance
    read fails, and that None was passed through as `balance_after` on a
    GRANTED result — so the response said "300 credits added", `success: true`,
    while the ledger was untouched. The payment row said `paid`, so every
    retry answered "already added — you were charged once".

    Charged, uncredited, and self-confirming: the surface that should have
    surfaced the problem was the one insisting nothing was wrong.
    """
    client, db, gateway = live
    client.post("/payments/topup", json={"pack_key": "booster"}, headers=token())
    gateway.link_status = "paid"
    db.failing.add("credits")

    body = client.post("/payments/verify", json={"link_id": "plink_test_1"}, headers=token()).json()

    assert body["success"] is False
    assert body["outcome"] == "grant_failed"
    assert body["credits_added"] == 0
    assert "couldn't add the credits" in body["message"]
    assert "payment went through" in body["message"], "it must admit the charge"


def test_a_payment_we_provably_did_not_credit_is_released_to_retry(live: Any) -> None:
    """Self-healing beats a support ticket.

    A failed balance READ means `apply_delta` was never called, so nothing was
    applied — releasing the claim is safe and cannot double-credit. The next
    page visit reconciles and the credits arrive.
    """
    client, db, gateway = live
    client.post("/payments/topup", json={"pack_key": "booster"}, headers=token())
    gateway.link_status = "paid"

    db.failing.add("credits")
    client.post("/payments/verify", json={"link_id": "plink_test_1"}, headers=token())
    assert db.rows("payments")[0]["status"] == pay.PaymentStatus.CREATED.value

    # Supabase comes back; the next reconcile finishes the job.
    db.failing.discard("credits")
    body = client.post("/payments/verify", json={"link_id": "plink_test_1"}, headers=token()).json()

    assert body["outcome"] == "granted"
    assert body["credits_added"] == 300
    assert balance(db) == 320


def test_a_grant_of_unknown_outcome_keeps_the_claim(live: Any) -> None:
    """The other half of the same decision, and it goes the other way.

    If the WRITE raises we do not know whether it landed, so the claim must
    stay — releasing it could credit the same payment twice. Only a provably
    unapplied grant is safe to retry automatically.
    """
    client, db, gateway = live
    client.post("/payments/topup", json={"pack_key": "booster"}, headers=token())
    gateway.link_status = "paid"

    class Exploding:
        def grant(self, *_a: Any, **_k: Any) -> int | None:
            raise RuntimeError("the update timed out; did it land?")

    result = pay.verify_topup(
        routes.SupabasePaymentStore(db),
        Exploding(),
        user_id=USER,
        link_id="plink_test_1",
        provider_status="paid",
    )

    assert result.outcome is pay.GrantOutcome.GRANT_FAILED
    assert result.user_sees_success is False
    assert db.rows("payments")[0]["status"] == pay.PaymentStatus.PAID.value


# ── Subscriptions: the same defect, credited monthly ───────────────────────


def a_subscription(cycles: int = 0) -> dict[str, Any]:
    return {
        "user_id": USER,
        "sub_id": "sub_test_1",
        "plan_key": "plus",
        "cycles_credited": cycles,
        "status": "active",
    }


def test_a_paid_cycle_credits_the_plan_and_advances_the_counter(live: Any) -> None:
    """The subscription happy path, which no test had run either.

    (This assertion was written under a name claiming it checked the FAILURE
    case, and passed — a test whose name and body disagree is worse than a
    missing test, because the gap looks covered.)
    """
    _client, db, _ = live
    db.tables["user_subscriptions"] = [a_subscription()]

    result = pay.verify_subscription_cycles(
        routes.SupabasePaymentStore(db),
        routes._CreditGranter(SupabaseCreditStore(db)),
        user_id=USER,
        sub_id="sub_test_1",
        provider_paid_count=1,
        provider_status="active",
    )

    assert result.outcome is pay.GrantOutcome.GRANTED
    assert balance(db) == 270  # the Plus plan's 250, on top of the starting 20
    assert db.rows("user_subscriptions")[0]["cycles_credited"] == 1


def test_a_failed_cycle_grant_winds_the_counter_back(live: Any) -> None:
    """So the month is retried rather than lost."""
    _client, db, _ = live
    db.tables["user_subscriptions"] = [a_subscription()]
    db.failing.add("credits")

    result = pay.verify_subscription_cycles(
        routes.SupabasePaymentStore(db),
        routes._CreditGranter(SupabaseCreditStore(db)),
        user_id=USER,
        sub_id="sub_test_1",
        provider_paid_count=1,
        provider_status="active",
    )

    assert result.outcome is pay.GrantOutcome.GRANT_FAILED
    assert result.user_sees_success is False
    assert db.rows("user_subscriptions")[0]["cycles_credited"] == 0, (
        "a cycle we did not pay must not be recorded as paid"
    )

    # And the retry finishes it.
    db.failing.discard("credits")
    again = pay.verify_subscription_cycles(
        routes.SupabasePaymentStore(db),
        routes._CreditGranter(SupabaseCreditStore(db)),
        user_id=USER,
        sub_id="sub_test_1",
        provider_paid_count=1,
        provider_status="active",
    )
    assert again.outcome is pay.GrantOutcome.GRANTED
    assert balance(db) == 270


def test_months_offline_are_credited_together(live: Any) -> None:
    """`paid_count` is cumulative, so the arrears are exactly the difference."""
    _client, db, _ = live
    db.tables["user_subscriptions"] = [a_subscription(cycles=1)]

    result = pay.verify_subscription_cycles(
        routes.SupabasePaymentStore(db),
        routes._CreditGranter(SupabaseCreditStore(db)),
        user_id=USER,
        sub_id="sub_test_1",
        provider_paid_count=4,
        provider_status="active",
    )

    assert result.credits_added == 250 * 3
    assert "3 paid months" in result.message
    assert balance(db) == 20 + 750
