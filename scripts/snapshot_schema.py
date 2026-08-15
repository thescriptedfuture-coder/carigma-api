"""Record the live database's shape, so tests can check against it.

    python scripts/snapshot_schema.py        # writes schema_snapshot.json

## Why this exists

`test_migration_columns.py` could only check tables THIS repo declares. V1's
tables — `profiles`, `jobs_feed`, `credits` — were listed in a `NOT_OURS`
exemption and skipped, because their DDL lives in the Streamlit repo.

That list was an assumption wearing a fact's costume. **Five of its sixteen
entries named tables that exist nowhere at all**: `content_loop`, `tracker`,
`weekly_review`, `ai_memory`, `users`. Nobody had ever asked. So the guard
skipped five names that deserved to fail, and the exemption laundered a
mistaken belief into a permanent pass.

A snapshot of the real schema replaces the belief. Every table the code
touches is now checkable — ours from the migrations, V1's from here.

## It never regenerates itself

Same rule as `contract_keys.json`: run this deliberately, review the diff,
commit it. A snapshot that refreshed on every test run would agree with any
change, including the rename it exists to catch.
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "schema_snapshot.json"


def _env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        sys.exit(".env not found — this script needs live credentials.")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def fetch() -> dict[str, list[str]]:
    env = _env()
    url = env["SUPABASE_URL"].rstrip("/")
    key = env.get("SUPABASE_SERVICE_KEY") or ""
    if not key:
        sys.exit("SUPABASE_SERVICE_KEY is required to read the schema.")

    # S310: the URL is our own SUPABASE_URL from .env, and this is a developer
    # script run by hand — not a request path reachable by any user input.
    request = urllib.request.Request(  # noqa: S310
        f"{url}/rest/v1/",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/openapi+json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        doc = json.load(response)

    return {
        table: sorted(definition.get("properties", {}))
        for table, definition in sorted(doc.get("definitions", {}).items())
    }


def main() -> None:
    tables = fetch()
    if len(tables) < 10:
        sys.exit(f"only {len(tables)} tables returned — refusing to write a truncated snapshot")
    SNAPSHOT.write_text(json.dumps(tables, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT.name}: {len(tables)} tables")


if __name__ == "__main__":
    main()
