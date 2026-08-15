"""The three deferred items: rate limits, durable runs, idempotency replay.

The one that costs real money is idempotency. A double-tap on "Generate" must
return the first answer and charge once — and the charge count on a replay must
be ZERO, or a client that sums receipts double-counts a single debit.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from carigma_api.services.credits import CreditReceipt
from carigma_api.services.ratelimit import (
    AGENT_BURST,
    PUBLIC_BURST,
    Bucket,
    RateLimiter,
    SharedResourceMisuse,
    assert_not_shared_resource,
    client_ip,
)
from carigma_api.services.run_store import (
    IdempotencyConflict,
    SupabaseRunStore,
    replay_or_none,
    replay_payload,
)
from carigma_api.services.runs import AgentRun, RunStatus, RunStep

# ── Limiter 1: per user, per agent ─────────────────────────────────────────


def test_a_burst_is_allowed_then_the_next_request_is_refused() -> None:
    rl = RateLimiter()
    for i in range(AGENT_BURST):
        assert rl.check_agent_run("u1", "profile").allowed is True, f"request {i} refused"

    refused = rl.check_agent_run("u1", "profile")
    assert refused.allowed is False
    assert refused.scope == "agent_run"


def test_a_refusal_always_says_a_usable_retry_time() -> None:
    """ "Retry in 0s" tells someone nothing."""
    rl = RateLimiter()
    for _ in range(AGENT_BURST):
        rl.check_agent_run("u1", "profile")

    refused = rl.check_agent_run("u1", "profile")
    assert refused.retry_after_seconds >= 1


def test_running_a_different_agent_is_not_blocked() -> None:
    """Profile Analyst and Career Scout in the same minute is normal use. A
    combined bucket would punish it."""
    rl = RateLimiter()
    for _ in range(AGENT_BURST):
        rl.check_agent_run("u1", "profile")

    assert rl.check_agent_run("u1", "profile").allowed is False
    assert rl.check_agent_run("u1", "jobs").allowed is True


def test_one_user_hitting_the_limit_does_not_block_another() -> None:
    rl = RateLimiter()
    for _ in range(AGENT_BURST + 2):
        rl.check_agent_run("noisy", "profile")

    assert rl.check_agent_run("noisy", "profile").allowed is False
    assert rl.check_agent_run("quiet", "profile").allowed is True


def test_the_message_does_not_confuse_rate_limiting_with_credits() -> None:
    """`rate_limited` is not `quota_exhausted`. Telling someone to buy credits
    they already have would be a lie about why we said no."""
    rl = RateLimiter()
    for _ in range(AGENT_BURST):
        rl.check_agent_run("u1", "profile")

    payload = rl.check_agent_run("u1", "profile").as_dict()
    assert payload["error"] == "rate_limited"
    assert "nothing was charged" in payload["message"]
    assert "credit" not in payload["message"].lower()


# ── Limiter 2: per IP ──────────────────────────────────────────────────────


def test_the_public_limiter_is_more_generous_than_the_agent_one() -> None:
    """A shared office NAT is one IP with many honest people behind it."""
    assert PUBLIC_BURST > AGENT_BURST

    rl = RateLimiter()
    for i in range(PUBLIC_BURST):
        assert rl.check_public("1.2.3.4").allowed is True, f"request {i} refused"
    assert rl.check_public("1.2.3.4").allowed is False


def test_one_ip_hitting_the_limit_does_not_block_another() -> None:
    rl = RateLimiter()
    for _ in range(PUBLIC_BURST + 2):
        rl.check_public("1.2.3.4")

    assert rl.check_public("1.2.3.4").allowed is False
    assert rl.check_public("5.6.7.8").allowed is True


def test_the_client_ip_is_the_first_forwarded_entry_not_a_proxy() -> None:
    """Keying on a proxy would put every user in one bucket."""
    assert client_ip({"x-forwarded-for": "203.0.113.7, 10.0.0.1, 10.0.0.2"}) == "203.0.113.7"
    assert client_ip({"X-Forwarded-For": "203.0.113.7"}) == "203.0.113.7"


def test_a_missing_forwarded_header_falls_back_rather_than_crashing() -> None:
    assert client_ip({}) == "unknown"
    assert client_ip({"x-real-ip": "198.51.100.9"}) == "198.51.100.9"


def test_an_empty_forwarded_header_does_not_become_the_key() -> None:
    """Otherwise every such caller shares one bucket named "" — which is a
    global limiter by accident."""
    assert client_ip({"x-forwarded-for": ""}) == "unknown"
    assert client_ip({"x-forwarded-for": "  ,10.0.0.1"}) == "unknown"


# ── Limiter 3 is deliberately NOT here ─────────────────────────────────────


@pytest.mark.parametrize("name", ["jsearch", "JSearch", "adzuna"])
def test_rate_limiting_a_shared_upstream_quota_here_is_refused(name: str) -> None:
    """§21 Q7: the JSearch quota is a SHARED resource owned by the jobs cache
    and JSEARCH_DAILY_CAP. A second limiter over it would duplicate or fight
    the first, and fighting is the worse failure — two limiters each believing
    the other let the request through."""
    with pytest.raises(SharedResourceMisuse, match="SHARED"):
        assert_not_shared_resource(name)


def test_the_guard_fires_through_the_agent_limiter_too() -> None:
    """Not just when called directly — the realistic mistake is passing
    "jsearch" as an agent name."""
    with pytest.raises(SharedResourceMisuse):
        RateLimiter().check_agent_run("u1", "jsearch")


def test_real_agent_names_pass_the_guard() -> None:
    for agent in ("profile", "jobs", "content", "interview", "cv", "naukri"):
        assert_not_shared_resource(agent)


# ── The bucket itself ──────────────────────────────────────────────────────


def test_tokens_refill_continuously_rather_than_in_windows() -> None:
    """A stepped window makes someone wait the full period for one token."""
    bucket = Bucket(capacity=5, refill_per_second=1.0)
    now = 1000.0
    for _ in range(5):
        assert bucket.take(now) is True
    assert bucket.take(now) is False

    # Half a second later: still nothing. A full second: one token.
    assert bucket.take(now + 0.5) is False
    assert bucket.take(now + 1.0) is True


def test_a_bucket_never_refills_past_its_capacity() -> None:
    bucket = Bucket(capacity=3, refill_per_second=1.0)
    bucket.take(1000.0)
    # An hour of idleness must not bank 3,600 tokens.
    for _ in range(3):
        assert bucket.take(4600.0) is True
    assert bucket.take(4600.0) is False


# ── Idempotency replay ─────────────────────────────────────────────────────


class FakeStore:
    def __init__(self, runs: dict[tuple[str, str], AgentRun] | None = None):
        self.runs = runs or {}
        self.lookups: list[tuple[str, str]] = []

    def find_by_idempotency_key(self, user_id: str, key: str) -> AgentRun | None:
        self.lookups.append((user_id, key))
        return self.runs.get((user_id, key))


def finished_run(agent: str = "profile", charged: int = 10) -> AgentRun:
    run = AgentRun(id="run_1", user_id="u1", agent=agent, status=RunStatus.SUCCEEDED)
    run.result = {"profileScore": 71}
    run.credits = CreditReceipt(charged, 90, "Profile Analyst run")
    run.finished_at = datetime.now(UTC)
    return run


def test_no_key_means_no_replay_and_no_lookup() -> None:
    store = FakeStore()
    assert replay_or_none(store, "u1", None, agent="profile") is None
    assert store.lookups == [], "a lookup without a key is a wasted query"


def test_an_unseen_key_starts_a_fresh_run() -> None:
    assert replay_or_none(FakeStore(), "u1", "key-1", agent="profile") is None


def test_a_repeated_key_returns_the_original_run() -> None:
    """The double-tap on Generate."""
    original = finished_run()
    store = FakeStore({("u1", "key-1"): original})

    replayed = replay_or_none(store, "u1", "key-1", agent="profile")
    assert replayed is original


def test_a_replay_reports_zero_charged() -> None:
    """THE rule. The original charge already happened and was reported once;
    repeating the number makes a client that sums receipts double-count."""
    payload = replay_payload(finished_run(charged=10))

    assert payload["replayed"] is True
    assert payload["credits"]["charged"] == 0
    assert "charged once" in payload["note"]


def test_a_replay_still_carries_the_original_result() -> None:
    payload = replay_payload(finished_run())
    assert payload["result"] == {"profileScore": 71}
    assert payload["status"] == "succeeded"


def test_one_users_key_is_never_another_users_replay() -> None:
    store = FakeStore({("u1", "key-1"): finished_run()})
    assert replay_or_none(store, "u2", "key-1", agent="profile") is None


def test_reusing_a_key_for_a_different_agent_is_an_error() -> None:
    """A key promises two requests are the same request. Answering with another
    agent's result would be worse than refusing."""
    store = FakeStore({("u1", "key-1"): finished_run(agent="profile")})

    with pytest.raises(IdempotencyConflict, match="profile"):
        replay_or_none(store, "u1", "key-1", agent="jobs")


