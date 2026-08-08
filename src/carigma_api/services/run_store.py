"""Durable run storage and idempotency replay — the `agent_runs` table.

Replaces `InMemoryRunStore` for anything that must outlive a process. Three
things stop working the moment there is more than one API instance, or one
restart, and all three are user-visible:

1. A run started before a deploy becomes unpollable — the client waits forever
   on a run the new process has never heard of.
2. A user cannot poll from a second device.
3. **Idempotency stops working**, which is the one that costs money. A
   double-tap on "Generate" that lands on two instances charges twice, because
   neither instance can see the other's key.

`agent_runs` has a unique index on `(user_id, idempotency_key)` where the key
is not null, so the database — not application logic — is what makes a replay
impossible to get wrong under concurrency.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from carigma_api.services.credits import CreditReceipt
from carigma_api.services.runs import AgentRun, RunStatus, RunStep

logger = logging.getLogger(__name__)

_TABLE = "agent_runs"


class IdempotencyConflict(RuntimeError):
    """The key exists and belongs to a DIFFERENT request than this one.

    A key is a promise that two requests are the same request. Reusing one for
    different work is a client bug, and silently returning the first result
    would hand back an answer to a question nobody asked.
    """


def _to_row(run: AgentRun, idempotency_key: str | None) -> dict[str, Any]:
    return {
        "id": run.id,
        "user_id": run.user_id,
        "agent": run.agent,
        "status": run.status.value,
        "steps": [s.as_dict() for s in run.steps],
        "result": run.result,
        "error": run.error,
        "credits_charged": run.credits.charged if run.credits else 0,
        "idempotency_key": idempotency_key,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def _from_row(row: dict[str, Any]) -> AgentRun:
    run = AgentRun(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        agent=row["agent"],
        status=RunStatus(row["status"]),
        result=row.get("result"),
        error=row.get("error"),
        started_at=_parse(row.get("started_at")) or datetime.now(UTC),
        finished_at=_parse(row.get("finished_at")),
    )
    run.steps = [
        RunStep(
            step=s.get("step", ""),
            label=s.get("label", ""),
            status=s.get("status", "pending"),
            count=s.get("count"),
        )
        for s in (row.get("steps") or [])
    ]
    # Balance is not stored — it is a property of the ledger, not of this run,
    # and a stale copy would be worse than None.
    run.credits = CreditReceipt(int(row.get("credits_charged") or 0), None, "")
    return run


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class SupabaseRunStore:
    """`agent_runs`, per-request client so RLS applies.

    Writes are best-effort by design: a persistence failure must never abort a
    run the user is watching, and must never cause a double charge. The run
    still completes in memory; what is lost is cross-instance visibility, which
    is logged loudly.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._keys: dict[str, str] = {}

    def save(self, run: AgentRun) -> None:
        key = self._keys.get(run.id)
        try:
            self._client.table(_TABLE).upsert(_to_row(run, key)).execute()
        except Exception:
            logger.exception("agent_runs write failed for run %s", run.id)

    def get(self, run_id: str) -> AgentRun | None:
        try:
            res = self._client.table(_TABLE).select("*").eq("id", run_id).limit(1).execute()
        except Exception:
            logger.exception("agent_runs read failed for run %s", run_id)
            return None
        rows = res.data or []
        return _from_row(rows[0]) if rows else None

    def remember_idempotency(self, user_id: str, key: str, run_id: str) -> None:
        self._keys[run_id] = key

    def find_by_idempotency_key(self, user_id: str, key: str) -> AgentRun | None:
        try:
            res = (
                self._client.table(_TABLE)
                .select("*")
                .eq("user_id", user_id)
                .eq("idempotency_key", key)
                .limit(1)
                .execute()
            )
        except Exception:
            logger.exception("idempotency lookup failed")
            # Returning None here means "start a fresh run", which risks a
            # double charge — so it is logged as an exception rather than
            # swallowed quietly. Better than failing the user's request
            # outright, but it is the one lookup we would rather not lose.
            return None
        rows = res.data or []
        return _from_row(rows[0]) if rows else None


def replay_or_none(store: Any, user_id: str, key: str | None, *, agent: str) -> AgentRun | None:
    """The replay decision, in one place.

    Returns the original run when a key has been seen before — so a double-tap
    on "Generate" gets the first answer back and is charged once.

    A run still in flight is returned as-is rather than waited on: the client
    polls the run protocol anyway, and blocking here would hold a connection
    open for the length of an agent run.
    """
    if not key:
        return None

    previous = store.find_by_idempotency_key(user_id, key)
    if previous is None:
        return None

    if previous.agent != agent:
        # Same key, different work. The client is confused, and answering with
        # the wrong agent's result would be worse than an error.
        raise IdempotencyConflict(f"Idempotency-Key was already used for a {previous.agent} run.")

    logger.info("idempotency replay for run %s (%s)", previous.id, previous.status.value)
    run: AgentRun = previous
    return run


def replay_payload(run: AgentRun) -> dict[str, Any]:
    """The replayed response.

    Carries `replayed: true` and **`credits.charged = 0`**. The original charge
    already happened and is reported on the original response; repeating the
    number here would make a client that sums receipts double-count a single
    debit.
    """
    payload = run.as_dict()
    payload["replayed"] = True
    payload["credits"] = {**payload.get("credits", {}), "charged": 0}
    payload["note"] = "Already ran — this is the original result, and you were charged once."
    return payload
