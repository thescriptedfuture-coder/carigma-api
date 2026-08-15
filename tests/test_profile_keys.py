"""Every key read from a profile must be a key the repository can produce.

## The bug this exists for

`routes/naukri.py` read `profile.get("linkedin_headline")` — the DATABASE
column name — from a dict `db_to_profile` fills with `linkedinHeadline`. Every
user with a headline was told they had none, and offered a fix proposing to
write the headline they already had.

Nothing could have caught it downstream. `dict.get` returns `None` for a
missing key, and `None` is exactly what an empty field returns, so the wrong
key and the honest answer are indistinguishable at every layer after the read.
The guards this codebase already had — no fabricated scores, no charge on
failure, every claim carries a receipt — all passed, because the value was
faithfully derived from data that was faithfully reported. It just was not
this user's data.

## Why this is a static check and not a fixture

A fixture proves one key works. This proves every key EXISTS, including the
ones no test exercises, and it fails at the moment the typo is written rather
than the moment someone opens that screen with a populated profile.

The rule generalises past this codebase: **whenever a boundary renames things,
the rename is the invariant, and both sides of it should be checked against one
declaration rather than against each other's memory.**
"""

from __future__ import annotations

import ast
import pathlib

from carigma_api.services.naukri import FILTER_FIELDS
from carigma_api.services.repository import _PROFILE_COLUMNS

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "carigma_api"

#: The JSON shape `db_to_profile` hands back. This is the ONLY legal vocabulary
#: for reading a loaded profile.
LEGAL = set(_PROFILE_COLUMNS)

#: Keys that live inside a profile but are not columns — nested payloads and
#: the like. Each must say why, so this cannot quietly become a bypass.
NESTED_OK = {
    # `preferences` is a jsonb blob; its inner keys are not profile columns.
    "email",
    "digest",
    "weekly",
}


def _profile_names(tree: ast.AST) -> set[str]:
    """Names that hold a REPOSITORY-shaped profile, derived not guessed.

    Two sources, both structural:

    - anything bound from a `.load(...)` call — `ProfileRepository.load` is the
      only thing that returns this shape;
    - any parameter literally named `profile`, which is how the scorers receive
      one.

    Guessing by name alone was the first version, and it flagged five raw
    database rows in `admin.py` — `for p in _rows(db, "profiles", ...)` reads
    COLUMNS, so `p["user_id"]` is correct there and the guard was wrong. A
    check that fires on correct code gets deleted.
    """
    names = {"profile"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            # `profiles.load(user.id)`, and `... or {}` around it.
            if isinstance(func, ast.Attribute) and func.attr == "load":
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.BoolOp):
            for value in node.value.values:
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "load"
                ):
                    names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.FunctionDef):
            names.update(a.arg for a in node.args.args if a.arg == "profile")
    return names


def _profile_reads(tree: ast.AST) -> set[tuple[str, int]]:
    """Every literal key read from a profile-shaped name.

    Both spellings a violation could use: `profile.get("x")` and `profile["x"]`.
    Asserting an absence means reasoning about every form the forbidden thing
    could take, and only a parse can do that.
    """
    targets = _profile_names(tree)
    found: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        target = None
        key = None

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            target, key = node.func.value, node.args[0].value
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            target, key = node.value, node.slice.value

        if key is None or not isinstance(target, ast.Name):
            continue
        if target.id not in targets:
            continue
        found.add((key, node.lineno))
    return found


def test_every_profile_key_read_in_the_source_is_one_the_repository_produces() -> None:
    offenders: list[str] = []

    for path in sorted(SRC.rglob("*.py")):
        # `repository.py` is where the mapping is DEFINED — it reads database
        # rows, not the translated dict, so its vocabulary is the other one.
        if path.name == "repository.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for key, line in sorted(_profile_reads(tree)):
            if key in LEGAL or key in NESTED_OK:
                continue
            rel = path.relative_to(SRC.parent.parent)
            snake = key.replace("_", "")
            near = [k for k in LEGAL if k.lower() == snake.lower()]
            hint = f" — did you mean {near[0]!r}?" if near else ""
            offenders.append(f"{rel}:{line} reads profile[{key!r}]{hint}")

    assert not offenders, "profile keys that db_to_profile never produces:\n  " + "\n  ".join(
        offenders
    )


def test_the_guard_catches_the_bug_it_was_written_for() -> None:
    """Verify by breaking. A guard that has never failed has never been tested.

    This is the exact expression that shipped, and the exact reason it was
    invisible: `linkedin_headline` is the column, `linkedinHeadline` is the key.
    """
    tree = ast.parse('score_headline(str(profile.get("linkedin_headline") or ""))')
    keys = {key for key, _ in _profile_reads(tree)}

    assert keys == {"linkedin_headline"}
    assert "linkedin_headline" not in LEGAL
    assert "linkedinHeadline" in LEGAL


def test_the_guard_does_not_fire_on_raw_database_rows() -> None:
    """Assert the precondition the other way round.

    `admin.py` selects columns straight out of `profiles`, so `p["user_id"]`
    and `p["created_at"]` are the CORRECT vocabulary there — they are column
    names, and `db_to_profile` never ran. The first version of this guard
    flagged all five.
    """
    tree = ast.parse(
        """
profiles = _rows(db, "profiles", "user_id,name")
rows = [{"user_id": p["user_id"], "at": p.get("created_at")} for p in profiles]
"""
    )

    assert _profile_reads(tree) == set()


def test_the_guard_sees_subscripts_too_not_only_get() -> None:
    """`profile.get(...)` was the spelling that shipped. It is not the only one
    available, and a guard that only knows the spelling it was written for is
    the recurring mistake in this codebase."""
    tree = ast.parse('x = profile["linkedin_headline"]')

    assert {key for key, _ in _profile_reads(tree)} == {"linkedin_headline"}


def test_a_loaded_profile_is_tracked_under_any_name() -> None:
    """The bug would have escaped a name-based guard the moment someone wrote
    `me = profiles.load(...)`."""
    tree = ast.parse(
        """
me = profiles.load(user.id) or {}
x = me.get("linkedin_headline")
"""
    )

    assert {key for key, _ in _profile_reads(tree)} == {"linkedin_headline"}


def test_the_guard_does_not_fire_on_a_correct_read() -> None:
    """Assert the precondition: a check that fires on everything proves
    nothing."""
    tree = ast.parse('profile.get("linkedinHeadline")')
    keys = {key for key, _ in _profile_reads(tree)}

    assert keys <= LEGAL


def test_dynamic_field_lists_are_checked_too() -> None:
    """`score_filter_fields` reads `profile.get(f)` over a tuple — a variable
    key, which the AST walk cannot resolve. Rather than let the green tick
    imply coverage, the list itself is checked directly."""
    for field in FILTER_FIELDS:
        assert field in LEGAL, f"FILTER_FIELDS names {field!r}, which is not a profile key"
