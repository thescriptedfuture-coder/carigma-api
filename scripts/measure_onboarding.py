"""What the live profiles actually contain, before onboarding is written.

    python scripts/measure_onboarding.py

Read-only. Prints per-column fill rates over `profiles` so the onboarding path
is built against what a real row looks like rather than against the column
list. The column list says every field is possible; the data says which ones
users actually arrive with, which is a different question and the one that
decides what onboarding must produce and what it may leave empty.

Prints no field VALUES — only counts. A script that dumps twelve people's
headlines to a terminal is a data-handling decision nobody made.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carigma_api.config import get_settings  # noqa: E402
from carigma_api.services.repository import _PROFILE_COLUMNS  # noqa: E402

#: The fields onboarding is responsible for producing, per Brief 3a.
WRITTEN_BY_ONBOARDING = [
    "name",
    "current_role",
    "linkedin_headline",
    "about_section",
    "skills",
    "experience",
    "education",
    "certifications",
    "industry",
    "target_roles",
    "target_role",
    "location",
    "platforms",
    "cadence",
    "onboarded",
]


def main() -> int:
    settings = get_settings()
    # Through Settings, never `os.getenv`. A direct env read is invisible to
    # both sides of the `.env.example` guard, which is exactly how the SMTP
    # fields existed for a whole phase without appearing in the template.
    key = settings.supabase_service_key
    if not key:
        print("SUPABASE_SERVICE_KEY is not set; this script only reads, but it needs it.")
        return 2

    from supabase import create_client

    client = create_client(settings.supabase_url, key)
    rows = (client.table("profiles").select("*").execute().data) or []
    total = len(rows)
    print(f"profiles: {total} rows\n")

    if not total:
        print("No rows. Nothing can be concluded about what users arrive with.")
        return 1

    print(f"{'column':<26} {'filled':>7}  {'%':>5}")
    print("-" * 42)
    for column in WRITTEN_BY_ONBOARDING:
        filled = sum(1 for r in rows if r.get(column) not in (None, "", [], {}))
        print(f"{column:<26} {filled:>4}/{total:<3} {100 * filled // total:>4}%")

    print("\nonboarded:", Counter(str(r.get("onboarded")) for r in rows).most_common())
    print("cadence:  ", Counter(str(r.get("cadence")) for r in rows).most_common())
    print("platforms:", Counter(str(r.get("platforms")) for r in rows).most_common())

    # Columns the database has that the API cannot address. `last_seen_jobs_at`
    # was exactly this and cost the "new since you last looked" signal.
    live = {c for r in rows for c in r}
    mapped = set(_PROFILE_COLUMNS.values()) | {"id", "user_id", "created_at", "updated_at"}
    unmapped = sorted(live - mapped)
    if unmapped:
        print(f"\nCOLUMNS THE API CANNOT WRITE OR READ: {unmapped}")
        for column in unmapped:
            filled = sum(1 for r in rows if r.get(column) not in (None, "", [], {}))
            print(f"  {column:<24} filled on {filled}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
