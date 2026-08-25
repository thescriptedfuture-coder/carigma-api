"""One-click unsubscribe, for someone who is not signed in.

## Why this is not a settings screen

An unsubscribe link arrives by email, usually on a device where the person was
never signed in. Putting it behind the auth guard makes "unsubscribe" mean
"sign in first" — a bad answer, and for marketing mail arguably not a lawful
one. DPDPA is on the pre-launch checklist.

So the link carries a signed token that identifies the SUBSCRIPTION, not a
session. It works on a borrowed phone, months later, with no account access.

## The key is derived, not new

    subkey = HMAC(SUPABASE_SERVICE_KEY, "carigma.unsubscribe.v1")

No new secret to store, rotate or leak. The purpose string is what makes it a
*different* key: a token minted here can never be replayed against anything
else that might one day derive from the same root, because that thing will use
a different purpose and get a different subkey.

## Single-purpose by construction

The token grants exactly one operation: set `unsubscribed_all`. It carries a
user id and nothing else — no scope field, no action parameter, nothing a
caller could widen. There is no code path from a token to a read.

That is deliberate over a general "signed action" helper. A general one would
be one `action="delete_account"` away from a very bad day.

## No expiry, stated rather than implied

An unsubscribe link SHOULD still work in a year — an old email is exactly when
someone reaches for it. So there is no timestamp in the payload at all, rather
than a timestamp nobody checks: an unchecked expiry field is dead code that
reads as a policy, and the next person trusts it.

Revocation, if ever needed, is rotating the service key — which already
invalidates everything.

## It never confirms whether an address is registered

Same non-enumeration rule as password reset. The token contains a user id we
issued, so a valid one proves nothing an attacker did not already hold. An
INVALID one gets "this link cannot be read" — a statement about the link, not
about any person.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: What makes this a distinct key from anything else derived from the same
#: root. Changing it invalidates every issued link, which is the intended
#: emergency lever.
PURPOSE = b"carigma.unsubscribe.v1"

#: Truncated to 32 hex chars — 128 bits. Forging one is infeasible, and a
#: shorter tail keeps the URL something a person can paste out of an email
#: client that has helpfully wrapped it.
SIG_LENGTH = 32


def _subkey(service_key: str) -> bytes:
    """The derived key. The root never signs anything directly."""
    return hmac.new(service_key.encode("utf-8"), PURPOSE, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(user_id: str, *, service_key: str) -> str:
    """A token for this user's marketing subscription.

    Raises rather than returning something unusable when the key is missing: a
    link signed with an empty key would verify against an empty key, which is
    every attacker's key too.
    """
    if not service_key:
        raise ValueError("cannot issue an unsubscribe token without a signing key")
    if not user_id:
        raise ValueError("cannot issue an unsubscribe token without a user")

    payload = _b64(user_id.encode("utf-8"))
    signature = hmac.new(_subkey(service_key), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature[:SIG_LENGTH]}"


def resolve(token: str, *, service_key: str) -> str | None:
    """The user this token belongs to, or None if it cannot be read.

    None covers every failure — malformed, wrong signature, truncated by an
    email client. The caller must not distinguish them to the user, and does
    not have the information to anyway.
    """
    if not service_key or not token or "." not in token:
        return None

    payload, _, signature = token.partition(".")
    expected = hmac.new(_subkey(service_key), payload.encode("ascii"), hashlib.sha256).hexdigest()

    # `compare_digest`, not `==`. A timing-variable comparison on a signature
    # is the textbook way to leak it a byte at a time.
    if not hmac.compare_digest(expected[:SIG_LENGTH], signature):
        return None

    try:
        user_id = _unb64(payload).decode("utf-8")
    except Exception:
        # A signature that verified over an undecodable payload means our own
        # issuer changed shape. Worth a log line; still not the user's problem.
        logger.error("unsubscribe token verified but its payload is unreadable")
        return None

    return user_id or None


def link(user_id: str, *, service_key: str, app_url: str) -> str:
    """The URL that goes in an email footer."""
    return f"{app_url.rstrip('/')}/unsubscribe/{issue(user_id, service_key=service_key)}"


#: What every caller sees, whatever happened.
#:
#: The response is identical for a token we honoured and one we could not read,
#: because the difference is not the sender's business — the same
#: non-enumeration rule as password reset. The landing page distinguishes them
#: only to say whether it could read the LINK.
DONE: dict[str, Any] = {
    "done": True,
    "scope": "marketing emails",
    "note": "You will still get receipts and anything you ask us for directly.",
}


__all__ = ["DONE", "PURPOSE", "SIG_LENGTH", "issue", "link", "resolve"]
