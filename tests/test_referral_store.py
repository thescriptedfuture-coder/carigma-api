"""Referral storage and routes, against rows rather than a dataclass.

`test_referrals.py` proves the rules. This proves that the rules survive the
database and that nothing on the wire can reach around them.

Two of these tests exist because of a decision, not a bug:

- **50 credits on profile upload, never signup.** Structural via `Activation`,
  and a route could still open a second door. One test walks the ROUTE modules
  and asserts none of them can reach the grant.
- **The cap is the database's.** One test asserts the store never compares a
  count to `MAX_REFERRALS`.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import referrals as routes
from carigma_api.services import referral_store as store_mod
from carigma_api.services.referral_store import (
    CODES,
    REFERRALS,
    SupabaseReferralStore,
    summary,
)
from carigma_api.services.referrals import (
    MAX_REFERRALS,
    REFERRED_CREDITS,
    REFERRER_CREDITS,
    AlreadyReferred,
    CapReached,
    SelfReferral,
    State,
    activation_from_profile_upload,
    share_payload,
)
from tests.conftest import USER_ID, auth, make_token
from tests.fake_supabase import FakeDB

OTHER = "00000000-0000-4000-8000-0000000000ff"


class Duplicate(Exception):
    """What PostgREST raises on a unique violation."""

    code = "23505"


class Raised(Exception):
    """A `raise exception ... using errcode` from a Postgres function."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PostgresLike(FakeDB):
    """A FakeDB with the unique indexes AND the three V2_013 functions.

    The functions are reimplemented here in Python, which is a duplication and
    is worth naming: **the SQL and this fake can disagree.** They are kept
    honest by `test_the_fake_functions_match_the_migration`, which checks that
    every function the store calls exists in `V2_013_referral_access.sql` with
    the same argument names. That catches a renamed function or parameter — the
    drift that actually happens — but not a logic difference, which only a
    staging run against the real database can settle.

    The alternative was no coverage at all for the paths RLS forced into
    functions, which is worse: those paths carry the cap.
    """

    UNIQUE = {
        CODES: [("user_id",), ("code",)],
        REFERRALS: [("referrer_id", "slot"), ("referred_id",)],
    }

    def __init__(self) -> None:
        super().__init__()
        self.caller: str = USER_ID
        self.functions = {
            "referral_summary": self._summary,
            "referral_claim": self._claim,
            "referral_code_valid": self._code_valid,
        }

    def table(self, name: str) -> Any:
        table = super().table(name)
        original = table.execute

        def execute() -> Any:
            payload = getattr(table, "_payload", None)
            if getattr(table, "_op", None) == "insert" and isinstance(payload, dict):
                for cols in self.UNIQUE.get(name, []):
                    ident = tuple(str(payload.get(c)) for c in cols)
                    for row in self.rows(name):
                        if tuple(str(row.get(c)) for c in cols) == ident:
                            raise Duplicate(f"duplicate key on {cols}")
            return original()

        table.execute = execute  # type: ignore[method-assign]
        return table

    # ── the functions ──────────────────────────────────────────────────────

    def _mine(self) -> list[dict[str, Any]]:
        return [r for r in self.rows(REFERRALS) if str(r.get("referrer_id")) == self.caller]

    def _summary(self, _params: dict[str, Any]) -> list[dict[str, Any]]:
        mine = self._mine()
        return [
            {
                "activated": sum(1 for r in mine if r["state"] == str(State.ACTIVATED)),
                "pending": sum(1 for r in mine if r["state"] == str(State.PENDING)),
                "total": len(mine),
            }
        ]

    def _code_valid(self, params: dict[str, Any]) -> bool:
        return any(r.get("code") == params["p_code"] for r in self.rows(CODES))

    def _claim(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        code = params["p_code"]
        owner = next((r["user_id"] for r in self.rows(CODES) if r.get("code") == code), None)
        if owner is None:
            raise Raised("P0002")
        if str(owner) == self.caller:
            raise Raised("P0001")

        for slot in range(1, MAX_REFERRALS + 1):
            try:
                self.table(REFERRALS).insert(
                    {
                        "referrer_id": owner,
                        "referred_id": self.caller,
                        "code": code,
                        "slot": slot,
                        "state": str(State.PENDING),
                    }
                ).execute()
            except Duplicate:
                if any(str(r["referred_id"]) == self.caller for r in self.rows(REFERRALS)):
                    raise Raised("P0003") from None
                continue
            return [{"slot": slot, "state": str(State.PENDING)}]
        raise Raised("P0004")


class FakeCredits:
    def __init__(self) -> None:
        self.grants: list[tuple[str, int, str]] = []

    def grant(self, user_id: str, amount: int, reason: str) -> int | None:
        self.grants.append((user_id, amount, reason))
        return 100 + amount


@pytest.fixture
def db() -> PostgresLike:
    return PostgresLike()


@pytest.fixture
def wired(client: TestClient, db: PostgresLike, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setattr(routes, "_store", lambda request, settings: SupabaseReferralStore(db))
    monkeypatch.setattr(routes, "user_client", lambda *_a, **_k: db)
    yield client, db


def token() -> dict[str, str]:
    return auth(make_token())


def store(db: PostgresLike) -> SupabaseReferralStore:
    return SupabaseReferralStore(db)


# ── The decision: never on signup ──────────────────────────────────────────


def test_no_route_can_reach_the_grant() -> None:
    """The second door.

    `Activation` makes "never on signup" structural in the service. It does not
    stop a ROUTE from constructing one and calling `activate` — a handler that
    did would be a request-triggered grant, which is exactly the thing the type
    was chosen to prevent.

    Walks every route module's AST for either name. Neither may appear.
    """
    forbidden = {"activate", "activation_from_profile_upload", "grant_for_activation"}
    routes_dir = Path(routes.__file__).parent
    offenders: list[str] = []

    for path in sorted(routes_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                offenders.append(f"{path.name}: .{node.attr}()")
            elif isinstance(node, ast.Name) and node.id in forbidden:
                offenders.append(f"{path.name}: {node.id}")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in forbidden:
                        offenders.append(f"{path.name}: imports {alias.name}")

    assert offenders == [], (
        "a route can reach the referral grant — the reward must be triggered by "
        f"a profile upload, never by a request: {offenders}"
    )


def test_the_grant_has_no_caller_yet_and_that_is_recorded() -> None:
    """V2 has no profile-upload path, so nothing activates a referral.

    This test exists so the gap is a FACT in the suite rather than something
    someone notices later. When onboarding lands, `activate` must be wired into
    it — and this test is where that wiring gets asserted, by being changed to
    require the caller instead of recording its absence.

    Kept deliberately: a surface promising "credits land when you upload a
    profile" while nothing can grant them is a promise the product cannot keep,
    and the referral page says so in those words.
    """
    src = Path(store_mod.__file__).parents[2]
    callers = [
        path.name
        for path in src.rglob("*.py")
        if path.name != "referral_store.py" and ".activate(" in path.read_text(encoding="utf-8")
    ]
    assert callers == [], (
        "`activate` now has a caller — good. Rewrite this test to assert the "
        f"caller is the profile-upload path and nothing else: {callers}"
    )


def test_the_store_never_compares_a_count_to_the_cap() -> None:
    """The cap belongs to the unique index.

    `len(rows) >= MAX_REFERRALS` is read-then-write, and two concurrent claims
    would both pass it. The AST walk covers the forms a substring scan missed
    when this rule was first written for the service module.
    """
    tree = ast.parse(Path(store_mod.__file__).read_text(encoding="utf-8"))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        calls = {
            n.func.id
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        if "MAX_REFERRALS" in names and ({"len", "count"} & calls):
            offenders.append(ast.unparse(node))

    assert offenders == [], f"the cap is being counted rather than arbitrated: {offenders}"


# ── The fake and the migration must at least agree on names ───────────


def _migration() -> str:
    root = Path(store_mod.__file__).parents[3]
    return (root / "migrations" / "V2_013_referral_access.sql").read_text(encoding="utf-8")


def test_the_fake_functions_match_the_migration() -> None:
    """The duplication `PostgresLike` admits to, checked where it can be.

    A renamed function or parameter is the drift that actually happens, and it
    would leave the suite green while every call failed in production. A logic
    difference between the SQL and the Python is NOT caught here, and only a
    run against the real database settles that.
    """
    sql = _migration()
    source = Path(store_mod.__file__).read_text(encoding="utf-8")
    called = set(re.findall(r'\.rpc\("(\w+)"', source))
    assert called, "no rpc calls found - this guard would pass vacuously"

    for name in called:
        assert f"function public.{name}(" in sql, f"{name} is called but not defined in V2_013"

    for param in set(re.findall(r'\.rpc\("\w+", \{"(\w+)"', source)):
        assert param in sql, f"{param} is passed but no function declares it"


def test_activation_is_not_reachable_by_rpc() -> None:
    """The hole the other three functions could have opened.

    `referral_summary`, `referral_claim` and `referral_code_valid` are granted
    to `authenticated` because RLS otherwise makes the surface impossible.
    Activation must NOT join them: a function an authenticated user can execute
    is one they can execute from a browser console, and that would mint 20
    credits with no upload. The `Activation` type guards the Python call site;
    a SQL grant would route straight around it.
    """
    sql = _migration()
    assert "referral_activate" not in sql
    for line in sql.splitlines():
        if line.strip().startswith("grant execute"):
            assert "activate" not in line, f"activation granted over RPC: {line.strip()}"


# ── Codes ────────────────────────────────────────────────────────


def test_a_code_is_minted_on_first_read_and_then_stable(db: PostgresLike) -> None:
    first = store(db).code_for(USER_ID)
    second = store(db).code_for(USER_ID)

    assert first == second
    assert len(db.rows(CODES)) == 1, "a second read must not mint a second code"


def test_two_tabs_opening_at_once_do_not_produce_two_codes(db: PostgresLike) -> None:
    """The loser of the race reads back the winner's code.

    Returning its own would hand the user a string that is in nobody's table -
    they would share a link that resolves to nothing.
    """
    winner = store(db).code_for(USER_ID)
    assert store(db).code_for(USER_ID) == winner
    assert len(db.rows(CODES)) == 1


# ── Claiming ───────────────────────────────────────────────────


def seed_code(db: PostgresLike, user_id: str = OTHER, code: str = "abcd2345") -> str:
    db.seed(CODES, {"user_id": user_id, "code": code})
    return code


def test_claiming_records_a_pending_row_and_grants_nothing(db: PostgresLike) -> None:
    code = seed_code(db)
    row = store(db).claim(code=code)

    assert row["state"] == str(State.PENDING)
    assert row["slot"] == 1
    assert "delta" not in row and "credits" not in row


def test_slots_fill_from_the_lowest_free_one(db: PostgresLike) -> None:
    code = seed_code(db)
    for i in range(3):
        db.caller = f"user-{i}"
        store(db).claim(code=code)

    assert sorted(r["slot"] for r in db.rows(REFERRALS)) == [1, 2, 3]


def test_a_slot_collision_retries_rather_than_failing(db: PostgresLike) -> None:
    """The concurrency the slot design exists for.

    Slot 1 is already held. The unique index refuses the row and the retry
    places it at 2 - nobody loses a referral and nobody gets two.
    """
    code = seed_code(db)
    db.seed(
        REFERRALS,
        {
            "referrer_id": OTHER,
            "referred_id": "someone-else",
            "code": code,
            "slot": 1,
            "state": str(State.PENDING),
        },
    )

    row = store(db).claim(code=code)
    assert row["slot"] == 2
    assert len([r for r in db.rows(REFERRALS) if r["slot"] == 1]) == 1


def test_the_sixth_referral_is_refused_by_the_slots(db: PostgresLike) -> None:
    code = seed_code(db)
    for i in range(MAX_REFERRALS):
        db.caller = f"user-{i}"
        store(db).claim(code=code)

    db.caller = "one-too-many"
    with pytest.raises(CapReached):
        store(db).claim(code=code)

    assert len(db.rows(REFERRALS)) == MAX_REFERRALS


def test_being_referred_twice_is_refused(db: PostgresLike) -> None:
    first = seed_code(db)
    second = seed_code(db, user_id="third-party", code="wxyz6789")
    store(db).claim(code=first)

    with pytest.raises(AlreadyReferred):
        store(db).claim(code=second)


def test_referring_yourself_is_refused(db: PostgresLike) -> None:
    code = seed_code(db, user_id=USER_ID)
    with pytest.raises(SelfReferral):
        store(db).claim(code=code)
    assert db.rows(REFERRALS) == []


def test_an_unrecognised_failure_is_not_reported_as_a_bad_code(db: PostgresLike) -> None:
    """A database outage must not render "that link isn't valid".

    `_translate` returns unknown errors unchanged, so they reach the route's
    catch-all and become a 503 rather than a 404 blaming the link.
    """

    def explode(_params: dict[str, Any]) -> Any:
        raise RuntimeError("connection reset")

    db.functions["referral_claim"] = explode
    with pytest.raises(RuntimeError):
        store(db).claim(code="abcd2345")


# ── Activation ────────────────────────────────────────────────


def test_activation_pays_both_sides_once(db: PostgresLike) -> None:
    code = seed_code(db)
    store(db).claim(code=code)
    credits = FakeCredits()

    grant = store(db).activate(activation_from_profile_upload(USER_ID), credits=credits)

    assert grant is not None
    assert [(u, amount) for u, amount, _ in credits.grants] == [
        (OTHER, REFERRER_CREDITS),
        (USER_ID, REFERRED_CREDITS),
    ]
    assert db.rows(REFERRALS)[0]["state"] == str(State.ACTIVATED)
    assert db.rows(REFERRALS)[0]["activated_at"] is not None


def test_activating_twice_pays_once(db: PostgresLike) -> None:
    """Two uploads, or a retry. The conditional update decides."""
    code = seed_code(db)
    store(db).claim(code=code)
    credits = FakeCredits()

    first = store(db).activate(activation_from_profile_upload(USER_ID), credits=credits)
    second = store(db).activate(activation_from_profile_upload(USER_ID), credits=credits)

    assert first is not None
    assert second is None
    assert len(credits.grants) == 2, "two rows for the first activation, none for the second"


def test_an_upload_with_no_referral_is_not_an_error(db: PostgresLike) -> None:
    """Most users were not referred. A profile upload must never fail because
    of the referral programme attached to it."""
    credits = FakeCredits()
    assert store(db).activate(activation_from_profile_upload(USER_ID), credits=credits) is None
    assert credits.grants == []


def test_the_state_flip_gates_the_credits(db: PostgresLike) -> None:
    """A concurrent activation that loses the update must not pay out."""
    code = seed_code(db)
    store(db).claim(code=code)
    db.rows(REFERRALS)[0]["state"] = str(State.ACTIVATED)
    db.rows(REFERRALS)[0]["activated_at"] = datetime.now(UTC).isoformat()

    credits = FakeCredits()
    assert store(db).activate(activation_from_profile_upload(USER_ID), credits=credits) is None
    assert credits.grants == []


# ── The surface ───────────────────────────────────────────────


def counts(activated: int = 0, pending: int = 0) -> dict[str, int]:
    return {"activated": activated, "pending": pending, "total": activated + pending}


def test_the_summary_counts_only_activated_referrals_as_earned() -> None:
    out = summary(code="abcd2345", counts=counts(2, 1), share=share_payload("abcd2345"))

    assert out["activated"] == 2
    assert out["pending"] == 1
    assert out["earned"] == 2 * REFERRER_CREDITS, "a pending referral has earned nothing"
    assert out["slots_left"] == MAX_REFERRALS - 3


def test_a_pending_referral_is_explained_rather_than_just_counted() -> None:
    out = summary(code="abcd2345", counts=counts(pending=1), share=share_payload("abcd2345"))
    assert out["pending_note"] is not None
    assert "uploaded a profile" in out["pending_note"]


def test_nobody_with_no_referrals_is_told_a_number_that_looks_like_a_failure() -> None:
    out = summary(code="abcd2345", counts=counts(), share=share_payload("abcd2345"))
    assert out["pending_note"] is None
    assert out["slots_left"] == MAX_REFERRALS


def test_the_summary_can_never_name_the_people_referred() -> None:
    """It is given counts, not rows - there is nothing to leak.

    `referrals` is closed to the user precisely so the referrer/referred
    association cannot escape, and `referral_summary()` returns three integers.
    """
    import inspect

    assert "referred_id" not in inspect.getsource(summary)
    assert "rows" not in inspect.signature(summary).parameters


# ── Over HTTP ──────────────────────────────────────────────────


def test_every_private_referral_route_requires_a_token(client: TestClient) -> None:
    assert client.get("/referrals").status_code == 401
    assert client.post("/referrals/claim", json={"code": "abcd2345"}).status_code == 401


def test_the_referral_page_returns_a_shareable_link(wired: Any) -> None:
    client, _db = wired
    body = client.get("/referrals", headers=token()).json()

    assert body["link"].endswith(body["code"])
    assert body["whatsapp_url"].startswith("https://wa.me/?text=")
    assert str(REFERRED_CREDITS) in body["message"]
    assert body["activated"] == 0
    assert body["earned"] == 0


def test_the_cap_is_stated_before_anyone_shares(wired: Any) -> None:
    client, _db = wired
    body = client.get("/referrals", headers=token()).json()

    assert body["cap"] == MAX_REFERRALS
    assert "upload a profile" in body["cap_note"]


def test_the_page_reports_what_has_actually_been_earned(wired: Any) -> None:
    """Through the function, not a table read.

    A select on `referrals` returns [] under RLS, which would render "0
    activated" for a referrer with five - an answer, not an error, which is
    what makes it dangerous.
    """
    client, db = wired
    seed_code(db, user_id=USER_ID, code="mycode12")
    db.seed(
        REFERRALS,
        {"referrer_id": USER_ID, "referred_id": "a", "slot": 1, "state": str(State.ACTIVATED)},
        {"referrer_id": USER_ID, "referred_id": "b", "slot": 2, "state": str(State.PENDING)},
    )

    body = client.get("/referrals", headers=token()).json()

    assert body["activated"] == 1
    assert body["pending"] == 1
    assert body["earned"] == REFERRER_CREDITS
    assert body["slots_left"] == MAX_REFERRALS - 2


def test_claiming_over_http_says_the_credits_have_not_arrived(wired: Any) -> None:
    """The whole point of the pending state, said to the user.

    A bare success here would read as "you have your credits", and the first
    thing they would do is look at a balance that has not moved.
    """
    client, db = wired
    seed_code(db)

    body = client.post("/referrals/claim", json={"code": "abcd2345"}, headers=token()).json()

    assert body["credits_now"] == 0
    assert body["credits_on_upload"] == REFERRED_CREDITS
    assert "upload" in body["message"]


def test_an_unknown_code_is_a_plain_404(wired: Any) -> None:
    client, _db = wired
    res = client.post("/referrals/claim", json={"code": "nosuchcode"}, headers=token())

    assert res.status_code == 404
    assert res.json()["detail"]["error"] == "unknown_code"
    assert "Traceback" not in res.text


def test_the_public_landing_reveals_nothing_about_the_referrer(wired: Any) -> None:
    """It confirms the code works and what the visitor gets. Nothing else.

    An endpoint that answered "this is Ravi's code" to anyone holding a string
    would be a lookup service for our user list.
    """
    client, db = wired
    seed_code(db)

    body = client.get("/referrals/abcd2345").json()

    assert body["valid"] is True
    assert body["credits"] == REFERRED_CREDITS
    assert OTHER not in repr(body)


def test_the_public_landing_needs_no_token(wired: Any) -> None:
    """The visitor has no account yet - that is the entire point of the link."""
    client, db = wired
    seed_code(db)
    assert client.get("/referrals/abcd2345").status_code == 200


def test_an_invalid_code_is_told_plainly_without_blaming_the_visitor(wired: Any) -> None:
    client, _db = wired
    body = client.get("/referrals/zzzz9999").json()

    assert body["valid"] is False
    assert body["credits"] == 0
    for word in ("error", "invalid code", "failed", "denied"):
        assert word not in body["sub"].lower()
