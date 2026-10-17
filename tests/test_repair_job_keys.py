"""The one-off key repair: the order it writes in, and what it never does."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "repair_job_keys", ROOT / "scripts" / "repair_job_keys.py"
)
assert _SPEC and _SPEC.loader
repair = importlib.util.module_from_spec(_SPEC)
sys.modules["repair_job_keys"] = repair
_SPEC.loader.exec_module(repair)

USER = "u1"


def row(id_: int, key: str, **kw: Any) -> dict[str, Any]:
    base = {
        "id": id_,
        "user_id": USER,
        "dedup_key": key,
        "title": "Senior Analyst E-Commerce",
        "company": "NuvoRetail",
        "status": "active",
        "kit_created_at": None,
        "first_seen": "2026-10-17T12:16:20+00:00",
        "last_seen": "2026-10-17T12:16:20+00:00",
    }
    base.update(kw)
    return base


def test_the_key_is_v1s() -> None:
    assert repair.v1_key("NuvoRetail", "Senior Analyst E-Commerce") == "7e90d297588b9dd4"


def test_a_v1_dismissal_keeps_the_key_and_the_v2_copy_steps_aside() -> None:
    """NuvoRetail, measured: #37 dismissed in July under V1's key, #136 the
    same posting brought back as new by V2's."""
    rows = [
        row(
            37,
            "7e90d297588b9dd4",
            status="dismissed",
            first_seen="2026-07-14T07:40:38+00:00",
            last_seen="2026-07-18T07:50:33+00:00",
        ),
        row(136, "senior analyst e-commerce|nuvoretail|delhi"),
    ]

    steps = repair.plan(rows)

    assert [(a, r["id"]) for a, r, _p in steps] == [("duplicate", 136), ("keep", 37)]
    _a, _r, aside = steps[0]
    assert aside["status"] == "dismissed"
    assert aside["dismiss_reason"] == "Duplicate of #37"
    assert aside["dedup_key"] != "7e90d297588b9dd4", "the duplicate must free the key"
    # The keeper learns it was seen again, and keeps its decision.
    assert steps[1][2] == {"last_seen": "2026-10-17T12:16:20+00:00"}


def test_every_duplicate_steps_off_the_key_before_a_keeper_takes_it() -> None:
    """`(user_id, dedup_key)` is unique. Re-keying a keeper while a duplicate
    still holds the key would fail half-way through the repair."""
    rows = [
        row(
            126,
            "senior analyst, market insights|ets|delhi",
            title="Senior Analyst, Market Insights",
            company="ETS",
            first_seen="2026-10-17T04:41:54+00:00",
        ),
        row(
            135,
            "senior analyst, market insights|ets|in",
            title="Senior Analyst, Market Insights",
            company="ETS",
            first_seen="2026-10-17T04:47:45+00:00",
        ),
        row(132, "package consultant|ibm|gurugram", title="Package Consultant", company="IBM"),
    ]

    actions = [a for a, _r, _p in repair.plan(rows)]

    assert actions == sorted(actions, key=lambda a: a != "duplicate")
    assert actions.count("duplicate") == 1


def test_v1s_own_rows_are_left_exactly_as_they_are() -> None:
    rows = [
        row(1, "7e90d297588b9dd4"),
        row(2, repair.v1_key("TCS", "BI Analyst"), company="TCS", title="BI Analyst"),
    ]

    assert repair.plan(rows) == []


def test_the_repair_never_deletes() -> None:
    """A migration may not delete rows while V1 is live, and a repair should
    not either — every row stays, with the reason it moved."""
    import inspect

    source = inspect.getsource(repair)
    assert ".delete(" not in source
