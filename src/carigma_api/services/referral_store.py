"""Referral rows, and the one function that may move credits.

`services/referrals.py` holds the rules and knows nothing about storage. This
holds the storage and re-states none of the rules.

## Where the cap actually lives

Not here. `next_slot` proposes and `unique (referrer_id, slot)` decides — so
`claim` inserts, and on a 23505 it adds the losing slot to the taken set and
asks again. Two concurrent activations cannot both take slot 5, because the
second insert fails rather than the second count being stale.

The retry is bounded by `MAX_REFERRALS`: after five collisions every slot is
demonstrably taken by somebody, which is `CapReached` and not an error.

## The single door

`activate` is the only function in the codebase that writes referral credits,
and it takes an `Activation` — a value only `activation_from_profile_upload`
can construct. There is no route that calls it and no route that ever should:
the trigger is a profile upload, not a request someone can make.

**It currently has no caller**, because V2 has no profile-upload path yet.
That is recorded in `test_referral_store.py` rather than left to be noticed —
a grant with no trigger is a promise the surface makes and the product cannot
keep, and the day onboarding lands, the wiring must not be the thing everyone
assumed someone else did.
"""

from __future__ import annotations

import logging
from typing import Any

from carigma_api.services.referrals import (
    MAX_REFERRALS,
    Activation,
    AlreadyReferred,
    CapReached,
    Grant,
    LedgerKind,
    SelfReferral,
    State,
    grant_for_activation,
    new_code,
)

logger = logging.getLogger(__name__)

CODES = "referral_codes"
REFERRALS = "referrals"

#: PostgREST surfaces a unique violation as this SQLSTATE.
UNIQUE_VIOLATION = "23505"


def _is_unique_violation(exc: Exception) -> bool:
    """True for a duplicate-key error, whatever the client wrapped it in.

    Checked on the CODE rather than the message: the message is prose that
    changes between PostgREST versions, and a slot collision misread as a
    generic failure would abandon a referral the retry would have placed.
    """
    code = getattr(exc, "code", None)
    if code == UNIQUE_VIOLATION:
        return True
    details = getattr(exc, "details", "") or ""
    return UNIQUE_VIOLATION in f"{details}{exc}"


#: The SQLSTATEs `referral_claim()` raises, and what each means here. The
#: function speaks in codes rather than messages so this mapping is exact —
#: matching on prose would break the first time the wording changed.
_CLAIM_ERRORS: dict[str, type[Exception]] = {
    "P0002": LookupError,
    "P0001": SelfReferral,
    "P0003": AlreadyReferred,
    "P0004": CapReached,
}

_CLAIM_MESSAGES: dict[str, str] = {
    "P0002": "that code doesn't exist",
    "P0001": "You can't refer yourself.",
    "P0003": "You've already been referred.",
    "P0004": "Every slot on that code is taken.",
}


def _translate(exc: Exception) -> Exception:
    """Turn a raised SQLSTATE into the exception the routes already handle."""
    code = str(getattr(exc, "code", "") or "")
    if not code:
        details = f"{getattr(exc, 'details', '') or ''}{exc}"
        code = next((c for c in _CLAIM_ERRORS if c in details), "")
    kind = _CLAIM_ERRORS.get(code)
    if kind is None:
        # Not one of ours. Re-raised as-is so a real outage is not reported to
        # the user as "that link isn't valid".
        return exc
    return kind(_CLAIM_MESSAGES[code])


