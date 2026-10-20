"""The agent-run protocol.

Replaces V1's `runtime.py` thread registry, which existed only to survive
Streamlit reruns and could neither outlive a restart nor be polled by a second
client.

The important property of this module is that **the credit rule is structural**:
`execute` is the single path from "work" to "charged", and it charges only after
the work returned a non-empty result. A failure, a cancellation, or an empty
result all leave `credits_charged = 0` — not by the caller remembering, but
because there is no other code path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from carigma_api.services import credits as credits_service
from carigma_api.services.ai import UpstreamError
from carigma_api.services.constants import AGENT_LABELS
from carigma_api.services.credits import CreditReceipt, CreditStore

logger = logging.getLogger(__name__)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    EMPTY = "empty"  # sources had nothing; NOT a failure, charges nothing
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self is not RunStatus.QUEUED and self is not RunStatus.RUNNING


@dataclass
class RunStep:
    step: str
    label: str
    status: str = "pending"  # pending | active | done | failed
    count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"step": self.step, "label": self.label, "status": self.status}
        if self.count is not None:
            out["count"] = self.count
        return out


@dataclass
class AgentRun:
    id: str
    user_id: str
    agent: str
    status: RunStatus = RunStatus.QUEUED
    steps: list[RunStep] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    credits: CreditReceipt | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    #: What the run is FOR — `{"day": "MON", "week_start": "..."}` for a draft,
    #: `{"job_id": "..."}` for a CV. A page that lost its run finds it again
    #: by agent AND subject: Monday's editor must not pick up Thursday's draft.
    subject: dict[str, str] = field(default_factory=dict)
    _events: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue, repr=False)
    _cancelled: bool = False

    def __post_init__(self) -> None:
        """Refuse an id the `agent_runs` table cannot hold.

        `agent_runs.id` and `.user_id` are both `uuid`. For months `new_run`
        produced `run_<hex>`, so **every insert was rejected with a 400** — and
        because `SupabaseRunStore` documents its writes as best-effort, the
        rejection was logged and the request returned 200. Nothing surfaced.

        The check belongs HERE rather than in each store because the stores are
        where the fakes are, and a fake is exactly the thing that cannot
        refuse:

            class _NullRunStore:
                def save(self, run: object) -> None: ...

        That is not a simplified store, it is an absent one. `InMemoryRunStore`
        is the same absence with a dict in front — a dict accepts any string as
        a key, so it can never disagree with Postgres about a type. **Guarding
        the class instead means an unstorable run cannot be constructed at
        all**, and every store, fake or real, inherits the refusal.

        `user_id` is checked too. It has never been wrong — it comes from the
        JWT `sub` — but it is the same column type reached by a different path,
        and a guard scoped to the failure we happened to find is a guard for
        the paths we remembered.
        """
        for label, value in (("id", self.id), ("user_id", self.user_id)):
            try:
                UUID(str(value))
            except (ValueError, AttributeError, TypeError) as exc:
                raise ValueError(
                    f"AgentRun.{label} must be a UUID — agent_runs.{label} is a uuid "
                    f"column and Postgres answers anything else with a 400. Got {value!r}."
                ) from exc

    def as_dict(self) -> dict[str, Any]:
        """The run as a client reads it — a poll, a replay, a start.

        `error` is the plain-language sentence, never the exception text kept
        on the row: this is read back long after the request that failed, by
        a page that shows what it is given.
        """
        return {
            "run_id": self.id,
            "agent": self.agent,
            "agent_label": AGENT_LABELS.get(self.agent, self.agent),
            "status": self.status.value,
            "steps": [s.as_dict() for s in self.steps],
            "result": self.result,
            "error": friendly_error(self.error) if self.status is RunStatus.FAILED else None,
            "credits": (self.credits or CreditReceipt(0, None, "")).as_dict(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "subject": dict(self.subject),
        }


class RunNotStored(Exception):
    """A run could not be recorded as started.

    For a run whose answer rides on the response, that costs a record. For a
    run that answers with an id to poll, the record IS the answer — a run
    nobody can read back is one the user can never see finish — so the caller
    refuses to start it.
    """


class RunStoreUnavailable(Exception):
    """A run could not be READ. Not "there is no such run".

    `SupabaseRunStore.get` once logged and returned `None` for both, which is
    the `read_balance` conflation again: a poll that met a blip would be told
    the run does not exist, and stop watching a run that is still going.
    """


class RunStore(Protocol):
    """Persistence for runs. In-memory in P2; a table in production so a run
    survives a restart and can be polled from another device."""

    def save(self, run: AgentRun) -> None: ...

    def start(self, run: AgentRun) -> AgentRun | None:
        """Record a run as it begins — an INSERT, which can be refused.

        `None`: this run is recorded, and holds its idempotency key. A run:
        another request already holds the key, and that run is the answer.
        Raises `RunNotStored` when nothing could be written.
        """
        ...

    def get(self, run_id: str) -> AgentRun | None: ...

    def find_by_idempotency_key(self, user_id: str, key: str) -> AgentRun | None: ...


class InMemoryRunStore:
    """Process-local store.

    Adequate for a single API instance and for tests. Documented limitation: it
    does not survive a restart and does not span instances — the `agent_runs`
    table exists for that, wired when the app runs multi-instance.
    """

    def __init__(self) -> None:
        self._runs: dict[str, AgentRun] = {}
        #: The key a run was asked under, until the run is recorded.
        self._asked: dict[str, tuple[str, str]] = {}
        #: The run that HOLDS each key — the first one recorded under it.
        self._idempotency: dict[tuple[str, str], str] = {}

    def save(self, run: AgentRun) -> None:
        self._claim(run)
        self._runs[run.id] = run

    def start(self, run: AgentRun) -> AgentRun | None:
        holder = self._claim(run)
        if holder != run.id:
            # The table's unique index refuses the second run under a key.
            return self._runs[holder]
        self._runs[run.id] = run
        return None

    def _claim(self, run: AgentRun) -> str:
        asked = self._asked.get(run.id)
        if asked is None:
            return run.id
        holder = self._idempotency.get(asked)
        if holder is not None and holder in self._runs:
            return holder
        self._idempotency[asked] = run.id
        return run.id

    def get(self, run_id: str) -> AgentRun | None:
        return self._runs.get(run_id)

    def remember_idempotency(self, user_id: str, key: str, run_id: str) -> None:
        self._asked[run_id] = (user_id, key)

    def find_by_idempotency_key(self, user_id: str, key: str) -> AgentRun | None:
        run_id = self._idempotency.get((user_id, key))
        return self._runs.get(run_id) if run_id else None


# The work an agent does: () -> result. Kept deliberately narrow so the run
# machinery knows nothing about any particular agent.
AgentWork = Callable[["RunReporter"], Any]


class RunReporter:
    """Handed to agent work so it can report progress without knowing about SSE.

    Each change is SAVED when there is a store. A client polling
    `GET /agents/runs/{id}` reads the steps from the table, and steps that
    lived only in this process's memory would show a run making no progress
    until the moment it finished — the empty progress bar the run protocol was
    built to replace.
    """

    def __init__(self, run: AgentRun, run_store: RunStore | None = None) -> None:
        self._run = run
        self._store = run_store

    def step(self, step: str, label: str, *, count: int | None = None) -> None:
        entry = RunStep(step=step, label=label, status="active", count=count)
        self._run.steps.append(entry)
        self._emit("step", entry.as_dict())
        self._save()

    def complete_step(self, *, count: int | None = None, failed: bool = False) -> None:
        if not self._run.steps:
            return
        last = self._run.steps[-1]
        last.status = "failed" if failed else "done"
        if count is not None:
            last.count = count
        self._emit("step", last.as_dict())
        self._save()

    @property
    def cancelled(self) -> bool:
        return self._run._cancelled

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        self._run._events.put_nowait({"event": event, "data": data})

    def _save(self) -> None:
        if self._store is not None:
            self._store.save(self._run)


class RunCancelled(Exception):
    """Raised inside agent work when the user cancels."""


async def settle_detached(run: AgentRun, work: AgentWork, **kwargs: Any) -> None:
    """`execute`, after the answer has gone — a background task's body.

    `execute` does not raise, but this is the last frame anything could reach,
    so nothing escapes it unlogged.
    """
    try:
        await execute(run, work, **kwargs)
    except Exception:  # noqa: BLE001 — there is no caller left to tell
        logger.exception("background run %s did not settle", run.id)


def begin(run: AgentRun, run_store: RunStore) -> AgentRun | None:
    """Record the run as started, before any work — the strict start.

    `execute` records the start too, best-effort. This is for a route that
    answers with the run's id: the row has to exist before the id is handed
    out, or the first poll is told there is no such run.

    Returns the run that already holds this request's Idempotency-Key when
    another request got there first, so the caller answers with THAT run
    instead of starting a second one and charging twice. The table's unique
    index decides, not a lookup: two requests a millisecond apart can both
    look and both find nothing, and only one of them can insert. Raises
    `RunNotStored` when nothing could be written.
    """
    run.status = RunStatus.RUNNING
    return run_store.start(run)


async def execute(
    run: AgentRun,
    work: AgentWork,
    *,
    credit_store: CreditStore,
    action: str,
    run_store: RunStore | None = None,
) -> AgentRun:
    """Run the work and settle the run — the ONLY path that can charge credits.

    Charging happens after a non-empty success and nowhere else. Read the
    branches below as the credit rule in executable form.

    **It never raises.** A run started in the background has no caller
    waiting to catch anything: an exception escaping here would leave its row
    at `running` for ever, polled by a page that can never be told how it
    ended.
    """
    reporter = RunReporter(run, run_store)
    if run.status is not RunStatus.RUNNING:
        # Recorded at the START, not only at the finish. When `_finish` was
        # the only save, an Idempotency-Key retry sent while the run was in
        # flight found nothing — and started a second run, charged twice.
        run.status = RunStatus.RUNNING
        if run_store is not None:
            run_store.save(run)

    try:
        # Agent work is synchronous (the Anthropic SDK is), so keep the event
        # loop free for the SSE stream and other requests.
        result = await asyncio.to_thread(work, reporter)
    except RunCancelled:
        run.status = RunStatus.CANCELLED
        _close_open_steps(run)
        run.credits = _no_charge(credit_store, run.user_id)
        _finish(run, run_store)
        reporter._emit("done", {"status": run.status.value, "credits": run.credits.as_dict()})
        return run
    except UpstreamError as exc:
        # NO CHARGE. The user got nothing; billing them for our upstream's bad
        # day is exactly what the rule forbids.
        logger.warning("Run %s failed upstream: %s", run.id, exc)
        run.status = RunStatus.FAILED
        run.error = str(exc)
        _close_open_steps(run)
        run.credits = _no_charge(credit_store, run.user_id)
        _finish(run, run_store)
        reporter._emit("error", {"status": run.status.value, "detail": friendly_error(run.error)})
        return run
    except Exception as exc:  # noqa: BLE001 — a run must never take the API down
        logger.exception("Run %s failed unexpectedly", run.id)
        run.status = RunStatus.FAILED
        run.error = str(exc)
        _close_open_steps(run)
        run.credits = _no_charge(credit_store, run.user_id)
        _finish(run, run_store)
        reporter._emit("error", {"status": run.status.value, "detail": friendly_error(run.error)})
        return run

    if credits_service.is_empty_result(result):
        # Honest empty state. Nothing was fabricated to fill the gap, and
        # nothing is charged for the gap.
        run.status = RunStatus.EMPTY
        run.result = result
        run.credits = _no_charge(credit_store, run.user_id)
        _finish(run, run_store)
        reporter._emit("done", {"status": run.status.value, "credits": run.credits.as_dict()})
        return run

    run.status = RunStatus.SUCCEEDED
    run.result = result
    try:
        run.credits = credits_service.charge_for_result(credit_store, run.user_id, action, result)
    except credits_service.CreditsUnavailable:
        # The balance could not be read, so `apply_delta` was never reached
        # and nothing was charged. Delivered unbilled — the answer
        # `charge_for_result` already gives a ledger write that fails: we
        # under-charge rather than lose work the user already has.
        logger.exception("Run %s: balance unreadable at charge time; delivered unbilled", run.id)
        run.credits = CreditReceipt(0, None, credits_service.label_for(action))
    _finish(run, run_store)
    reporter._emit(
        "done",
        {
            "status": run.status.value,
            "result_url": f"/agents/runs/{run.id}",
            "credits": run.credits.as_dict(),
        },
    )
    return run


def _close_open_steps(run: AgentRun) -> None:
    """A step still `active` when the run stopped did not finish: it failed.

    Left `active`, a finished run reads back with a step still spinning —
    the page would show work going on that ended minutes ago.
    """
    for step in run.steps:
        if step.status == "active":
            step.status = "failed"


def _no_charge(credit_store: CreditStore, user_id: str) -> CreditReceipt:
    """The receipt for a run that charged nothing, with the balance if known.

    The balance is decoration here, and `read_balance` raises when it cannot
    find out. Read bare, a failed run whose balance read ALSO failed raised
    out of `execute` — in the background, a row left at `running` for ever.
    Unknown is `None`, which is what a receipt already says for a balance it
    does not have.
    """
    try:
        return CreditReceipt(0, credit_store.read_balance(user_id), "")
    except credits_service.CreditsUnavailable:
        return CreditReceipt(0, None, "")


def _finish(run: AgentRun, run_store: RunStore | None) -> None:
    run.finished_at = datetime.now(UTC)
    if run_store is not None:
        run_store.save(run)


def friendly_error(error: str | None) -> str:
    """Map a failure to plain language.

    Ported from V1 `errors.friendly_message`. Users never see a raw exception,
    and error copy carries no emoji. It lives with the run because a run is
    read back long after the request that started it — by a poll, or by a
    replay — and every one of those answers needs this sentence, never
    `str(exc)`.
    """
    low = (error or "").lower()

    def has(*words: str) -> bool:
        return any(w in low for w in words)

    if has("overloaded", "rate limit", "rate_limit", "429", "too many requests"):
        return "Things are busy right now. Please wait a few seconds and try again."
    if has("timed out", "timeout", "connection", "network", "502", "503", "504"):
        return "We couldn't reach the server. Check your connection and try again in a moment."
    if has("api key", "authentication", "401"):
        return "That feature isn't fully switched on yet — our team has been notified."
    return "We couldn't complete that just now. Please try again in a moment."


def new_run(user_id: str, agent: str) -> AgentRun:
    """A run id the `agent_runs` table can actually store.

    This was `f"run_{uuid4().hex}"`, and `agent_runs.id` has been
    `uuid primary key` since V2_001 — so **every insert was rejected with a
    400, on every run, from the first day the table existed.**

    Three components then disagreed: the score computed and persisted, the API
    returned 200, and the run record did not exist. The 200 was correct —
    `SupabaseRunStore` documents its writes as best-effort — but "best-effort"
    covered a write that had never once succeeded.

    The column is the right side. The schema declared uuid first, `user_id`
    beside it is uuid, and **nothing anywhere parsed the prefix**: it was
    decoration that cost a column type. No migration.
    """
    return AgentRun(id=str(uuid4()), user_id=user_id, agent=agent)


def cancel(run: AgentRun) -> None:
    run._cancelled = True


async def stream(run: AgentRun, *, poll_interval: float = 0.05) -> AsyncIterator[dict[str, Any]]:
    """SSE event stream for a run.

    Clients that cannot stream (or users with reduced-motion preferences) poll
    `GET /agents/runs/{id}` instead and see the same states — the stream is an
    enhancement, never the only way to learn the outcome.
    """
    while True:
        try:
            event = await asyncio.wait_for(run._events.get(), timeout=poll_interval)
            yield event
            if event["event"] in ("done", "error"):
                return
        except TimeoutError:
            if run.status.is_terminal and run._events.empty():
                # Terminal before anyone subscribed — emit the outcome so a late
                # subscriber isn't left hanging.
                yield {
                    "event": "done" if run.status is not RunStatus.FAILED else "error",
                    "data": {
                        "status": run.status.value,
                        "credits": (run.credits or CreditReceipt(0, None, "")).as_dict(),
                    },
                }
                return
