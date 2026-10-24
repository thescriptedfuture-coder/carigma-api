"""Is everything the domain switch depends on in place? Read-only.

    ./run cutover-check            before the switch: is V2 ready to take over?
    ./run cutover-check --after    after it: is app.carigma.in now V2, whole?

Sends no email, writes no row, changes no setting: every check is a GET or
a CORS preflight against a public address. Exits 0 only when every check the
phase requires passes. The runbook is `CUTOVER.md` beside the two repos.

## Why a script

On the day, app.carigma.in moves from V1's Render service to V2's. Half of
what that needs lives in dashboards nothing here can read (DNS, Render's
custom domain, Supabase's URLs and templates), and the failure of each is
silent until a user meets it: a CORS miss is a front page that cannot load a
thing, a missing rewrite is a reset link that 404s. So this checks by
CONSEQUENCE — what the domain serves, what the API allows — rather than
reading any setting, and names what it cannot see.

## What it judges

| check         | before the switch                   | after it                        |
|---------------|-------------------------------------|---------------------------------|
| api           | answers `ok`                        | same                            |
| web           | reports its build                   | same                            |
| app domain    | V1 still answering is expected      | serves the web's exact build    |
| deep links    | V2 serves /auth/reset and /today    | same, on app.carigma.in         |
| cors          | the API allows https://app.carigma.in | same                          |
| v1 fallback   | V1 answers at its own address       | same: it is the way back        |
| legal         | carigma.in serves terms and privacy | same                            |

CORS is required BEFORE the switch on purpose: allowing an origin that does
not serve V2 yet costs nothing, and without it the first minutes after the
DNS change are a product that cannot read a thing.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import TextIO

APP = "https://app.carigma.in"
API = "https://carigma-api.onrender.com"
WEB = "https://carigma-web.onrender.com"
#: V1's own Render address — kept running after the switch as the way back.
V1 = "https://resumeiq-5k0u.onrender.com"
SITE = "https://carigma.in"

#: What cannot be read from here, in the order the runbook sets it.
BY_HAND = (
    "Supabase > Auth > URL configuration: Site URL and redirect allow-list",
    "Supabase > Auth > Emails: reset and confirm templates (?token_hash= form)",
    "Supabase > Auth > SMTP: custom SMTP (support@carigma.in), not the built-in mailer",
    "Google Cloud > OAuth client: https://app.carigma.in as an origin",
    "Render > carigma-api and the 4 crons: APP_URL, SUNDAY_AUDIENCE, EMAIL_AUDIENCE",
)


class Phase(Enum):
    BEFORE = "before"
    AFTER = "after"


class Mark(Enum):
    OK = "ok"
    FAIL = "FAIL"
    #: True and expected for this phase; neither a pass nor a failure.
    NOTE = "note"


@dataclass(frozen=True)
class Result:
    name: str
    mark: Mark
    summary: str


@dataclass(frozen=True)
class Response:
    status: int
    body: str
    headers: dict[str, str]


#: What a fetch returns when there was no HTTP answer at all.
@dataclass(frozen=True)
class Unreachable:
    reason: str


Fetched = Response | Unreachable


def fetch(
    url: str, *, method: str = "GET", headers: dict[str, str] | None = None, timeout: float = 90.0
) -> Fetched:
    """One request. Never raises: an HTTP error is a Response, no answer is Unreachable."""
    request = urllib.request.Request(  # noqa: S310
        url,
        method=method,
        headers={
            "User-Agent": "carigma-cutover-check",
            "Cache-Control": "no-cache",
            **(headers or {}),
        },
    )
    try:
        # S310: fixed https URLs declared above, never user input.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return Response(
                response.status,
                response.read(200_000).decode("utf-8", "replace"),
                {k.lower(): v for k, v in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return Response(exc.code, "", {k.lower(): v for k, v in exc.headers.items()})
    except ssl.SSLError as exc:
        return Unreachable(f"TLS: {exc}")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            return Unreachable(f"TLS: {reason}")
        return Unreachable(str(reason))
    except (TimeoutError, OSError) as exc:
        return Unreachable(str(exc) or type(exc).__name__)


def _commit(fetched: Fetched) -> str | None:
    if not isinstance(fetched, Response) or fetched.status != 200:
        return None
    try:
        payload = json.loads(fetched.body)
    except ValueError:
        return None
    commit = payload.get("commit") if isinstance(payload, dict) else None
    return commit if isinstance(commit, str) and commit and commit != "unknown" else None


def _is_v1(fetched: Fetched) -> bool:
    # Streamlit answers every path with its own index page.
    return isinstance(fetched, Response) and "streamlit" in fetched.body.lower()


def _spa(fetched: Fetched) -> bool:
    return isinstance(fetched, Response) and fetched.status == 200 and 'id="root"' in fetched.body


def _why(fetched: Fetched) -> str:
    if isinstance(fetched, Unreachable):
        return f"no answer ({fetched.reason})"
    return f"HTTP {fetched.status}"


# ── The judgments: pure, so the tests can hand them any answer ─────────────


def judge_api(health: Fetched) -> Result:
    if isinstance(health, Response) and health.status == 200:
        try:
            ok = json.loads(health.body).get("status") == "ok"
        except (ValueError, AttributeError):
            ok = False
        if ok:
            return Result("api", Mark.OK, f"{API} answers ok")
    return Result("api", Mark.FAIL, f"{API}/health: {_why(health)}")


def judge_web(build: Fetched) -> Result:
    commit = _commit(build)
    if commit:
        return Result("web", Mark.OK, f"{WEB} runs {commit}")
    return Result("web", Mark.FAIL, f"{WEB}/build.json reports no build: {_why(build)}")


def judge_app_domain(app_build: Fetched, web_commit: str | None, phase: Phase) -> Result:
    name = "app domain"
    served = _commit(app_build)
    if served and web_commit and served == web_commit:
        return Result(name, Mark.OK, f"{APP} serves V2, build {served}")
    if served:
        return Result(
            name,
            Mark.FAIL,
            f"{APP} serves a V2 build ({served}), not the one {WEB} runs ({web_commit or '?'})"
            " - a second V2 service, or a stale one, holds the domain",
        )
    if _is_v1(app_build):
        if phase is Phase.BEFORE:
            return Result(name, Mark.NOTE, f"{APP} is still V1, as it should be before the switch")
        return Result(
            name,
            Mark.FAIL,
            f"{APP} is still V1. DNS may not have propagated, or the custom domain"
            " is still on V1's Render service",
        )
    return Result(name, Mark.FAIL, f"{APP} serves neither V1 nor V2: {_why(app_build)}")


def judge_deep_links(host: str, pages: dict[str, Fetched]) -> Result:
    broken = [f"{path} {_why(f)}" for path, f in pages.items() if not _spa(f)]
    if not broken:
        return Result("deep links", Mark.OK, f"{host} serves {', '.join(pages)}")
    return Result(
        "deep links",
        Mark.FAIL,
        f"{host}: {'; '.join(broken)} - an emailed link to these would 404 (the rewrite to"
        " /index.html)",
    )


def judge_cors(preflight: Fetched) -> Result:
    allowed = (
        preflight.headers.get("access-control-allow-origin")
        if isinstance(preflight, Response)
        else None
    )
    if allowed in (APP, "*"):
        return Result("cors", Mark.OK, f"the API allows {APP}")
    return Result(
        "cors",
        Mark.FAIL,
        f"the API does not allow {APP} ({_why(preflight)}): add it to CORS_ORIGINS on"
        " carigma-api, keeping the onrender address",
    )


def judge_v1(health: Fetched) -> Result:
    if isinstance(health, Response) and health.status == 200:
        return Result("v1 fallback", Mark.OK, f"V1 answers at {V1} - the way back is open")
    return Result(
        "v1 fallback",
        Mark.FAIL,
        f"V1 does not answer at {V1} ({_why(health)}): with no way back, do not switch",
    )


def judge_legal(pages: dict[str, Fetched]) -> Result:
    down = [
        f"{path} {_why(f)}"
        for path, f in pages.items()
        if not (isinstance(f, Response) and f.status == 200)
    ]
    if not down:
        return Result("legal", Mark.OK, f"{SITE} serves {', '.join(pages)}")
    return Result(
        "legal",
        Mark.FAIL,
        f"{SITE}: {'; '.join(down)} - Settings and sign-up link here",
    )


# ── Running it ─────────────────────────────────────────────────────────────


def run(phase: Phase, timeout: float) -> list[Result]:
    stamp = f"check={int(time.time())}"
    results = [judge_api(fetch(f"{API}/health", timeout=timeout))]

    web_build = fetch(f"{WEB}/build.json?{stamp}", timeout=timeout)
    results.append(judge_web(web_build))
    results.append(
        judge_app_domain(
            fetch(f"{APP}/build.json?{stamp}", timeout=timeout), _commit(web_build), phase
        )
    )

    host = APP if phase is Phase.AFTER else WEB
    results.append(
        judge_deep_links(
            host,
            {path: fetch(f"{host}{path}", timeout=timeout) for path in ("/auth/reset", "/today")},
        )
    )
    results.append(
        judge_cors(
            fetch(
                f"{API}/health",
                method="OPTIONS",
                headers={
                    "Origin": APP,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
                timeout=timeout,
            )
        )
    )
    results.append(judge_v1(fetch(f"{V1}/_stcore/health", timeout=timeout)))
    results.append(
        judge_legal(
            {
                path: fetch(f"{SITE}{path}", timeout=timeout)
                for path in ("/terms.html", "/privacy.html")
            }
        )
    )
    return results


def report(results: list[Result], phase: Phase, out: TextIO) -> bool:
    """Print every result, then what only a person can check. True when none failed.

    ASCII only, like `deployed.py`: a Windows console must be able to print
    the failures this exists to deliver.
    """
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{r.name:<{width}}  {r.mark.value:<4}  {r.summary}", file=out)

    print(file=out)
    print("Not readable from here - confirm by hand (CUTOVER.md):", file=out)
    for line in BY_HAND:
        print(f"  [ ] {line}", file=out)

    passed = all(r.mark is not Mark.FAIL for r in results)
    print(file=out)
    if phase is Phase.BEFORE:
        print("Ready to switch." if passed else "NOT ready to switch.", file=out)
    else:
        print("app.carigma.in is V2." if passed else "The switch is NOT complete.", file=out)
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().splitlines()[0])
    parser.add_argument(
        "--after",
        action="store_true",
        help="The DNS switch has been made: app.carigma.in must now be V2.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Seconds to wait for each address. A sleeping free instance takes ~50.",
    )
    args = parser.parse_args()
    phase = Phase.AFTER if args.after else Phase.BEFORE
    return 0 if report(run(phase, args.timeout), phase, sys.stdout) else 1


if __name__ == "__main__":
    sys.exit(main())
