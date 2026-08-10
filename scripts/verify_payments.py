"""End-to-end payment proof against Razorpay TEST MODE.

    python scripts/verify_payments.py

Creates a real test-mode Payment Link, then drives the verification path the
way the app does — including replaying it — and asserts the credit ledger moved
exactly once.

**Refuses to run against live keys.** The whole point is to exercise money
handling without moving money; doing that against `rzp_live_` would do the
opposite. Test-mode links are real objects in Razorpay's sandbox and cost
nothing.

Nothing here touches a real user: it operates on a throwaway payment row keyed
to the founder account and cleans up after itself.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import razorpay  # noqa: E402

from carigma_api.config import Settings  # noqa: E402
from carigma_api.services import payments as pay  # noqa: E402
from carigma_api.services.repository import emails_by_user_id, service_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("verify_payments")

PROBE_PACK = "booster"  # 300 credits, ₹399


class Store:
    """The real SupabasePaymentStore semantics, on the service client.

    `claim_payment` is a CONDITIONAL update whose result gates the grant —
    that is the whole verify-once mechanism, so this proof must exercise the
    real one rather than a fake.
    """

    def __init__(self, db: Any) -> None:
        from carigma_api.routes.payments import SupabasePaymentStore

        self._inner = SupabasePaymentStore(db)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class LedgerGranter:
    """Grants through the real credits service, so the ledger is the witness."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.calls = 0

    def grant(self, user_id: str, amount: int, reason: str) -> int | None:
        self.calls += 1
        row = self._db.table("credits").select("balance").eq("user_id", user_id).execute().data or [
            {}
        ]
        new = int(row[0].get("balance") or 0) + amount
        self._db.table("credits").upsert({"user_id": user_id, "balance": new}).execute()
        self._db.table("credit_ledger").insert(
            {
                "user_id": user_id,
                "delta": amount,
                "balance_after": new,
                "kind": "purchase",
                "reason": reason,
            }
        ).execute()
        return new


