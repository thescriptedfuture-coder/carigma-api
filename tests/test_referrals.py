"""Referral attribution.

Four guarantees, and three of them are structural rather than checked:

1. **The grant is unreachable from a signup.** It takes an `Activation`, which
   only the profile-upload path can construct. Asserted by walking the AST.
2. **The cap is five slots the database arbitrates**, not a count this code
   compares. A count is read-then-write, which is V1's double-credit shape.
3. **Self-referral is a row the database will not hold.**
4. **Both sides are paid, with distinct ledger kinds.**
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime

import pytest

from carigma_api.services import referrals as mod
from carigma_api.services.referrals import (
    MAX_REFERRALS,
    REFERRED_CREDITS,
    REFERRER_CREDITS,
    Activation,
    CapReached,
    LedgerKind,
    SelfReferral,
    activation_from_profile_upload,
    assert_referable,
    grant_for_activation,
    new_code,
    next_slot,
    share_payload,
)

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def activation(user: str = "referred") -> Activation:
    return activation_from_profile_upload(user, now=NOW)


# ── 1. The grant cannot be reached from a signup ───────────────────────────


def test_the_grant_requires_an_activation_not_a_user_id() -> None:
    """The type IS the rule. There is no signature this can be called with
    from a registration handler."""
    signature = inspect.signature(grant_for_activation)
    first = list(signature.parameters.values())[0]

    assert first.annotation is Activation or first.annotation == "Activation"


def test_no_code_path_constructs_an_activation_except_the_upload_one() -> None:
    """The structural guarantee, asserted rather than asserted-to.

    If a second constructor appears — or if anything calls `Activation(...)`
    directly from somewhere that is not the profile-upload seam — this fails.
    That is the whole "never on signup" promise, made checkable.
    """
    tree = ast.parse(inspect.getsource(mod))

    constructors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name == "Activation":
            # Which function is it inside?
            constructors.append("<module-level or nested>")

    # Exactly one construction site, and it lives in the named seam.
    upload_fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "activation_from_profile_upload"
    )
    inside_upload = [
        n
        for n in ast.walk(upload_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Activation"
    ]

    assert len(inside_upload) == 1, "the upload seam must construct exactly one Activation"
    assert len(constructors) == 1, (
        "Activation is constructed somewhere other than activation_from_profile_upload — "
        "that is a path from signup to a credit grant"
    )


def test_the_grant_never_mentions_signup_or_registration() -> None:
    """A cheap, blunt second net. If someone wires a signup handler in here,
    the word tends to come with it."""
    source = inspect.getsource(mod).lower()

    for forbidden in ("def on_signup", "signup_handler", "on_register"):
        assert forbidden not in source


def test_an_activation_needs_the_user_who_activated() -> None:
    with pytest.raises(ValueError, match="who activated"):
        Activation(referred_id="", at=NOW)


# ── 2. The cap is slots, not a count ───────────────────────────────────────


def test_the_first_referral_takes_slot_one() -> None:
    assert next_slot(set()) == 1


def test_slots_fill_from_the_lowest_free_one() -> None:
    assert next_slot({1, 2}) == 3
    # A gap is reused — a referral removed for fraud should free its slot.
    assert next_slot({1, 3}) == 2


def test_the_sixth_referral_is_refused() -> None:
    with pytest.raises(CapReached, match=str(MAX_REFERRALS)):
        next_slot(set(range(1, MAX_REFERRALS + 1)))


def test_the_cap_message_explains_rather_than_erroring() -> None:
    """Hitting the cap is the programme working, not a fault."""
    with pytest.raises(CapReached) as exc:
        next_slot(set(range(1, MAX_REFERRALS + 1)))

    assert "most one account can earn" in str(exc.value)


def test_a_slot_outside_the_range_is_refused_even_if_asked_for() -> None:
    """The service refuses before the database has to. Both refuse."""
    with pytest.raises(CapReached):
        grant_for_activation(activation(), referrer_id="ref", slot=MAX_REFERRALS + 1)
    with pytest.raises(CapReached):
        grant_for_activation(activation(), referrer_id="ref", slot=0)


def test_this_module_never_compares_a_count_to_the_cap() -> None:
    """Read-then-write is the bug this design exists to avoid. A
    `len(...) >= MAX_REFERRALS` means the database has stopped being the
    arbiter and two concurrent activations can both win.

    AST, not substring. The first version listed spellings — `count(`,
    `count >=` — and a planted `len(taken) >= MAX_REFERRALS` walked straight
    past it. Same blind spot as a guard that checks identifiers while the
    violation sits in a string literal: it inspected one representation of the
    idea and the violation used another.
    """
    tree = ast.parse(inspect.getsource(mod))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        # Any comparison against the cap...
        against_cap = any(
            isinstance(c, ast.Name) and c.id == "MAX_REFERRALS" for c in node.comparators
        )
        if not against_cap:
            continue
        # ...whose left side is a size, however it is spelled.
        left = node.left
        is_size = isinstance(left, ast.Call) and (
            (isinstance(left.func, ast.Name) and left.func.id in {"len", "count", "sum"})
            or (isinstance(left.func, ast.Attribute) and left.func.attr in {"count", "size"})
        )
        if is_size:
            offenders.append(ast.unparse(node))

    assert not offenders, (
        "the cap is compared against a counted value — the database must be the "
        f"arbiter, not this code: {offenders}"
    )


# ── 3. Self-referral ───────────────────────────────────────────────────────


def test_referring_yourself_is_refused() -> None:
    with pytest.raises(SelfReferral):
        assert_referable("same-user", "same-user")


def test_the_grant_refuses_a_self_referral_too() -> None:
    """Not only the pre-check — the grant path itself."""
    with pytest.raises(SelfReferral):
        grant_for_activation(activation("same"), referrer_id="same", slot=1)


def test_a_missing_side_is_refused() -> None:
    with pytest.raises(ValueError):
        assert_referable("", "someone")


# ── 4. Both sides are paid, legibly ────────────────────────────────────────


def test_both_sides_get_credits() -> None:
    """Without the referred user's bonus the link is a favour asked rather
    than a gift given, and it converts far worse."""
    grant = grant_for_activation(activation(), referrer_id="ref", slot=1)

    assert grant.referrer_credits == REFERRER_CREDITS == 50
    assert grant.referred_credits == REFERRED_CREDITS == 20


def test_the_two_sides_are_distinct_ledger_kinds() -> None:
    """One kind with two amounts would make "what did referrals cost us"
    unanswerable without parsing reason strings."""
    rows = grant_for_activation(activation(), referrer_id="ref", slot=1).ledger_rows(NOW)

    kinds = {r["user_id"]: r["kind"] for r in rows}
    assert kinds["ref"] == str(LedgerKind.REFERRER) == "referral"
    assert kinds["referred"] == str(LedgerKind.REFERRED) == "referral_bonus"


def test_referral_kinds_are_distinct_from_every_other_kind() -> None:
    existing = {"spend", "signup", "grant", "purchase", "admin_adjustment", "refund"}

    assert str(LedgerKind.REFERRER) not in existing
    assert str(LedgerKind.REFERRED) not in existing


def test_the_ledger_rows_are_both_positive() -> None:
    rows = grant_for_activation(activation(), referrer_id="ref", slot=1).ledger_rows(NOW)

    assert all(r["delta"] > 0 for r in rows)


# ── Codes and sharing ──────────────────────────────────────────────────────


def test_codes_avoid_characters_that_are_misread_aloud() -> None:
    """A code gets copied out of a WhatsApp message or read down a phone."""
    for _ in range(50):
        code = new_code()
        assert len(code) == 8
        assert not (set(code) & set("0o1li")), f"{code} contains an ambiguous character"


def test_the_share_message_says_what_the_friend_gets() -> None:
    payload = share_payload("abcd2345")

    assert "20 free credits" in payload["message"]
    assert payload["link"].endswith("/r/abcd2345")
    assert payload["whatsapp_url"].startswith("https://wa.me/?text=")


def test_the_share_payload_states_the_cap_before_anyone_shares() -> None:
    """Someone about to ask five people deserves to know the reward is bounded
    before they ask, not after the sixth."""
    payload = share_payload("abcd2345")

    assert payload["cap"] == MAX_REFERRALS
    assert "upload a profile" in payload["cap_note"]
    assert "not when they sign up" in payload["cap_note"]
