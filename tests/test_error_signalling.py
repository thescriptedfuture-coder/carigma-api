"""A swallowed exception must not return the same value as a legitimate answer.

## The bug this generalises

`claim_period` inserts into `digest_log` and reads a unique-index violation as
"already claimed":

    except Exception as exc:
        logger.info("period already claimed ...")
        return False

The comment is right about the case it was written for and wrong about every
other one. `weekly_key` produced `"2026-W34"` for a `date` column, Postgres
refused it, and **"the value was the wrong type" came back as the same `False`
as "somebody already sent this"** — which `send()` reports as DUPLICATE, which
reads in the run summary as an ordinary quiet day. No weekly review has ever
been sent.

`agent_runs` failed the same way one layer down: a rejected insert became a
logged warning and a 200.

## Why this is an inventory rather than a rule

Finding the collisions is mechanical — an `except` returning `False`, `None`,
`0`, or `[]` from a function whose ordinary path returns the same value. **The
verdict is not.** Most of the thirteen below are timestamp parsers where a
malformed value and a missing value genuinely ARE the same answer, and forcing
them apart would add a distinction nobody could act on.

So the sweep runs, and every collision must be listed here with a reason. A new
one fails until someone writes the reason down. That puts the judgement at the
moment the code is written, which is the only moment anyone has the context to
make it.

One entry below is marked DECIDE. It is not a parser, and its two answers are
not the same answer.

There were two. `read_balance` was the other, and it was FIXED rather than
accepted: a failed read now raises `CreditsUnavailable` instead of returning
the same `None` as "this user has no credits row" — which `check_affordable`
read as "credits are not configured, run free". A transient Supabase failure
therefore made every run free, on the money path. **Inferring a dev condition
from a production failure was the actual bug.**
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: (file, function) -> why an exception may return what a real answer returns.
ACCEPTED: dict[tuple[str, str], str] = {
    # ── Timestamp parsers. A malformed string and a missing string are the
    # same answer to the only question the caller asks: "do I have a usable
    # time?" Splitting them would produce a distinction with no action behind
    # it, which is its own failure mode.
    ("admin.py", "_parse"): "unusable timestamp; caller has no other branch",
    ("providers.py", "_parse_ts"): "unusable timestamp from a third party",
    ("market.py", "_parse"): "unusable timestamp",
    ("reengagement.py", "_parse"): "unusable timestamp",
    ("run_store.py", "_parse"): "unusable timestamp",
    ("today.py", "_parse"): "unusable timestamp",
    ("trigger_facts.py", "_parse"): "unusable timestamp",
    # ── Reasoned, and the reasoning is in the function.
    ("feed.py", "_is_new"): "an unreadable date must not be allowed to CLAIM novelty",
    ("thread_store.py", "is_suppressed"): "errs toward showing the user their own item back",
    ("runs.py", "stream"): "ends the stream; there is no other terminal value",
    ("unsubscribe.py", "resolve"): "a corrupt token and an invalid token are both 'bad link'",
    # ── DECIDE. Listed so they are visible, not because they are settled.
    # `read_balance` was here, marked DECIDE, and is now FIXED rather than
    # accepted: it raises `CreditsUnavailable` on a read it could not complete,
    # so a failed read and an absent row are no longer the same answer. The
    # entry is gone rather than reworded, and the stale-entry half of the
    # assertion below is what would have caught it if it were not.
    ("credits.py", "grant"): (
        "None means 'the grant did not happen', and a read that threw is PROOF "
        "apply_delta was never reached. The payments path relies on exactly that "
        "to release a payment for retry — only a grant we can prove did not happen "
        "is safe to retry automatically. Contrast check_affordable, where the same "
        "exception must NOT be swallowed: 'we could not find out' is not an answer "
        "to 'can this person afford it'."
    ),
    ("onboarding.py", "_activate_referral"): (
        "DECIDE: None means 'no referral to activate'. A failed activation returns the "
        "same, so a referral that silently failed to grant is indistinguishable from a "
        "user who was never referred, and there is no retry path."
    ),
}


def _empty_literal(node: ast.expr | None) -> str | None:
    """A comparable name for a returned constant or empty container."""
    if node is None:
        return "None"
    if isinstance(node, ast.Constant) and not isinstance(node.value, str):
        if node.value in (False, None, 0):
            return repr(node.value)
    if isinstance(node, ast.List) and not node.elts:
        return "[]"
    if isinstance(node, ast.Dict) and not node.keys:
        return "{}"
    if isinstance(node, ast.Tuple) and not node.elts:
        return "()"
    return None


def collisions() -> dict[tuple[str, str], list[str]]:
    """`(file, function) -> the values returned from BOTH an except and the
    ordinary path`."""
    out: dict[tuple[str, str], list[str]] = {}
    files = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]:
            # A procedure returning nothing cannot signal anything by its
            # return value, so a bare `return` in a handler says nothing.
            if fn.returns is not None and ast.unparse(fn.returns) == "None":
                continue

            handler_ids = {
                id(node)
                for block in ast.walk(fn)
                if isinstance(block, ast.Try)
                for handler in block.handlers
                for node in ast.walk(handler)
            }
            from_handler: set[str] = set()
            from_body: set[str] = set()
            for ret in [n for n in ast.walk(fn) if isinstance(n, ast.Return)]:
                value = _empty_literal(ret.value)
                if value is None:
                    continue
                (from_handler if id(ret) in handler_ids else from_body).add(value)

            shared = sorted(from_handler & from_body)
            if shared:
                out[(path.name, fn.name)] = shared
    return out


FOUND = collisions()


def test_the_sweep_can_still_see_the_code() -> None:
    """The precondition. An AST walk pointed at the wrong directory finds no
    collisions and reports the same green as a codebase with none."""
    assert FOUND, "no collisions found at all — the sweep is not reading the source"
    assert (ROOT / "src").is_dir() and (ROOT / "scripts").is_dir()


def test_every_swallowed_exception_that_mimics_an_answer_has_a_reason() -> None:
    """A new collision fails here until someone writes down why the two
    answers are the same answer.

    Both directions: a stale entry is also a failure, because an inventory that
    describes code which no longer exists stops describing the code that does.
    """
    unlisted = sorted(FOUND.keys() - ACCEPTED.keys())
    stale = sorted(ACCEPTED.keys() - FOUND.keys())

    assert unlisted == [], (
        "an exception here returns the same value as a legitimate answer, so a failure "
        f"is indistinguishable from an ordinary outcome: {unlisted}. Either raise a "
        "distinct outcome, or add it to ACCEPTED with the reason the two really are "
        "the same."
    )
    assert stale == [], f"ACCEPTED names functions that no longer collide: {stale}"


def test_the_two_undecided_ones_are_still_marked() -> None:
    """`read_balance` and `_activate_referral` are listed, not settled.

    This exists so "it is in the inventory" cannot quietly become "it was
    reviewed". When either is decided, the DECIDE prefix goes and this test
    fails, which is the prompt to update the entry rather than forget it.
    """
    undecided = sorted(k for k, why in ACCEPTED.items() if why.startswith("DECIDE:"))
    assert undecided == [
        ("onboarding.py", "_activate_referral"),
    ], (
        "`read_balance` was the other one and is now fixed rather than accepted — it "
        "raises CreditsUnavailable, so a failed read and an absent row are different "
        "answers. If a DECIDE reappears here, decide it."
    )
