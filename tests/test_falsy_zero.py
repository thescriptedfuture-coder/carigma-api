"""`x or default` on a number is a bug three times over.

    jobs_fetches_today() or 0     -> "cannot count" became "counted zero",
                                     which GRANTS permission to call a paid API
    balance or 0                  -> a user with credits told they have none
    cadence_per_week or 2         -> a PAUSED week promoted back to two posts

Every one was written by someone who knew the rule. The third was written into
a file I had created an hour earlier, in the same session as fixing the second.
That is the argument for a guard rather than a reminder.

## What it flags

`A or B` where the result is numeric, which is where zero is a real answer:

- the whole expression coerced — `int(x or y)`, `float(x or y)`
- either side a numeric literal — `x or 0`, `x or 2`

## What it cannot see

A helper returning a number, then `helper() or 0` two files away with no
coercion and no literal. Static analysis stops at the expression; this is
narrow on purpose, because a guard that fires on strings and lists would be
turned off within a week.

`# falsy-ok:` on the line marks a deliberate case — where the fallback and the
falsy value are genuinely the same answer.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "carigma_api"

NUMERIC_COERCIONS = {"int", "float", "round", "sum", "abs"}


def _is_numeric_literal(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    )


def suspect_expressions() -> list[tuple[str, int, str]]:
    """(file, line, source) for every numeric `or` fallback we can see."""
    found: list[tuple[str, int, str]] = []

    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        tree = ast.parse(text)

        # `int(...)` wrapping an `or`, and bare `or` with a numeric literal.
        for node in ast.walk(tree):
            hits: list[ast.BoolOp] = []
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in NUMERIC_COERCIONS
            ):
                hits += [
                    a for a in node.args if isinstance(a, ast.BoolOp) and isinstance(a.op, ast.Or)
                ]
            elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                if any(_is_numeric_literal(v) for v in node.values):
                    hits.append(node)

            for hit in hits:
                line = hit.lineno
                # The marker may sit on the expression's line or the line the
                # statement started on, because formatters wrap long calls.
                # Look BOTH ways. `ruff format` splits a long call across
                # lines and parks a trailing comment on the CLOSING paren, so
                # a marker written beside the expression ends up two lines
                # below the `lineno` the AST reports. A window that only
                # looked backwards reported nineteen correctly-marked sites.
                window = lines[max(0, line - 3) : line + 2]
                if any("falsy-ok:" in t for t in window):
                    continue
                # Deduplicated: `ast.walk` reaches a nested BoolOp both
                # through its wrapping Call and on its own, so an unguarded
                # append reports every coerced expression twice.
                entry = (str(path.relative_to(SRC.parent.parent)), line, ast.unparse(hit)[:70])
                if entry not in found:
                    found.append(entry)
        del text
    return found


def test_the_scan_finds_expressions_at_all() -> None:
    """Assert the input. A walk matching nothing would report a clean codebase,
    which is how a guard passes forever while checking zero things."""
    tree = ast.parse("x = int(a.get('k') or 3)\ny = b or 0\nz = c or 'text'")
    ors = [n for n in ast.walk(tree) if isinstance(n, ast.BoolOp)]

    assert len(ors) == 3, "the fixture itself is wrong"
    assert sum(1 for n in ors if any(_is_numeric_literal(v) for v in n.values)) == 2


def test_a_string_fallback_is_not_flagged() -> None:
    """`name or "Anonymous"` is fine — an empty string and a missing name are
    the same answer. Firing on those would get this deleted."""
    tree = ast.parse('x = name or "Anonymous"')
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.BoolOp))

    assert not any(_is_numeric_literal(v) for v in node.values)


def test_no_numeric_or_fallback_survives_unmarked() -> None:
    """The guard itself.

    Where a numeric `or` is genuinely correct, mark it `# falsy-ok:` with the
    reason — the fallback and the falsy value have to be the same answer, and
    saying so out loud is the whole point.
    """
    offenders = suspect_expressions()

    assert not offenders, "numeric `or` fallbacks — 0 is a real value:\n  " + "\n  ".join(
        f"{path}:{line}  {src}" for path, line, src in offenders
    )
