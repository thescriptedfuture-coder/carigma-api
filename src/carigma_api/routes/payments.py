"""Payment endpoints — complete, test-mode, dormant. Contract §17.

Every route checks `settings.payments_live` first. With the flag off there is
**no reachable payment path**: `/pricing` still renders prices and explains
why buying is unavailable, and every mutating route 404s as if it did not
exist. That is deliberate — a 403 would advertise a hidden feature.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import enforce_public_rate_limit
from carigma_api.services import payments as pay
from carigma_api.services.repository import SupabaseCreditStore, user_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["payments"])


class TopupRequest(BaseModel):
    pack_key: str = Field(min_length=1)


class SubscribeRequest(BaseModel):
    plan_key: str = Field(min_length=1)


class VerifyRequest(BaseModel):
    """The return from Razorpay, or a reconciliation triggered by a page visit.

    `razorpay_signature` is present on the checkout return and absent on a
    plain reconcile — both are legitimate, and both end in the same
    verify-once path.
    """

    link_id: str | None = None
    subscription_id: str | None = None
    razorpay_payment_id: str | None = None
    razorpay_order_id: str | None = None
    razorpay_signature: str | None = None


# ── Storage ────────────────────────────────────────────────────────────────


class SupabasePaymentStore:
    """`payments` / `user_subscriptions`.

    `claim_payment` and `claim_cycles` are **conditional updates**, and their
    result is what the caller uses to decide whether to credit. This is the
    whole idempotency mechanism — see `services/payments`.
    """

    def __init__(self, client: Any) -> None:
        self._c = client

    def record_payment(self, row: dict[str, Any]) -> None:
        self._c.table("payments").insert(row).execute()

    def get_payment(self, user_id: str, link_id: str) -> dict[str, Any] | None:
        res = (
            self._c.table("payments")
            .select("*")
            .eq("user_id", user_id)
            .eq("link_id", link_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return dict(rows[0]) if rows else None

    def claim_payment(self, user_id: str, link_id: str, payment_id: str | None) -> bool:
        """`... where status = 'created'` — and the affected-row count decides.

        PostgREST returns the updated rows, so a non-empty result means THIS
        request won the claim. An empty result means someone else did, and we
        must not credit.
        """
        res = (
            self._c.table("payments")
            .update(
                {
                    "status": pay.PaymentStatus.PAID.value,
                    "payment_id": payment_id,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            .eq("user_id", user_id)
            .eq("link_id", link_id)
            .eq("status", pay.PaymentStatus.CREATED.value)
            .execute()
        )
        return bool(res.data)

    def mark_payment(self, user_id: str, link_id: str, status: str) -> None:
        self._c.table("payments").update(
            {"status": status, "updated_at": datetime.now(UTC).isoformat()}
        ).eq("user_id", user_id).eq("link_id", link_id).execute()

    def get_subscription(self, user_id: str, sub_id: str) -> dict[str, Any] | None:
        res = (
            self._c.table("user_subscriptions")
            .select("*")
            .eq("user_id", user_id)
            .eq("sub_id", sub_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return dict(rows[0]) if rows else None

    def claim_cycles(self, user_id: str, sub_id: str, *, seen: int, paid: int) -> int:
        """Guarded on the count we read, so two reconciles cannot both pay a
        cycle. Same shape as `claim_payment`: the update's result decides."""
        res = (
            self._c.table("user_subscriptions")
            .update({"cycles_credited": paid, "updated_at": datetime.now(UTC).isoformat()})
            .eq("user_id", user_id)
            .eq("sub_id", sub_id)
            .eq("cycles_credited", seen)
            .execute()
        )
        return (paid - seen) if res.data else 0