def test_an_in_flight_run_is_returned_rather_than_waited_on() -> None:
    """The client polls the run protocol anyway; blocking here would hold a
    connection open for the length of an agent run."""
    running = AgentRun(id="run_2", user_id="u1", agent="jobs", status=RunStatus.RUNNING)
    store = FakeStore({("u1", "key-2"): running})

    replayed = replay_or_none(store, "u1", "key-2", agent="jobs")
    assert replayed is running
    assert replayed.status is RunStatus.RUNNING


# ── The durable store ──────────────────────────────────────────────────────


class FakeTable:
    def __init__(self, rows: list[dict[str, Any]] | None = None, explode: bool = False):
        self.rows = rows or []
        self.explode = explode
        self.upserted: list[dict[str, Any]] = []
        self._filters: dict[str, Any] = {}

    def table(self, name: str) -> FakeTable:
        return self

    def upsert(self, row: dict[str, Any]) -> FakeTable:
        if self.explode:
            raise RuntimeError("db down")
        self.upserted.append(row)
        return self

    def select(self, *_: Any) -> FakeTable:
        self._filters = {}
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def limit(self, _: int) -> FakeTable:
        return self

    def execute(self) -> Any:
        if self.explode:
            raise RuntimeError("db down")
        matched = [
            r for r in self.rows if all(str(r.get(k)) == str(v) for k, v in self._filters.items())
        ]
        return type("Res", (), {"data": matched})()


