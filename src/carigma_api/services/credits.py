"""Credits — the in-app currency, and the rule that governs it.

**THE RULE (Bible §13.6, carried into V2 unchanged):**

    Credits are NEVER charged on failure or on an empty result.

This module is the only place that may move a balance, and `charge_for_result`
is deliberately the only charging entry point. Routes cannot bypass it, because
routes do not touch the balance at all — they hand a result to this module and
it decides. Making the rule a property of the code's shape rather than of every
caller's diligence is the point: a future endpoint author cannot forget it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from carigma_api.services.constants import CREDIT_COSTS, CREDIT_LABELS, FREE_ACTIONS

logger = logging.getLogger(__name__)


class InsufficientCredits(Exception):
    def __init__(self, required: int, balance: int) -> None:
        self.required = required
        self.balance = balance
        super().__init__(f"Requires {required} credits, balance is {balance}.")


@dataclass(frozen=True)
class CreditReceipt:
    """What the client is told about a charge. `charged == 0` on an empty or
    failed run is expected and correct, not a bug."""

    charged: int
    balance_after: int | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "charged": self.charged,
            "balance_after": self.balance_after,
            "reason": self.reason,
        }


class CreditStore(Protocol):
    """The persistence this module needs. A Protocol so the rule can be tested
    exhaustively without a database — the rule is what matters, not the driver."""

    def read_balance(self, user_id: str) -> int | None: ...

    def apply_delta(self, user_id: str, delta: int, reason: str, balance_after: int) -> None: ...


def cost_of(action: str) -> int:
    """Credit cost of an action. Free actions cost 0 by definition."""
    if action in FREE_ACTIONS:
        return 0
    return CREDIT_COSTS.get(action, 0)


def label_for(action: str) -> str:
    return CREDIT_LABELS.get(action, action.replace("_", " ").capitalize())


def check_affordable(store: CreditStore, user_id: str, action: str) -> int:
    """Verify the user can afford `action` BEFORE the work runs.

    Returns the cost. Raises InsufficientCredits otherwise.

    Fails OPEN when credits aren't configured (balance is None): a missing
    credits table must not lock every user out of the product.
    """
    cost = cost_of(action)
    if cost <= 0:
        return 0
    balance = store.read_balance(user_id)
    if balance is None:
        return cost
    if balance < cost:
        raise InsufficientCredits(cost, balance)
    return cost


def is_empty_result(result: Any) -> bool:
    """Is this result 'empty' for charging purposes?

    An empty result means the sources genuinely had nothing to give — an honest
    outcome, not a failure, and not something to bill for. Note that `0` and
    `False` are NOT empty: a profile score of 0 is a real, meaningful answer.
    """
    if result is None:
        return True
    if isinstance(result, (str, bytes, list, tuple, dict, set)):
        return len(result) == 0
    return False


def charge_for_result(
    store: CreditStore,
    user_id: str,
    action: str,
    result: Any,
) -> CreditReceipt:
    """Charge for a COMPLETED, NON-EMPTY result — the only way to spend credits.

    Call this *after* the work succeeded, never before. If the work raised, do
    not call it at all: no call, no charge.
    """
    cost = cost_of(action)
    reason = label_for(action)

    if cost <= 0:
        return CreditReceipt(0, store.read_balance(user_id), reason)

    if is_empty_result(result):
        logger.info("No charge for %s/%s: empty result", user_id, action)
        return CreditReceipt(0, store.read_balance(user_id), reason)

    balance = store.read_balance(user_id)
    if balance is None:
        # Credits not configured — fail open, but never invent a balance.
        return CreditReceipt(0, None, reason)

    if balance < cost:
        # The pre-check passed but the balance moved underneath us (a
        # concurrent spend). Deliver the work — it is already done and the user
        # would otherwise pay in wasted time for our race — but charge nothing.
        logger.warning(
            "Balance fell below cost between check and charge for %s/%s; delivering unbilled",
            user_id,
            action,
        )
        return CreditReceipt(0, balance, reason)

    new_balance = balance - cost
    try:
        store.apply_delta(user_id, -cost, reason, new_balance)
    except Exception:
        # A ledger write failure must not fail a completed run. We under-charge
        # rather than hand the user an error for work they already received.
        logger.exception("Credit write failed for %s/%s; delivering unbilled", user_id, action)
        return CreditReceipt(0, balance, reason)

    return CreditReceipt(cost, new_balance, reason)


def grant(store: CreditStore, user_id: str, amount: int, reason: str) -> int | None:
    """Add credits (welcome grant, top-up, subscription cycle)."""
    balance = store.read_balance(user_id)
    if balance is None:
        return None
    new_balance = balance + int(amount)
    store.apply_delta(user_id, int(amount), reason, new_balance)
    return new_balance