class _CreditGranter:
    """Adapts the credit store to what payments needs."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def grant(self, user_id: str, amount: int, reason: str) -> int | None:
        from carigma_api.services import credits as credits_service

        return credits_service.grant(self._store, user_id, amount, reason)


def _deps(request: Request, settings: Settings) -> tuple[SupabasePaymentStore, _CreditGranter]:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    client = user_client(settings, token)
    return SupabasePaymentStore(client), _CreditGranter(SupabaseCreditStore(client))


def _require_live(settings: Settings) -> None:
    """With payments off, the mutating routes do not exist.

    404 rather than 403: a forbidden response confirms the endpoint is there
    and merely gated, which is information about an unreleased feature.
    """
    if not settings.payments_live:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "Not found."},
        )


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/pricing")
def get_pricing(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    """Always reachable, dormant or not.

    When dormant it carries the honest-nothing block instead of a dead button,
    and names the real alternative: message us and we top you up, free, during
    beta.
    """
    # Public and uncached, so it is worth a per-IP ceiling even though it is
    # only a read.
    enforce_public_rate_limit(request)
    return pay.pricing_payload(enabled=settings.payments_live)


@router.post("/payments/topup")
def create_topup(
    body: TopupRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Create a Razorpay Payment Link for a credit pack."""
    _require_live(settings)

    chosen = pay.pack(body.pack_key)
    if chosen is None:
        # Never fall back to a default pack — that would charge someone for
        # something they did not choose.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_pack", "message": "That pack doesn't exist."},
        )

    store, _ = _deps(request, settings)
    try:
        link = _razorpay(settings).payment_link.create(
            {
                "amount": chosen.amount_inr * 100,  # paise
                "currency": "INR",
                "description": f"Carigma · {chosen.label} · {chosen.credits} credits",
                "customer": {"email": user.email},
                "notify": {"email": False, "sms": False},
                "reminder_enable": False,
                "notes": {"user_id": user.id, "pack_key": chosen.key},
                "callback_url": f"{settings.app_url}/pricing?link_id={{payment_link_id}}",
                "callback_method": "get",
            }
        )
    except Exception as exc:
        logger.exception("razorpay payment link creation failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "provider_unavailable",
                "message": "We couldn't start that payment. You haven't been charged.",
            },
        ) from exc

    store.record_payment(
        {
            "user_id": user.id,
            "provider": "razorpay",
            "link_id": link["id"],
            "pack_key": chosen.key,
            "credits": chosen.credits,
            "amount_inr": chosen.amount_inr,
            "status": pay.PaymentStatus.CREATED.value,
            "short_url": link.get("short_url"),
        }
    )
    return {"link_id": link["id"], "checkout_url": link.get("short_url")}


@router.post("/payments/verify")
def verify(
    body: VerifyRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Confirm a payment and credit it AT MOST ONCE.

    Safe to call repeatedly — on the return redirect, on a page visit, on a
    refresh. A replay credits zero and still reports success, because from the
    user's side the payment did succeed.
    """
    _require_live(settings)
    store, granter = _deps(request, settings)

    # When a signature is present it must be valid. Its absence is fine (a
    # plain reconcile has none); its presence-and-wrongness is not.
    if body.razorpay_signature:
        ok = (
            pay.verify_subscription_signature(
                subscription_id=body.subscription_id or "",
                payment_id=body.razorpay_payment_id or "",
                signature=body.razorpay_signature,
                secret=settings.razorpay_key_secret,
            )
            if body.subscription_id
            else pay.verify_signature(
                order_id=body.razorpay_order_id or "",
                payment_id=body.razorpay_payment_id or "",
                signature=body.razorpay_signature,
                secret=settings.razorpay_key_secret,
            )
        )
        if not ok:
            logger.warning("payment signature verification failed for user %s", user.id)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "bad_signature",
                    "message": "We couldn't verify that payment. Nothing was credited.",
                },
            )

    if body.subscription_id:
        try:
            sub = _razorpay(settings).subscription.fetch(body.subscription_id)
        except Exception as exc:
            raise _provider_down() from exc
        result = pay.verify_subscription_cycles(
            store,
            granter,
            user_id=user.id,
            sub_id=body.subscription_id,
            # falsy-ok: a subscription that has never billed has paid zero times
            provider_paid_count=int(sub.get("paid_count") or 0),
            provider_status=str(sub.get("status") or ""),
        )
    elif body.link_id:
        try:
            link = _razorpay(settings).payment_link.fetch(body.link_id)
        except Exception as exc:
            raise _provider_down() from exc
        result = pay.verify_topup(
            store,
            granter,
            user_id=user.id,
            link_id=body.link_id,
            provider_status=str(link.get("status") or ""),
            payment_id=(link.get("payments") or [{}])[-1].get("payment_id"),
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "nothing_to_verify", "message": "Nothing to verify."},
        )

    return result.as_dict()


def _provider_down() -> HTTPException:
    """ "We couldn't look" is not "it failed".

    The same distinction as `ProviderUnavailable` in the jobs layer: telling
    someone their payment failed when we simply could not reach Razorpay would
    send them to pay a second time.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "provider_unavailable",
            "message": (
                "We couldn't reach the payment provider to check. If you paid, "
                "your credits will appear — refresh in a minute."
            ),
        },
    )


def _razorpay(settings: Settings) -> Any:
    import razorpay

    return razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))


__all__ = ["router", "SupabasePaymentStore"]