def test_a_run_round_trips_through_the_table() -> None:
    table = FakeTable()
    store = SupabaseRunStore(table)

    run = finished_run()
    run.steps = [RunStep("analyse", "Reading your profile", "done", count=7)]
    store.save(run)

    row = table.upserted[0]
    assert row["id"] == "run_1"
    assert row["status"] == "succeeded"
    assert row["credits_charged"] == 10
    assert row["steps"][0]["count"] == 7


def test_the_idempotency_key_is_written_with_the_run() -> None:
    table = FakeTable()
    store = SupabaseRunStore(table)
    run = finished_run()

    store.remember_idempotency("u1", "key-1", run.id)
    store.save(run)

    assert table.upserted[0]["idempotency_key"] == "key-1"


def test_a_run_without_a_key_stores_null_not_an_empty_string() -> None:
    """The unique index is partial (`where idempotency_key is not null`). Empty
    strings would collide with each other and block unrelated runs."""
    table = FakeTable()
    SupabaseRunStore(table).save(finished_run())
    assert table.upserted[0]["idempotency_key"] is None


def test_reading_a_run_back_restores_its_status_and_steps() -> None:
    table = FakeTable(
        [
            {
                "id": "run_9",
                "user_id": "u1",
                "agent": "jobs",
                "status": "empty",
                "steps": [{"step": "scan", "label": "Scanning", "status": "done", "count": 0}],
                "result": None,
                "error": None,
                "credits_charged": 0,
                "started_at": "2026-08-07T09:00:00Z",
                "finished_at": "2026-08-07T09:01:00Z",
            }
        ]
    )
    run = SupabaseRunStore(table).get("run_9")

    assert run is not None
    # `empty` is a first-class non-failure and must survive the round trip.
    assert run.status is RunStatus.EMPTY
    assert run.credits is not None and run.credits.charged == 0
    assert run.steps[0].count == 0