class SupabaseReferralStore:
    """Reads and writes through the CALLER's client, so RLS applies."""

    def __init__(self, client: Any) -> None:
        self._c = client

    # ── Codes ──────────────────────────────────────────────────────────────

    def code_for(self, user_id: str) -> str:
        """This user's code, minted on first read.

        Lazy rather than at signup: a code nobody has ever looked at is a row
        for every user who will never share. On the race — two tabs opening the
        page at once — the unique index on `user_id` decides and the loser
        reads back the winner's code rather than overwriting it.
        """
        res = self._c.table(CODES).select("code").eq("user_id", user_id).limit(1).execute()
        rows = res.data if res and isinstance(res.data, list) else []
        if rows and isinstance(rows[0], dict) and rows[0].get("code"):
            return str(rows[0]["code"])

        code = new_code()
        try:
            self._c.table(CODES).insert({"user_id": user_id, "code": code}).execute()
        except Exception as exc:
            if not _is_unique_violation(exc):
                raise
            # Someone else minted it between our read and our write. Theirs is
            # the code; ours is discarded. Returning `code` here would hand out
            # a string that is in nobody's table.
            again = self._c.table(CODES).select("code").eq("user_id", user_id).limit(1).execute()
            rows = again.data if again and isinstance(again.data, list) else []
            if rows and isinstance(rows[0], dict) and rows[0].get("code"):
                return str(rows[0]["code"])
            raise
        return code

    # ── Attribution ────────────────────────────────────────────────────────

    def summary_counts(self) -> dict[str, int]:
        """Counts for the CALLER, via `referral_summary()`.

        Not a table read. `referrals` has RLS on and no policy — deliberately,
        because a row keyed on `referred_id` leaks who signed up — so a select
        through the caller's client returns an empty list rather than an error.
        Every referrer would be told "0 activated, 5 slots left" forever, which
        is worse than a failure because it looks like an answer.

        The function derives the user from `auth.uid()`, so there is no
        argument this can be called with to get somebody else's numbers.
        """
        res = self._c.rpc("referral_summary", {}).execute()
        rows = res.data if res and isinstance(res.data, list) else []
        if not rows or not isinstance(rows[0], dict):
            # `count(*)` always returns a row, so no row means the call did not
            # do what we think it did. Raising sends the route to its 503,
            # which is the truthful answer; the first draft here read
            # `int(row.get("activated") or 0)` and would have rendered "0
            # earned" — an answer, from an error, to someone who had earned it.
            raise LookupError("referral_summary returned nothing")
        row = rows[0]
        # No `or 0`: the three columns are `count(*)` aggregates and cannot be
        # null, so a fallback here would be unreachable code stating a policy —
        # that a summary we cannot read means nothing has been earned.
        return {
            "activated": int(row["activated"]),
            "pending": int(row["pending"]),
            "total": int(row["total"]),
        }

    def claim(self, *, code: str) -> dict[str, Any]:
        """Record a PENDING attribution for the caller. Moves no credits.

        The whole of what arriving through someone's link does — which is why
        there is no amount anywhere in this method.

        The insert, the slot retry and the cap all live in `referral_claim()`,
        because the cap is the unique index and the retry has to be on the same
        side of the wire as the insert that lost.
        """
        try:
            res = self._c.rpc("referral_claim", {"p_code": code}).execute()
        except Exception as exc:
            raise _translate(exc) from exc
        rows = res.data if res and isinstance(res.data, list) else []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        return {"slot": int(row["slot"]), "state": str(row.get("state") or State.PENDING)}

    def code_exists(self, code: str) -> bool:
        """For the landing page. Returns a BOOLEAN and nothing else.

        The visitor has no account — that is what the link is for — so this
        goes through a function granted to `anon`. It reveals whether a string
        is a code, never whose.
        """
        res = self._c.rpc("referral_code_valid", {"p_code": code}).execute()
        return bool(res.data) if res else False

    # ── The single door ────────────────────────────────────────────────────

    def referral_for(self, referred_id: str) -> dict[str, Any] | None:
        """A direct table read, so only the service-role caller below can use it.

        Not an RPC on purpose — see `activate`. A function returning this row
        to `authenticated` would hand a user their referrer's id, which is the
        association V2_010 closed the table to protect.
        """
        res = self._c.table(REFERRALS).select("*").eq("referred_id", referred_id).limit(1).execute()
        rows = res.data if res and isinstance(res.data, list) else []
        return dict(rows[0]) if rows and isinstance(rows[0], dict) else None

    def activate(self, activation: Activation) -> Grant | None:
        """Turn a pending referral into credits. THE only grant path.

        **Requires a SERVICE-ROLE client, and deliberately has no RPC.**

        The other three operations became `SECURITY DEFINER` functions granted
        to `authenticated`, because RLS on `referrals` otherwise makes the
        surface impossible. This one must not join them: a function an
        authenticated user can execute is one they can execute from a browser
        console, and `referral_activate()` granted to `authenticated` would let
        anyone mint 20 credits without uploading anything. The `Activation`
        type guards the Python call site; a SQL grant would route around it.

        So activation runs server-side after an upload completes — a system
        action, the same category as the cron paths where the service key is
        allowed — and there is no code path from a request to here.

        Takes an `Activation`, so there is no signature this can be called with
        from a registration handler — "50 credits on profile upload, never on
        signup" is a property of the type rather than a branch to remember.

        Returns None when there is nothing to do: no referral, or one already
        activated. Neither is an error, and a profile upload must never fail
        because of the referral programme attached to it.
        """
        row = self.referral_for(activation.referred_id)
        if row is None or str(row.get("state")) == str(State.ACTIVATED):
            return None

        grant = grant_for_activation(
            activation,
            referrer_id=str(row["referrer_id"]),
            slot=int(row["slot"]),
        )

        # ONE transaction: the conditional state flip AND both credit grants,
        # in `referral_activate_tx`.
        #
        # This used to be three round trips — flip, grant the referrer, grant
        # the referred user. A failure after the flip left the row saying
        # `activated` with nobody paid, and **a retry could not help**, because
        # the guard at the top of this method returns early once it sees
        # `activated`. The credits were owed with no path to recovery, and a
        # failure between the two grants paid one side and never the other.
        #
        # Grant-first-flip-after would have been worse: a crash between them
        # means a retry re-grants, which is the double payment the payments
        # path exists to prevent. So the window is removed rather than
        # recovered from.
        #
        # The function is `security definer` and granted to **service_role
        # only** — see V2_015. The danger with `referral_activate` was ever
        # granting it to `authenticated`, not the definer rights themselves.
        res = self._c.rpc(
            "referral_activate_tx",
            {
                "p_referred_id": activation.referred_id,
                "p_at": activation.at.isoformat(),
                "p_referrer_credits": grant.referrer_credits,
                "p_referred_credits": grant.referred_credits,
                "p_referrer_kind": str(LedgerKind.REFERRER),
                "p_referred_kind": str(LedgerKind.REFERRED),
            },
        ).execute()

        rows = res.data if res else None
        if not rows:
            # No pending row when the transaction ran — somebody else won the
            # race, or it was already activated. Not an error.
            logger.info("referral for %s was activated concurrently", activation.referred_id)
            return None
        return grant


def summary(*, code: str, counts: dict[str, int], share: dict[str, Any]) -> dict[str, Any]:
    """What the share surface renders.

    `counts` comes from `referral_summary()`, which returns numbers and no ids.
    A referrer does not need to know who signed up, and the underlying rows are
    closed to them precisely so that association cannot leak.

    Counting here would be fine — this is a report, not the cap — but there is
    nothing to count: the rows never reach this process.
    """
    activated = counts["activated"]
    pending = counts["pending"]
    return {
        **share,
        "activated": activated,
        "pending": pending,
        "slots_left": max(0, MAX_REFERRALS - counts["total"]),
        "earned": activated * share["referrer_credits"],
        # A pending referral is somebody who signed up and has not uploaded
        # anything. Said plainly, because "1 pending" with no explanation reads
        # like a delay on our side.
        "pending_note": (
            f"{pending} signed up and haven't uploaded a profile yet." if pending else None
        ),
    }


__all__ = [
    "CODES",
    "REFERRALS",
    "SupabaseReferralStore",
    "SelfReferral",
    "summary",
]
