"""Is the JSearch daily cap counting? Ask the database, as a real user.

    ./run python scripts/verify_cap.py

Run after V2_018 is applied. Three checks, and the exit code is the verdict:

1. **The counter answers a signed-in user with a number.** The 0.063 cap read
   `jobs_cache` through the user's client and got zero rows with no error —
   the failure this whole check exists for.
2. **The number is what it should be.** Recounted independently with the
   service role: V1's `jobs_cache` fetches today plus V2's logged JSearch calls
   today. A counter that answers with the wrong number is a cap set by
   accident.
3. **A signed-in user can log a call, and read it back.** `RecordedProvider`
   writes this row BEFORE every provider call and makes no call without it.
   Logged as `adzuna`, so this check never adds to the JSearch count, and
   removed afterwards.

Uses the smoke account from `.env`. Writes one probe row and deletes it.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from smoke import sign_in  # noqa: E402

from carigma_api.config import get_settings  # noqa: E402
from carigma_api.services.jobs import quota  # noqa: E402
from carigma_api.services.repository import service_client, user_client  # noqa: E402

PROBE_KEY = "verify-cap:probe"


def main() -> int:
    settings = get_settings()
    service = service_client(settings)
    token = sign_in(settings)
    as_user = user_client(settings, token)
    failures: list[str] = []

    counted = quota.calls_today(as_user)
    print(f"1. counter, as the user:           {counted!r}")
    if counted is None:
        failures.append(
            "the counter did not answer a signed-in user with a number (is V2_018 applied?)"
        )

    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        v1 = (
            service.table("jobs_cache")
            .select("id", count="exact")
            .in_("source", ["jsearch", "mixed"])
            .gte("fetched_at", midnight)
            .execute()
            .count
        )
        v2 = (
            service.table(quota.LEDGER)
            .select("id", count="exact")
            .eq("source", "jsearch")
            .gte("called_at", midnight)
            .execute()
            .count
        )
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        print(f"   recount failed: {str(exc)[:160]}")
        print("\nVERDICT: the cap is NOT verified — the call log is not there. Apply V2_018.")
        return 1
    expected = None if v1 is None or v2 is None else v1 + v2
    print(f"2. recounted, as the service role: {expected!r}  (V1 {v1}, V2 {v2})")
    if counted is not None and expected is not None and counted != expected:
        failures.append(f"the counter said {counted}, the tables say {expected}")

    try:
        as_user.table(quota.LEDGER).insert(
            {"user_id": _sub(token), "source": "adzuna", "query_key": PROBE_KEY}
        ).execute()
        seen = as_user.table(quota.LEDGER).select("id").eq("query_key", PROBE_KEY).execute().data
        print(f"3. user logs a call, reads it back: {'yes' if seen else 'NO'}")
        if not seen:
            failures.append("the call was logged but the user cannot read it back")
    except Exception as exc:  # noqa: BLE001 — the refusal IS the finding
        print(f"3. user logs a call:                REFUSED — {str(exc)[:160]}")
        failures.append("a signed-in user cannot log a call, so every scan will refuse")
    finally:
        service.table(quota.LEDGER).delete().eq("query_key", PROBE_KEY).execute()

    if failures:
        print("\nVERDICT: the cap is NOT verified.\n  - " + "\n  - ".join(failures))
        return 1
    print("\nVERDICT: the cap counts V1 and V2 together, as a signed-in user can reach it.")
    return 0


def _sub(token: str) -> str:
    import base64
    import json

    payload = token.split(".")[1]
    return str(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"])


if __name__ == "__main__":
    raise SystemExit(main())