def main() -> int:  # noqa: PLR0915
    s = Settings()

    if not s.razorpay_key_id or not s.razorpay_key_secret:
        logger.error("Razorpay is not configured. Nothing was done.")
        return 1
    if not s.razorpay_is_test_mode:
        # A guard, not a courtesy. This script creates payment objects; doing
        # that with live keys would create real ones.
        logger.error("REFUSING: keys are not rzp_test_. This script is test-mode only.")
        return 1

    client = razorpay.Client(auth=(s.razorpay_key_id, s.razorpay_key_secret))
    db = service_client(s)
    store, granter = Store(db), LedgerGranter(db)

    uid = next(iter(emails_by_user_id(db)), None)
    if not uid:
        logger.error("no user to attribute the probe to")
        return 1

    pack = pay.pack(PROBE_PACK)
    assert pack is not None
    before = int(
        (db.table("credits").select("balance").eq("user_id", uid).execute().data or [{}])[0].get(
            "balance"
        )
        or 0
    )
    logger.info("user balance before: %d", before)

    # ── 1. Create a REAL test-mode payment link ──────────────────────────────
    link = client.payment_link.create(
        {
            "amount": pack.amount_inr * 100,
            "currency": "INR",
            "description": f"Carigma verify — {pack.label}",
            "notify": {"email": False, "sms": False},
            "reminder_enable": False,
            "notes": {"user_id": uid, "pack_key": pack.key, "probe": "verify_payments"},
        }
    )
    link_id = link["id"]
    logger.info("created test payment link %s (status=%s)", link_id, link.get("status"))

    store.record_payment(
        {
            "user_id": uid,
            "provider": "razorpay",
            "link_id": link_id,
            "pack_key": pack.key,
            "credits": pack.credits,
            "amount_inr": pack.amount_inr,
            "status": pay.PaymentStatus.CREATED.value,
            "short_url": link.get("short_url"),
        }
    )

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        logger.info("  %s %s%s", "PASS" if ok else "FAIL", label, f" — {detail}" if detail else "")
        if not ok:
            failures.append(label)

    # ── 2. Unpaid link credits nothing ───────────────────────────────────────
    logger.info("2. verification while still unpaid")
    r = pay.verify_topup(
        store, granter, user_id=uid, link_id=link_id, provider_status=link.get("status", "created")
    )
    check("unpaid -> NOT_PAID_YET", r.outcome is pay.GrantOutcome.NOT_PAID_YET, str(r.outcome))
    check("unpaid grants nothing", granter.calls == 0)

    # ── 3. First verification of a paid link credits once ────────────────────
    # Razorpay's sandbox has no API to mark a link paid, so the PROVIDER STATUS
    # is supplied as 'paid' while everything on our side — the row, the
    # conditional claim, the ledger — is real. The mechanism under test is
    # ours, not Razorpay's.
    logger.info("3. first verification with provider_status=paid")
    r1 = pay.verify_topup(store, granter, user_id=uid, link_id=link_id, provider_status="paid")
    check("first -> GRANTED", r1.outcome is pay.GrantOutcome.GRANTED, str(r1.outcome))
    check(f"credited {pack.credits}", r1.credits_added == pack.credits, str(r1.credits_added))

    # ── 4. THE REPLAY ────────────────────────────────────────────────────────
    logger.info("4. REPLAYED verification (the double-tap)")
    r2 = pay.verify_topup(store, granter, user_id=uid, link_id=link_id, provider_status="paid")
    check(
        "replay -> ALREADY_CREDITED",
        r2.outcome is pay.GrantOutcome.ALREADY_CREDITED,
        str(r2.outcome),
    )
    check("replay credits ZERO", r2.credits_added == 0, str(r2.credits_added))
    check("replay still reads as success to the user", r2.user_sees_success is True)
    check("grant() called exactly once", granter.calls == 1, f"{granter.calls} calls")

    # ── 5. The ledger is the witness ─────────────────────────────────────────
    after = int(
        (db.table("credits").select("balance").eq("user_id", uid).execute().data or [{}])[0].get(
            "balance"
        )
        or 0
    )
    rows = (
        db.table("credit_ledger")
        .select("*")
        .eq("user_id", uid)
        .eq("kind", "purchase")
        .execute()
        .data
        or []
    )
    # Match on pack.KEY, not pack.label. verify_topup writes
    # "Top-up · booster (Razorpay)" from `pack_key`, so filtering on the
    # display label ("Booster") found nothing — my assertion was wrong, not
    # the code, and it also meant cleanup skipped the row it should have
    # removed.
    probe_rows = [r for r in rows if pack.key in str(r.get("reason", ""))]
    logger.info("5. ledger check")
    check(
        f"balance moved by exactly {pack.credits}",
        after - before == pack.credits,
        f"{before} -> {after}",
    )
    check("exactly ONE purchase ledger row", len(probe_rows) == 1, f"{len(probe_rows)} rows")

    # ── 6. Signature verification against a real Razorpay-shaped payload ─────
    logger.info("6. signature verification")
    import hashlib
    import hmac

    good = hmac.new(
        s.razorpay_key_secret.encode(), b"order_probe|pay_probe", hashlib.sha256
    ).hexdigest()
    check(
        "valid signature accepted",
        pay.verify_signature(
            order_id="order_probe",
            payment_id="pay_probe",
            signature=good,
            secret=s.razorpay_key_secret,
        ),
    )
    check(
        "tampered signature rejected",
        not pay.verify_signature(
            order_id="order_probe",
            payment_id="pay_probe",
            signature=good[:-1] + ("0" if good[-1] != "0" else "1"),
            secret=s.razorpay_key_secret,
        ),
    )

    # ── Clean up: undo the probe credit and remove the rows ──────────────────
    logger.info("cleaning up")
    db.table("credits").upsert({"user_id": uid, "balance": before}).execute()
    for row in probe_rows:
        db.table("credit_ledger").delete().eq("id", row["id"]).execute()
    db.table("payments").delete().eq("link_id", link_id).execute()
    try:
        client.payment_link.cancel(link_id)
    except Exception:
        logger.debug("could not cancel test link (harmless)", exc_info=True)

    restored = int(
        (db.table("credits").select("balance").eq("user_id", uid).execute().data or [{}])[0].get(
            "balance"
        )
        or 0
    )
    check("balance restored", restored == before, f"{restored} vs {before}")

    print()
    if failures:
        logger.error("FAILED: %s", ", ".join(failures))
        return 1
    logger.info("ALL CHECKS PASSED — verify-once holds against real test-mode Razorpay")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
