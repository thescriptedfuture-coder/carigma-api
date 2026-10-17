"""Re-key the `jobs_feed` rows V2 wrote with its own key. One-off. Dry run by default.

    ./run python scripts/repair_job_keys.py            # prints the plan, writes nothing
    ./run python scripts/repair_job_keys.py --apply    # does it

## Why

V1 keys a posting `sha1("company title")[:16]`. V2's first scans keyed it
`title|company|location`. So:

- a posting V1 had stored could never be recognised by a V2 scan — NuvoRetail's
  "Senior Analyst E-Commerce", dismissed in July, came back as new;
- one ETS posting was filed twice, because JSearch reported its location two
  ways six minutes apart.

The code now uses V1's key. This brings the rows already written into line, so
the next scan recognises them instead of filing each one a second time.

## What it does, per (user, V1 key) that has any V2-keyed row

- **keeper**: the row carrying a decision — dismissed or saved — else one with
  a paid apply kit, else the earliest `first_seen`. It gets the V1 key and the
  newest `last_seen` of the group.
- **every other row**: `status = 'dismissed'`, `dismiss_reason = 'Duplicate of
  #<keeper>'`, and its key moved aside so the keeper can take the V1 key
  (`(user_id, dedup_key)` is unique). Hidden in V2 and in V1, which shows every
  status except `dismissed`.

Nothing is deleted. A migration may not delete rows while V1 is live, and a
repair should not either: every row stays, readable, with the reason it moved.

Service role, because it rewrites rows across users. An admin path, run by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carigma_api.config import get_settings  # noqa: E402
from carigma_api.services.repository import service_client  # noqa: E402

V1_KEY = re.compile(r"^[0-9a-f]{16}$")


def v1_key(company: str | None, title: str | None) -> str:
    """`resumeiq/jobs_feed.py:dedup_key`, and `NormalizedJob.dedupe_key`."""
    base = re.sub(r"[^a-z0-9]+", " ", f"{company or ''} {title or ''}".lower()).strip()
    return hashlib.sha1(base.encode(), usedforsecurity=False).hexdigest()[:16]


def _keeper(group: list[dict[str, Any]]) -> dict[str, Any]:
    def rank(row: dict[str, Any]) -> tuple[int, int, str]:
        decided = 0 if str(row.get("status")) in ("dismissed", "saved") else 1
        kit = 0 if row.get("apply_kit") or row.get("kit_created_at") else 1
        return (decided, kit, str(row.get("first_seen") or "9999"))

    return min(group, key=rank)


def plan(rows: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """(action, row, patch) in the order they must be applied."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["user_id"]), v1_key(row.get("company"), row.get("title")))].append(row)

    moves_aside: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    rekeys: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for (_user, key), group in groups.items():
        if not any(not V1_KEY.match(str(r.get("dedup_key") or "")) for r in group):
            continue  # nothing V2-keyed here; V1's own rows are left exactly as they are
        keeper = _keeper(group)
        newest = max(str(r.get("last_seen") or "") for r in group) or None
        for row in group:
            if row is keeper:
                continue
            moves_aside.append(
                (
                    "duplicate",
                    row,
                    {
                        "dedup_key": f"dup-{row['id']}-of-{keeper['id']}",
                        "status": "dismissed",
                        "dismiss_reason": f"Duplicate of #{keeper['id']}",
                    },
                )
            )
        patch: dict[str, Any] = {}
        if keeper.get("dedup_key") != key:
            patch["dedup_key"] = key
        if newest and newest != keeper.get("last_seen"):
            patch["last_seen"] = newest
        if patch:
            rekeys.append(("keep", keeper, patch))
    # Every duplicate steps off the key before any keeper takes it.
    return moves_aside + rekeys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the plan (default: print it)")
    args = parser.parse_args()

    db = service_client(get_settings())
    rows = (
        db.table("jobs_feed")
        .select("id,user_id,dedup_key,title,company,status,kit_created_at,first_seen,last_seen")
        .execute()
        .data
        or []
    )
    steps = plan(rows)
    legacy = sum(1 for r in rows if not V1_KEY.match(str(r.get("dedup_key") or "")))
    print(f"{len(rows)} rows, {legacy} with a V2 key, {len(steps)} changes planned\n")
    for action, row, patch in steps:
        print(
            f"  {action:9} #{row['id']:<5} {str(row.get('status')):9} "
            f"{(row.get('title') or '')[:38]:38} | {(row.get('company') or '')[:22]:22} -> {patch}"
        )

    if not args.apply:
        print("\nDry run. Nothing was written. Re-run with --apply to make these changes.")
        return 0

    for _action, row, patch in steps:
        db.table("jobs_feed").update(patch).eq("id", row["id"]).execute()
    after = db.table("jobs_feed").select("dedup_key").execute().data or []
    still = sum(
        1
        for r in after
        if not V1_KEY.match(str(r.get("dedup_key") or ""))
        and not str(r.get("dedup_key")).startswith("dup-")
    )
    print(f"\nApplied {len(steps)} changes. Rows still on a V2 key: {still}.")
    return 0 if still == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