def test_a_persistence_failure_never_aborts_the_run() -> None:
    """The user is watching a run finish. Losing cross-instance visibility is
    bad; killing their run over it is worse."""
    store = SupabaseRunStore(FakeTable(explode=True))
    store.save(finished_run())  # must not raise


def test_a_failed_read_returns_none_rather_than_raising() -> None:
    assert SupabaseRunStore(FakeTable(explode=True)).get("run_1") is None
    assert SupabaseRunStore(FakeTable(explode=True)).find_by_idempotency_key("u1", "k") is None


def test_an_idempotency_lookup_is_scoped_to_the_user() -> None:
    table = FakeTable(
        [
            {
                "id": "run_1",
                "user_id": "u1",
                "agent": "profile",
                "status": "succeeded",
                "steps": [],
                "credits_charged": 10,
                "idempotency_key": "key-1",
                "started_at": "2026-08-07T09:00:00Z",
            }
        ]
    )
    store = SupabaseRunStore(table)

    assert store.find_by_idempotency_key("u1", "key-1") is not None
    assert store.find_by_idempotency_key("u2", "key-1") is None


# ── Wired into a real endpoint ─────────────────────────────────────────────
# The service-layer tests above prove the mechanisms. These prove they are
# actually connected — a limiter nobody calls is a module, not a limit.


@pytest.fixture
def frozen_limiter_clock(monkeypatch: Any):  # type: ignore[no-untyped-def]
    """Stop the token bucket refilling while a burst is in flight.

    The limiter reads `time.monotonic()`. A burst of six HTTP requests through
    TestClient takes real time, and as the suite grew past 800 tests that
    became enough for the bucket to refill mid-burst — so the sixth request was
    allowed and "the limiter never fired".

    CONTRIBUTING already says "no test may depend on being faster than a
    timer", and this one did anyway: it was written before the rule and never
    revisited, because an intermittent failure looks like noise. Freezing the
    clock makes the outcome a property of the limiter rather than of how busy
    the machine is.
    """
    from carigma_api.services import ratelimit
    from carigma_api.services.ratelimit import limiter as live_limiter

    live_limiter.reset()
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: 1_000.0)
    return live_limiter


def test_the_rate_limit_fires_on_a_real_endpoint(
    client: Any, monkeypatch: Any, frozen_limiter_clock: Any
) -> None:
    """Sixth request in a burst gets a 429 with a usable Retry-After."""
    from carigma_api.routes import score as score_routes
    from tests.conftest import auth, make_token
    from tests.test_score_endpoint import (  # type: ignore[attr-defined]
        FakeCredits,
        FakeProfiles,
        FakeScores,
        _NullRunStore,
    )

    monkeypatch.setattr(
        score_routes,
        "_deps",
        lambda request, user, settings: (
            FakeProfiles({"name": "Ravi"}),
            FakeScores(),
            FakeCredits(balance=1000),
            _NullRunStore(),
        ),
    )
    codes = [
        client.post(
            "/score/compute", json={"onboarding": True}, headers=auth(make_token())
        ).status_code
        for _ in range(AGENT_BURST + 1)
    ]

    # Precondition: the burst must have been ALLOWED, or a 429 at the end
    # proves nothing about the limiter — it could be any other failure.
    assert 429 not in codes[:AGENT_BURST], f"the burst was refused early: {codes}"
    assert codes[-1] == 429, f"the limiter never fired: {codes}"


