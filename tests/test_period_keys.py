"""A period key must be a value `digest_log.sent_on` can hold.

## The bug

`digest_log.sent_on` is `date not null`. `weekly_key` returned `"2026-W34"`.
Postgres answers that with

    ERROR: 22007: invalid input syntax for type date: "2026-W34"

and `claim_period` catches every exception and returns `False` — which means
"already claimed". **So every weekly review and every re-engagement digest was
claimed, refused, and reported as a DUPLICATE.** Nothing errored. Nothing sent.

A dry run cannot reveal it: `send()` returns at the dry-run branch before it
claims, which is correct behaviour and also why four `--dry-run` cron passes
showed a clean run.

Same shape as `agent_runs.id`: a value the column cannot hold, a broad `except`
between us and the truth, and a failure that reads as an ordinary quiet day.

## What this file checks, and why not with a fake

`FakeLog.claim_period` is a set — it accepts any string as a key, so it can
never disagree with Postgres about a type. That is the same absence
`_NullRunStore` had. So this checks the VALUE against the DDL, with no log
involved at all.
"""

from __future__ import annotations

import ast
import inspect
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from carigma_api.services import emails as mail
from tests.schema import SOURCES_FOUND, types_of

#: Every function whose result is passed as `period_key`.
KEY_FUNCTIONS = [mail.daily_key, mail.weekly_key]

A_YEAR = [date(2026, 1, 1) + timedelta(days=n) for n in range(0, 366, 7)]
AROUND_BOUNDARIES = [
    date(2026, 8, 23),  # Sunday
    date(2026, 8, 24),  # the Monday after it
    date(2026, 12, 31),
    date(2027, 1, 1),  # ISO year boundary, where a week number changes year
    date(2024, 2, 29),  # leap day
]


def test_the_schema_is_readable_at_all() -> None:
    """The precondition. Every assertion below reads the DDL, and a parser that
    finds nothing would make all of them pass on any value."""
    assert SOURCES_FOUND > 0, "no migration files found — the checks below prove nothing"
    assert types_of("digest_log"), "no `create table public.digest_log` found in any migration"


def test_sent_on_is_still_a_date_column() -> None:
    """If this ever changes, the checks below are testing the wrong thing —
    loudly, which is the point."""
    assert types_of("digest_log").get("sent_on") == "date"


#: `YYYY-MM-DD`, and nothing else.
#:
#: **Do not replace this with `date.fromisoformat`.** The first version of this
#: guard did exactly that, and Python 3.11's parser accepts ISO WEEK dates:
#: `date.fromisoformat("2026-W34")` returns 2026-08-17, while Postgres answers
#: the same string with `invalid input syntax for type date`. The check was
#: therefore MORE PERMISSIVE than the database, and passed on the very bug it
#: was written for — caught only because breaking the code deliberately showed
#: two behavioural tests failing and this one still green.
#:
#: It is the `_NullRunStore` defect in a different costume: a stand-in that
#: cannot refuse what the real thing refuses. A validator must use the same
#: parser as the thing it stands for, or a STRICTER one. Never a looser one.
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.mark.parametrize("fn", KEY_FUNCTIONS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("day", A_YEAR + AROUND_BOUNDARIES, ids=str)
def test_every_period_key_is_a_date_postgres_accepts(fn, day: date) -> None:  # type: ignore[no-untyped-def]
    key = fn(day)
    if not ISO_DATE.match(key) or date.fromisoformat(key).isoformat() != key:
        pytest.fail(
            f"{fn.__name__}({day}) = {key!r}, and digest_log.sent_on is `date`. "
            "Postgres answers anything but YYYY-MM-DD with `invalid input syntax "
            "for type date`; claim_period turns the exception into False, and "
            "False means 'already sent'. Nothing sends and the run reports a skip."
        )


def test_the_date_check_is_stricter_than_pythons() -> None:
    """The guard on the guard.

    If `ISO_DATE` is ever loosened to whatever `date.fromisoformat` takes, the
    check silently stops covering the case it exists for. This states the
    difference as an assertion so the two cannot quietly converge.
    """
    assert date.fromisoformat("2026-W34"), "python accepts ISO week dates"
    assert not ISO_DATE.match("2026-W34"), "our check must not"
    assert not ISO_DATE.match("20260823"), "nor the compact form"


def test_a_sunday_send_and_a_monday_retry_claim_the_same_week() -> None:
    """The property `weekly_key` has always CLAIMED and never had.

    The old implementation used the ISO week number. ISO weeks end on Sunday,
    so the Sunday was W34 and the Monday retry was W35 — a different key, and a
    second email. The docstring asserted the opposite for months.
    """
    sunday = date(2026, 8, 23)
    assert sunday.strftime("%A") == "Sunday"

    assert mail.weekly_key(sunday) == mail.weekly_key(sunday + timedelta(days=1))
    assert mail.weekly_key(sunday) == sunday.isoformat()


def test_a_different_week_is_a_different_key() -> None:
    """The other direction: collapsing must not collapse everything."""
    sunday = date(2026, 8, 23)
    assert mail.weekly_key(sunday) != mail.weekly_key(sunday + timedelta(days=7))
    assert len({mail.weekly_key(d) for d in A_YEAR}) == len(A_YEAR)


def test_every_day_of_a_week_maps_to_that_weeks_sunday() -> None:
    sunday = date(2026, 8, 23)
    keys = {mail.weekly_key(sunday + timedelta(days=n)) for n in range(7)}
    assert keys == {sunday.isoformat()}


def test_nothing_passes_a_period_key_that_did_not_come_from_these_functions() -> None:
    """Structural, because the value guards above only cover values we produce.

    A caller passing `period_key="2026-W34"` directly, or any other hand-rolled
    string, is the same bug arriving by a route the parametrised tests cannot
    see. Guarding the CALL SITES means a new sender inherits the check instead
    of needing to remember it.
    """
    allowed = {f.__name__ for f in KEY_FUNCTIONS}
    root = Path(inspect.getfile(mail)).parents[3]
    offenders: list[str] = []
    seen = 0

    def is_allowed(node: ast.expr, assigns: dict[str, ast.expr]) -> bool:
        """A call to a checked function, a ternary of them, or a local holding
        one. `period = daily_key(d) if kind == "daily" else weekly_key(d)` is
        the real shape in `send_emails.run`, and rejecting it would push the
        code into a worse form to satisfy the guard."""
        if isinstance(node, ast.IfExp):
            return is_allowed(node.body, assigns) and is_allowed(node.orelse, assigns)
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            return name in allowed
        if isinstance(node, ast.Name) and node.id in assigns:
            return is_allowed(assigns[node.id], assigns)
        return False

    for path in sorted((root / "src").rglob("*.py")) + sorted((root / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope in [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]:
            assigns: dict[str, ast.expr] = {
                a.targets[0].id: a.value
                for a in ast.walk(scope)
                if isinstance(a, ast.Assign)
                and len(a.targets) == 1
                and isinstance(a.targets[0], ast.Name)
            }
            for node in [n for n in ast.walk(scope) if isinstance(n, ast.Call)]:
                for kw in node.keywords:
                    if kw.arg != "period_key":
                        continue
                    seen += 1
                    if not is_allowed(kw.value, assigns):
                        offenders.append(
                            f"{path.name}:{node.lineno} period_key={ast.unparse(kw.value)}"
                        )

    assert seen, "no `period_key=` call sites found — this guard would prove nothing"
    assert offenders == [], (
        "period_key must come from a function this file checks against the DDL. "
        f"Unchecked: {offenders}"
    )
