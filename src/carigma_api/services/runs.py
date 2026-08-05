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
from uuid import uuid4

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
    _events: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue, repr=False)
    _cancelled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.id,
            "agent": self.agent,
            "agent_label": AGENT_LABELS.get(self.agent, self.agent),
            "status": self.status.value,
            "steps": [s.as_dict() for s in self.steps],
            "result": self.result,
            "error": self.error,
            "credits": (self.credits or CreditReceipt(0, None, "")).as_dict(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class RunStore(Protocol):
    """Persistence for runs. In-memory in P2; a table in production so a run
    survives a restart and can be polled from another device."""

    def save(self, run: AgentRun) -> None: ...

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
        self._idempotency: dict[tuple[str, str], str] = {}

    def save(self, run: AgentRun) -> None:
        self._runs[run.id] = run

    def get(self, run_id: str) -> AgentRun | None:
        return self._runs.get(run_id)

    def remember_idempotency(self, user_id: str, key: str, run_id: str) -> None:
        self._idempotency[(user_id, key)] = run_id

    def find_by_idempotency_key(self, user_id: str, key: str) -> AgentRun | None:
        run_id = self._idempotency.get((user_id, key))
        return self._runs.get(run_id) if run_id else None


# The work an agent does: () -> result. Kept deliberately narrow so the run
# machinery knows nothing about any particular agent.
AgentWork = Callable[["RunReporter"], Any]


class RunReporter:
    """Handed to agent work so it can report progress without knowing about SSE."""

    def __init__(self, run: AgentRun) -> None:
        self._run = run

    def step(self, step: str, label: str, *, count: int | None = None) -> None:
        entry = RunStep(step=step, label=label, status="active", count=count)
        self._run.steps.append(entry)
        self._emit("step", entry.as_dict())

    def complete_step(self, *, count: int | None = None) -> None:
        if not self._run.steps:
            return
        last = self._run.steps[-1]
        last.status = "done"
        if count is not None:
            last.count = count
        self._emit("step", last.as_dict())

    @property
    def cancelled(self) -> bool:
        return self._run._cancelled

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        self._run._events.put_nowait({"event": event, "data": data})


class RunCancelled(Exception):
    """Raised inside agent work when the user cancels."""


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
    """
    reporter = RunReporter(run)
    run.status = RunStatus.RUNNING

    try:
        # Agent work is synchronous (the Anthropic SDK is), so keep the event
        # loop free for the SSE stream and other requests.
        result = await asyncio.to_thread(work, reporter)
    except RunCancelled:
        run.status = RunStatus.CANCELLED
        run.credits = CreditReceipt(0, credit_store.read_balance(run.user_id), "")
        _finish(run, run_store)
        reporter._emit("done", {"status": run.status.value, "credits": run.credits.as_dict()})
        return run
    except UpstreamError as exc:
        # NO CHARGE. The user got nothing; billing them for our upstream's bad
        # day is exactly what the rule forbids.
        logger.warning("Run %s failed upstream: %s", run.id, exc)
        run.status = RunStatus.FAILED
        run.error = str(exc)
        run.credits = CreditReceipt(0, credit_store.read_balance(run.user_id), "")
        _finish(run, run_store)
        reporter._emit("error", {"status": run.status.value, "detail": run.error})
        return run
    except Exception as exc:  # noqa: BLE001 — a run must never take the API down
        logger.exception("Run %s failed unexpectedly", run.id)
        run.status = RunStatus.FAILED
        run.error = str(exc)
        run.credits = CreditReceipt(0, credit_store.read_balance(run.user_id), "")
        _finish(run, run_store)
        reporter._emit("error", {"status": run.status.value, "detail": run.error})
        return run

    if credits_service.is_empty_result(result):
        # Honest empty state. Nothing was fabricated to fill the gap, and
        # nothing is charged for the gap.
        run.status = RunStatus.EMPTY
        run.result = result
        run.credits = CreditReceipt(0, credit_store.read_balance(run.user_id), "")
        _finish(run, run_store)
        reporter._emit("done", {"status": run.status.value, "credits": run.credits.as_dict()})
        return run

    run.status = RunStatus.SUCCEEDED
    run.result = result
    run.credits = credits_service.charge_for_result(credit_store, run.user_id, action, result)
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


def _finish(run: AgentRun, run_store: RunStore | None) -> None:
    run.finished_at = datetime.now(UTC)
    if run_store is not None:
        run_store.save(run)


def new_run(user_id: str, agent: str) -> AgentRun:
    return AgentRun(id=f"run_{uuid4().hex}", user_id=user_id, agent=agent)


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
