"""Payment routes — dormant by default, and provably closed.

The acceptance criterion from the brief: *"flipping the flag off leaves no
reachable payment path."* That is what most of this file checks, because a
half-disabled payment surface is worse than none — it is a control about money
that looks live.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.config import Settings, get_settings
from tests.conftest import auth, make_token


@pytest.fixture
def live_settings(settings: Settings) -> Settings:
    """A settings object with payments switched on and test keys present."""
    return settings.model_copy(
        update={
            "payments_enabled": True,
            "razorpay_key_id": "rzp_test_abc123",
            "razorpay_key_secret": "rzp_test_secret",
        }
    )


@pytest.fixture
def live_client(client: TestClient, live_settings: Settings) -> Iterator[TestClient]:
    client.app.dependency_overrides[get_settings] = lambda: live_settings  # type: ignore[attr-defined]
    yield client
    client.app.dependency_overrides[get_settings] = lambda: live_settings  # type: ignore[attr-defined]


# ── Dormant: no reachable payment path ─────────────────────────────────────


def test_payments_are_off_by_default(settings: Settings) -> None:
    """The safe default. An accidental deploy must not start taking money."""
    assert Settings().payments_enabled is False
    assert Settings().payments_live is False


def test_the_flag_alone_is_not_enough(settings: Settings) -> None:
    """Enabled with no keys would render a checkout button that cannot work."""
    half = settings.model_copy(update={"payments_enabled": True})
    assert half.payments_live is False

    keys_only = settings.model_copy(
        update={"razorpay_key_id": "rzp_test_x", "razorpay_key_secret": "s"}
    )
    assert keys_only.payments_live is False


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/payments/topup", {"pack_key": "booster"}),
        ("post", "/payments/verify", {"link_id": "plink_1"}),
    ],
)
def test_every_mutating_route_is_unreachable_while_dormant(
    client: TestClient, method: str, path: str, body: dict[str, Any]
) -> None:
    res = getattr(client, method)(path, json=body, headers=auth(make_token()))
    assert res.status_code == 404


def test_a_dormant_route_404s_rather_than_403s(client: TestClient) -> None:
    """403 would confirm the endpoint exists and is merely gated — which is
    information about an unreleased feature."""
    res = client.post("/payments/topup", json={"pack_key": "booster"}, headers=auth(make_token()))
    assert res.status_code == 404
    assert res.json()["detail"]["error"] == "not_found"


def test_pricing_is_still_reachable_while_dormant(client: TestClient) -> None:
    """Hiding prices would make the product look like it has no plan for money.
    Showing a dead button would be the §25 bug class, about money."""
    res = client.get("/pricing")

    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is False
    assert len(body["packs"]) == 3
    assert set(body["unavailable"]) == {"happened", "why", "next"}


def test_the_dormant_pricing_names_the_real_alternative(client: TestClient) -> None:
    body = client.get("/pricing").json()
    assert "top you up" in body["unavailable"]["next"]


def test_pricing_needs_no_token(client: TestClient) -> None:
    """Someone deciding whether to sign up has no token yet."""
    assert client.get("/pricing").status_code == 200


# ── Live: the path opens ───────────────────────────────────────────────────


def test_pricing_drops_the_unavailable_block_when_live(live_client: TestClient) -> None:
    body = live_client.get("/pricing").json()
    assert body["enabled"] is True
    assert "unavailable" not in body


def test_an_unknown_pack_is_refused_rather_than_defaulted(
    live_client: TestClient,
) -> None:
    """Falling back to a default pack would charge someone for something they
    did not choose."""
    res = live_client.post(
        "/payments/topup", json={"pack_key": "enterprise"}, headers=auth(make_token())
    )
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "unknown_pack"


def test_topup_requires_a_verified_token(live_client: TestClient) -> None:
    assert live_client.post("/payments/topup", json={"pack_key": "booster"}).status_code == 401


def test_verify_requires_a_verified_token(live_client: TestClient) -> None:
    assert live_client.post("/payments/verify", json={"link_id": "x"}).status_code == 401


def test_verifying_nothing_is_a_clear_400(live_client: TestClient) -> None:
    res = live_client.post("/payments/verify", json={}, headers=auth(make_token()))
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "nothing_to_verify"


def test_a_bad_signature_credits_nothing_and_says_so(live_client: TestClient) -> None:
    """A forged return must never reach the grant path."""
    res = live_client.post(
        "/payments/verify",
        json={
            "link_id": "plink_1",
            "razorpay_order_id": "order_1",
            "razorpay_payment_id": "pay_1",
            "razorpay_signature": "forged",
        },
        headers=auth(make_token()),
    )

    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "bad_signature"
    assert "Nothing was credited" in res.json()["detail"]["message"]


def test_test_mode_is_detectable(live_settings: Settings) -> None:
    """Admin surfaces which mode a running instance is in, so nobody has to
    guess whether a payment was real."""
    assert live_settings.razorpay_is_test_mode is True
    live = live_settings.model_copy(update={"razorpay_key_id": "rzp_live_abc"})
    assert live.razorpay_is_test_mode is False


def test_no_payment_route_leaks_a_stack_trace(client: TestClient) -> None:
    for res in (
        client.get("/pricing"),
        client.post("/payments/topup", json={"pack_key": "x"}, headers=auth(make_token())),
        client.post("/payments/verify", json={}, headers=auth(make_token())),
    ):
        assert "Traceback" not in res.text
        assert "razorpay" not in res.text.lower() or res.status_code == 200


# ── The flag must not leak the wrong way ───────────────────────────────────
# The flag exists so payments stay dormant until KYC clears. A default that
# leaks `true` into production defeats the entire point of building it early.


def test_the_shipped_default_is_off() -> None:
    """Not "off in .env" — off in the CODE. A server with no PAYMENTS_ENABLED
    set at all must not take money."""
    import os
    from unittest import mock

    with mock.patch.dict(os.environ, {}, clear=True):
        assert Settings(_env_file=None).payments_enabled is False  # type: ignore[call-arg]


def test_the_template_ships_false() -> None:
    """`.env.example` is what a new environment is built from. If it shipped
    `true`, every fresh deploy would start live."""
    from pathlib import Path

    template = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    lines = [
        ln.strip() for ln in template.splitlines() if ln.strip().startswith("PAYMENTS_ENABLED=")
    ]
    assert lines, "PAYMENTS_ENABLED is missing from .env.example"
    assert lines == ["PAYMENTS_ENABLED=false"], f"template ships {lines}"


def test_no_deploy_config_enables_payments() -> None:
    """Scans every deploy descriptor in the repo tree.

    Today this passes because no V2 service is defined yet — safety by
    absence, which is weak. It becomes a real guard the moment someone adds
    one, which is exactly when the mistake would otherwise be made.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    patterns = ("render.yaml", "render.yml", "Procfile", "Dockerfile", "fly.toml")
    offenders: list[str] = []

    for pattern in patterns:
        for path in root.rglob(pattern):
            if "node_modules" in path.parts or ".venv" in path.parts:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if line.strip().startswith("#") or "PAYMENTS_ENABLED" not in line:
                    continue
                # YAML splits key and value across lines:
                #     - key: PAYMENTS_ENABLED
                #       value: true
                # so look at this line AND the next two. A same-line-only
                # check missed exactly this, which is how it was found —
                # the guard was verified by breaking it and did not fail.
                window = " ".join(
                    ln for ln in lines[i : i + 3] if not ln.strip().startswith("#")
                ).lower()
                if any(t in window for t in ("true", "yes", "= 1", "=1", ": 1")):
                    offenders.append(f"{path.name}:{i + 1}: {line.strip()}")

    assert not offenders, "a deploy descriptor turns payments ON: " + "; ".join(offenders)