def test_the_429_carries_a_retry_after_header_and_charges_nothing(
    client: Any, monkeypatch: Any, frozen_limiter_clock: Any
) -> None:
    from carigma_api.routes import score as score_routes
    from tests.conftest import auth, make_token
    from tests.test_score_endpoint import (  # type: ignore[attr-defined]
        FakeCredits,
        FakeProfiles,
        FakeScores,
        _NullRunStore,
    )

    credits = FakeCredits(balance=1000)
    monkeypatch.setattr(
        score_routes,
        "_deps",
        lambda request, user, settings: (
            FakeProfiles({"name": "Ravi"}),
            FakeScores(),
            credits,
            _NullRunStore(),
        ),
    )

    # FREEZE THE CLOCK. The bucket refills continuously against
    # time.monotonic(), so on a slow enough machine the loop below outlasts one
    # token's refill and the final request is allowed — the limiter working
    # correctly, reported as a failure. Observed once in four full runs.
    #
    # Pinning the clock removes the race rather than widening a margin: with no
    # elapsed time there is no refill, so exhausting the burst MUST 429. A test
    # that depends on being faster than a timer is a flake wearing a guard's
    # clothes.
    import carigma_api.services.ratelimit as ratelimit_module

    frozen = ratelimit_module.time.monotonic()
    monkeypatch.setattr(ratelimit_module.time, "monotonic", lambda: frozen)

    res = None
    for _ in range(AGENT_BURST + 1):
        res = client.post("/score/compute", json={"onboarding": True}, headers=auth(make_token()))

    assert res is not None and res.status_code == 429
    # And it tells the caller when to come back. "Retry in 0s" tells them nothing.
    assert int(res.headers["Retry-After"]) >= 1
    assert int(res.headers["Retry-After"]) >= 1
    assert res.json()["detail"]["error"] == "rate_limited"
    # A throttled request is not a charged one.
    assert "Traceback" not in str(res.json())


def test_the_limiter_test_no_longer_depends_on_being_fast(
    client: Any, monkeypatch: Any, frozen_limiter_clock: Any
) -> None:
    """Advance the clock a full hour BETWEEN requests and the burst still hits
    the ceiling — because the frozen clock is the one the limiter reads.

    Verifies the fixture rather than trusting it. Before this, the test passed
    or failed depending on how busy the machine was, which is the definition of
    a result that measures the wrong thing.
    """
    from carigma_api.routes import score as score_routes
    from carigma_api.services import ratelimit
    from carigma_api.services.ratelimit import AGENT_BURST
    from tests.conftest import auth, make_token
    from tests.test_score_endpoint import (  # type: ignore[attr-defined]
        FakeCredits,
        FakeProfiles,
        FakeScores,
        _NullRunStore,
    )

    monkeypatch.setattr(
        score_routes,
        "_deps",
        lambda request, user, settings: (
            FakeProfiles({"name": "Ravi"}),
            FakeScores(),
            FakeCredits(balance=1000),
            _NullRunStore(),
        ),
    )

    # Real time passes; the limiter's clock does not.
    codes = []
    for _ in range(AGENT_BURST + 1):
        time.sleep(0.01)
        codes.append(
            client.post(
                "/score/compute", json={"onboarding": True}, headers=auth(make_token())
            ).status_code
        )

    assert codes[-1] == 429, f"the limiter refilled despite a frozen clock: {codes}"
    assert ratelimit.time.monotonic() == 1_000.0, "the clock was not actually frozen"
