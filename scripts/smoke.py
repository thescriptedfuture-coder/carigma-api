"""Sign in to the DEPLOYED system as a real user and read every surface.

    run smoke                 (Windows)      ./run smoke        (bash)
    run smoke --no-upload     skip the one write

The layer this project did not have. Both suites run against fakes — the web's
e2e against MSW, the API's against `FakeDB` — so neither side has ever spoken to
the other. Every defect of the last month lived in that gap: an axios default
that turned uploads into JSON, an upsert resolving on a primary key the live
database does not have, a Render branch nobody could see. Each was found by a
person clicking, after a deploy.

## What it does, and what it deliberately does not

- Signs in with `SMOKE_EMAIL` / `SMOKE_PASSWORD` from `.env` — an ordinary
  account on production, not the service role. What it can reach is what a user
  can reach.
- **Reads** every surface an onboarded user sees, asserting a status AND a key
  the client actually renders. A 200 carrying the wrong shape is the failure
  this project keeps having.
- **Writes once**: a synthetic profile through `/onboarding/extract`. That one
  call exercises the upload encoding, `pdfplumber` on Render, the "is this a
  work profile?" check against the real model, and — because the account
  already has a profile row — the `profiles` upsert that returned 503 for a
  week. Skippable with `--no-upload`.
- **Never** triggers a job scan. `JSEARCH_DAILY_CAP` is 25 and V1's live users
  draw on the same key, so `/jobs/feed` (which only reads rows) is in and
  anything that calls a provider is out.
- Never spends credits: no agent run, no score, no post regeneration.

It is not a test suite and does not belong in CI: it needs credentials and it
touches production data. Run it after a deploy, with `run deployed`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carigma_api.config import Settings  # noqa: E402

API = "https://carigma-api.onrender.com"

#: Surfaces an onboarded user's app calls, and one key each that the client
#: renders. The key matters: `/today` answering 200 with a body the web cannot
#: read is the failure mode this exists for, and a status check alone would
#: call it healthy.
READS: tuple[tuple[str, str | None], ...] = (
    ("/health", "commit"),
    ("/pricing", None),
    ("/onboarding", "step"),
    ("/today", "primary_action"),
    ("/thread", "items"),
    ("/credits", "balance"),
    ("/profile", None),
    ("/profile/email-preferences", None),
    ("/profile/updates/open", "change"),
    ("/posts/week", None),
    ("/weekly/contract", None),
    ("/weekly/review", None),
    ("/weekly/standing-by", None),
    ("/score/history", None),
    ("/jobs/feed", "items"),
    ("/naukri/score", None),
    ("/referrals", "code"),
    # Added by `tests/test_smoke.py`, which derives this list from the recorded
    # contract rather than trusting it: the first draft left it out.
    ("/market/wire", None),
)

#: Obviously synthetic, because it lands on the smoke account's real profile.
SMOKE_PROFILE = [
    "SMOKE TEST PROFILE - not a real person",
    "Priya Testcase",
    "Senior Data Analyst at Example Analytics, Bengaluru",
    "Six years turning operational data into decisions.",
    "Skills: SQL, Python, Tableau, Power BI",
    "Experience: Example Analytics 2021-now. Sample Corp 2018-2021.",
    "Education: B.Tech, Example Institute of Technology",
]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    millis: int


def _pdf(lines: list[str]) -> bytes:
    """A minimal, valid, text-bearing PDF, built here so the upload exercises
    the real reader rather than a fixture nobody can regenerate."""
    stream = "BT /F1 12 Tf 54 720 Td 16 TL " + " ".join(f"({x}) Tj T*" for x in lines) + " ET"
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%b\nendstream" % (len(stream), stream.encode("latin-1")),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%b\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1,
        xref,
    )
    return bytes(out)


def _request(
    url: str, *, headers: dict[str, str], data: bytes | None = None, timeout: float = 90
) -> tuple[int, Any, int]:
    """(status, parsed body, milliseconds). An error response is read, not
    raised: its body is the thing worth seeing."""
    started = time.monotonic()
    request = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, str(exc), int((time.monotonic() - started) * 1000)
    millis = int((time.monotonic() - started) * 1000)
    try:
        return status, json.loads(raw or b"null"), millis
    except json.JSONDecodeError:
        return status, raw[:200].decode("utf-8", "replace"), millis


def sign_in(settings: Settings) -> str:
    """An ordinary user's access token, from Supabase's password grant."""
    if not (settings.smoke_email and settings.smoke_password):
        raise SystemExit(
            "SMOKE_EMAIL and SMOKE_PASSWORD are not set in .env.\n"
            "They belong to a throwaway account on the deployed system; see .env.example."
        )
    status, body, _ms = _request(
        f"{settings.supabase_url.rstrip('/')}/auth/v1/token?grant_type=password",
        headers={
            "apikey": settings.supabase_anon_key,
            "Content-Type": "application/json",
        },
        data=json.dumps(
            {"email": settings.smoke_email, "password": settings.smoke_password}
        ).encode(),
    )
    token = body.get("access_token") if isinstance(body, dict) else None
    if status != 200 or not token:
        # Supabase answers with `msg`, `error_description`, `error_code` or
        # `error` depending on the failure, and the first draft read only one of
        # them — so a wrong password and an unconfirmed address both printed
        # "None". Show whichever it sent, and the whole body if it sent none.
        detail = ""
        if isinstance(body, dict):
            for field in ("msg", "error_description", "error", "error_code", "message"):
                if body.get(field):
                    detail = f"{field}: {body[field]}"
                    break
            detail = detail or json.dumps(body)[:200]
        else:
            detail = str(body)[:200]
        raise SystemExit(
            f"could not sign in as the smoke account ({status}) - {detail}\n"
            "Check SMOKE_EMAIL / SMOKE_PASSWORD in .env, and that the address is confirmed."
        )
    return str(token)


def read_surfaces(token: str, base: str) -> list[Check]:
    checks: list[Check] = []
    for path, key in READS:
        status, body, millis = _request(
            f"{base}{path}", headers={"Authorization": f"Bearer {token}"}
        )
        if status != 200:
            shown = body if isinstance(body, str) else json.dumps(body)[:160]
            checks.append(Check(f"GET {path}", False, f"{status}: {shown}", millis))
            continue
        if key and not (isinstance(body, dict) and key in body):
            # 200 with the wrong shape. The web renders `undefined` for a key
            # that is not there, which looks exactly like an empty surface.
            keys = sorted(body)[:6] if isinstance(body, dict) else type(body).__name__
            checks.append(Check(f"GET {path}", False, f"200 but no '{key}' — got {keys}", millis))
            continue
        checks.append(Check(f"GET {path}", True, str(status), millis))
    return checks


def upload_profile(token: str, base: str) -> Check:
    """The one write. Also the only place anything reads a PDF on Render."""
    boundary = "----carigma-smoke-boundary"
    pdf = _pdf(SMOKE_PROFILE)
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="smoke-profile.pdf"\r\n',
            b"Content-Type: application/pdf\r\n\r\n",
            pdf,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    status, parsed, millis = _request(
        f"{base}/onboarding/extract",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        data=body,
        timeout=180,
    )
    if status != 200:
        detail = parsed if isinstance(parsed, str) else json.dumps(parsed)[:220]
        return Check("POST /onboarding/extract", False, f"{status}: {detail}", millis)
    found = parsed.get("extracted") if isinstance(parsed, dict) else None
    if not found:
        return Check("POST /onboarding/extract", False, "200 but extracted nothing", millis)
    return Check("POST /onboarding/extract", True, f"read {len(found)} fields", millis)


def report(checks: list[Check], out: TextIO) -> bool:
    """ASCII only, and a test holds it to a strict cp1252 stream: a check that
    crashes while printing its failures is worse than no check."""
    for check in checks:
        mark = "ok  " if check.ok else "FAIL"
        print(f"{mark} {check.name:<32} {check.millis:>6}ms  {check.detail}", file=out)
    failed = [c for c in checks if not c.ok]
    print(file=out)
    if failed:
        print(f"{len(failed)} of {len(checks)} surfaces are broken for a real user.", file=out)
        return False
    print(f"All {len(checks)} surfaces answered, with the shape the client reads.", file=out)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().splitlines()[0])
    parser.add_argument("--api", default=API, help=f"which deployment to read (default {API})")
    parser.add_argument(
        "--no-upload", action="store_true", help="skip the one write (the synthetic profile)"
    )
    args = parser.parse_args()

    settings = Settings()
    token = sign_in(settings)
    print(f"signed in as {settings.smoke_email} against {args.api}\n")

    checks = read_surfaces(token, args.api)
    if not args.no_upload:
        checks.append(upload_profile(token, args.api))
        # Read back: the upload saves a profile, and the point of the write is
        # that the NEXT read sees it. This is where the upsert bug surfaced.
        checks.extend(
            c
            for c in read_surfaces(token, args.api)
            if c.name in ("GET /profile", "GET /onboarding")
        )
    return 0 if report(checks, sys.stdout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
