"""Every route reachable without a token is rate limited, or says why not.

## The gap this closes

`enforce_public_rate_limit` and the whole per-IP bucket were built and tested
in P4-2 and **applied to nothing**. `PUBLIC_BURST`, `check_public`, the
`client_ip` parsing, the 429 shape — all correct, all covered, and no route
called any of it. Found by asking which public functions have no caller, not
by reading the code.

The worst of the four was `POST /auth/reset-password`: no token, and it sends
mail to an address the caller chooses. Unthrottled that is an email bomb aimed
at anyone, plus a free enumeration probe.

## Why this is a guard and not four fixes

Wiring the four routes fixes today. A route added next month is the same bug
again, and nothing would say so. This walks the router table, so a new public
endpoint is a failing test until someone decides about it.
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi.testclient import TestClient

#: Public AND deliberately unthrottled, each with the reason.
#:
#: Policed below: an entry that gains a limiter, or stops being public, fails.
#: An exemption list nobody re-checks is how five imaginary tables sat in
#: `NOT_OURS` for a phase.
UNTHROTTLED: dict[str, str] = {
    "/health": (
        "Uptime monitors poll this every few seconds by design. A 429 here "
        "would page someone about an outage that is not happening, and the "
        "endpoint returns no data worth harvesting."
    ),
}


def public_routes() -> dict[str, tuple[str, bool]]:
    """path -> (function name, whether the limiter is called).

    Built from the APP, not from the decorators.

    Reading the source directly got this wrong twice. The decorator says
    `/session` while the real path is `/auth/session`, because the router
    carries a prefix — so the exemption keys would never have matched. And
    excluding only `CurrentUser` and `AdminUser` classed `/auth/service-probe`
    as public when it requires `ServiceRole`; the staleness half of this guard
    is what caught that, which is the argument for policing exemptions in both
    directions.

    The app knows the real path and the full dependency list. The only thing it
    cannot tell us is whether the guard actually RUNS, so that part still comes
    from the function body.
    """
    from carigma_api.main import app

    found: dict[str, tuple[str, bool]] = {}

    def walk(routes: Any) -> None:
        for route in routes:
            endpoint = getattr(route, "endpoint", None)
            path = getattr(route, "path", None)
            if endpoint is not None and path:
                # FastAPI's own /docs and /openapi.json are framework routes,
                # already disabled in production by `create_app`. Ours are the
                # ones we can be wrong about.
                if not getattr(endpoint, "__module__", "").startswith("carigma_api"):
                    continue
                try:
                    source = inspect.getsource(endpoint)
                except (OSError, TypeError):
                    continue
                signature = inspect.signature(endpoint)
                annotations = " ".join(str(p.annotation) for p in signature.parameters.values())
                if any(role in annotations for role in ("CurrentUser", "AdminUser", "ServiceRole")):
                    continue
                found[path] = (endpoint.__name__, "enforce_public_rate_limit" in source)
            nested = getattr(route, "routes", None) or getattr(
                getattr(route, "original_router", None), "routes", None
            )
            if nested:
                walk(nested)

    walk(app.routes)
    return found


def test_the_scan_actually_finds_public_routes() -> None:
    """Assert the input. A walk that matched nothing would report a perfectly
    protected API — which is how a guard passes forever while checking zero
    things."""
    routes = public_routes()

    assert len(routes) >= 3, f"only found {sorted(routes)}"
    assert "/health" in routes
    # The prefixed path, which reading decorators alone would never produce.
    assert "/auth/reset-password" in routes


def test_every_public_route_is_throttled_or_listed() -> None:
    unprotected = [
        f"{route} ({fn})"
        for route, (fn, guarded) in sorted(public_routes().items())
        if not guarded and route not in UNTHROTTLED
    ]

    assert not unprotected, (
        "reachable without a token and not rate limited:\n  "
        + "\n  ".join(unprotected)
        + "\n\nAdd `enforce_public_rate_limit(request)`, or add the route to "
        "UNTHROTTLED with the reason."
    )


def test_the_unthrottled_list_cannot_outlive_its_reason() -> None:
    """Policed in both directions: an entry that gained a limiter is stale, and
    an entry that is no longer public should not be sitting here either."""
    routes = public_routes()
    stale = []

    for route, reason in UNTHROTTLED.items():
        if route not in routes:
            stale.append(f"{route} is no longer a public route — drop it")
        elif routes[route][1]:
            stale.append(f"{route} IS throttled now — drop it from UNTHROTTLED")
        assert len(reason) > 40, f"{route} is exempted without a real reason"

    assert not stale, "; ".join(stale)


def test_the_reset_endpoint_is_throttled() -> None:
    """Named explicitly, because it is the one that matters: no token, and it
    sends mail to an address the caller picks."""
    routes = public_routes()

    assert routes["/auth/reset-password"][1] is True


def test_a_public_burst_is_actually_refused(client: TestClient, frozen_public_clock: None) -> None:
    """Through HTTP, not against the limiter directly. The bucket was already
    proven correct in isolation — what was missing was any route consulting it.
    """
    from carigma_api.services.ratelimit import PUBLIC_BURST

    codes = [
        client.post("/auth/reset-password", json={"email": "a@b.c"}).status_code
        for _ in range(PUBLIC_BURST + 1)
    ]

    assert 429 not in codes[:PUBLIC_BURST], f"refused early: {codes}"
    assert codes[-1] == 429, f"the public limiter never fired: {codes}"


def test_the_429_tells_the_caller_when_to_come_back(
    client: TestClient, frozen_public_clock: None
) -> None:
    from carigma_api.services.ratelimit import PUBLIC_BURST

    last = None
    for _ in range(PUBLIC_BURST + 1):
        last = client.post("/auth/reset-password", json={"email": "a@b.c"})

    assert last is not None
    assert last.status_code == 429
    assert int(last.headers["Retry-After"]) >= 1
    # Rate limiting is not a credit problem. Saying so would send someone to
    # buy credits they already have.
    assert "credit" not in last.json()["detail"]["message"].lower()
