"""Record V1's field vocabulary, so the web can be checked against it.

    ./run scripts/snapshot_v1_vocabulary.py

## Why this exists

`JobsPage` read `scan.scanned_line` and `scan.can_rescan`. Neither was invented:
both are defined in V1's `resumeiq/jobs_feed.py`. **The web was written against
V1's contract for a scan V2 never ported**, and the page blanked in production
because V2 sends neither.

That is a different class from "the web declares a field the API never sends":
those names are real, documented and working — in the other system. Nothing was
looking for them, so this records the vocabulary and
`carigma-web/src/lib/v1Vocabulary.test.ts` holds the web to it.

Like `schema_snapshot.json`: written deliberately, reviewed, committed. V1 lives
two directories up and CI has one repository, so the snapshot is the only thing
a test can read.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
V1 = ROOT.parents[1] / "resumeiq"
OUT = ROOT / "v1_vocabulary.json"

if not V1.exists():
    sys.exit(f"V1 is not beside this repository ({V1}) — nothing to record.")

keys: set[str] = set()
for path in sorted(V1.glob("*.py")):
    source = path.read_text(encoding="utf-8", errors="replace")
    # Dict-literal keys and `.get("...")` reads: the names V1's payloads use.
    keys |= set(re.findall(r"""["']([a-zA-Z_][a-zA-Z_0-9]{2,})["']\s*:""", source))
    keys |= set(re.findall(r"""\.get\(\s*["']([a-zA-Z_][a-zA-Z_0-9]{2,})["']""", source))
    # Function names too. `scanned_line` and `can_rescan` — the two names that
    # blanked the jobs feed — are DEFINED as functions in `jobs_feed.py` and
    # never appear as dict keys. A vocabulary built from keys alone missed
    # exactly the case it exists for, which a break-check caught.
    keys |= set(re.findall(r"^def\s+([a-zA-Z_][a-zA-Z_0-9]{2,})\s*\(", source, re.M))

if len(keys) < 100:
    sys.exit(f"only {len(keys)} keys found — refusing to write a truncated vocabulary")

OUT.write_text(json.dumps(sorted(keys), indent=2) + "\n", encoding="utf-8")
print(f"wrote {OUT.name}: {len(keys)} keys from {V1}")
