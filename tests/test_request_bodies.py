"""Every field the API requires must be a key the web actually sends.

## The defect this exists for

`POST /weekly/contract/re-entry` answered **422 `cadence: Field required`** for
as long as it had existed, and the surface offering it reported "we couldn't
plan a week just now" — a failure of the attempt, which is not what happened.

The route declares `body: CadenceRequest | None = None`. That accepts no body
at all; it does not make the body's fields optional, so the moment a body IS
present `cadence` is required again. The web sends `{}`, because the button has
nothing to choose. **`None` and `{}` are different bodies.** `= Field(...)` with
no `default=` reads as a default and is not one, which is how it survived
review.

## Why nothing else could have caught it

| Suite | Calls | Sees |
|---|---|---|
| this repo's route tests | the route, the way the route wants | nothing about the client |
| the web's e2e | MSW | nothing about validation |
| `run smoke` | 18 reads, one upload, by design | no other write |

Every one of them is one-sided. The only check that can see this compares the
two repositories: the keys the client sends against the fields the server
requires. That is what this is.

## Keys, not values

A recorded value would have to be synthesised, and a synthesised value fails
type validation for reasons unrelated to the defect — a guard that fires on
correct code gets deleted. A required field missing from the client's keys is a
guaranteed 422 regardless of types, so the set comparison is the part worth
asserting.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, get_args, get_origin

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "request_bodies.json"
SNAPSHOT = ROOT / "scripts" / "snapshot_request_bodies.py"

#: Calls whose body this scanner cannot read statically — a variable, a spread,
#: a call. They are RECORDED rather than skipped, so they appear as something no
#: guard covers instead of vanishing. A number that may only fall.
UNREADABLE_BODIES = 3
# 5 -> 3, and three calls the guard had never seen became visible with them:
# `/weekly/contract/approve`, `/onboarding/complete` and `/tracker`. The scanner
# was choking on the TRAILING COMMA Prettier writes on every multi-line literal,
# so this number was counting the scanner's own defect as the codebase's.


def _recorded() -> list[dict[str, Any]]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _shape(path: str) -> str:
    """`/posts/week/{day}/skip` → `/posts/week/*/skip`, for matching either side."""
    out: list[str] = []
    for segment in path.split("/"):
        out.append("*" if segment.startswith("{") and segment.endswith("}") else segment)
    return "/".join(out)


def _body_model(dependant: Any) -> tuple[type[BaseModel] | None, bool]:
    """The route's request model, and whether the body itself is optional."""
    for field in dependant.body_params:
        annotation = field.field_info.annotation
        optional = False
        if get_origin(annotation) is not None:
            args = [a for a in get_args(annotation) if a is not type(None)]
            optional = len(args) != len(get_args(annotation))
            annotation = args[0] if args else annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation, optional
    return None, False


def _routes(app: FastAPI) -> dict[tuple[str, str], Any]:
    """Every mounted route's `(method, shape) -> dependant`.

    `app.routes` does NOT hold the endpoints in this FastAPI version: an
    included router is wrapped, and a walk looking only for `APIRoute` found
    ZERO routes — which would have made every assertion below pass on an empty
    dict. Found by the guard failing on all sixteen calls at once, which is the
    shape of a broken scan rather than sixteen real defects.
    """
    found: dict[tuple[str, str], Any] = {}

    def take(path: str, methods: Any, dependant: Any) -> None:
        for method in methods or ():
            found[(method, _shape(path))] = dependant

    for route in app.routes:
        if isinstance(route, APIRoute):
            take(route.path, route.methods, route.dependant)
        elif hasattr(route, "effective_candidates"):
            for candidate in route.effective_candidates():
                take(candidate.path, candidate.methods, candidate.dependant)
    assert found, "no routes found — the walk is looking in the wrong place"
    return found


def test_the_manifest_was_read(client: Any) -> None:
    """A truncated manifest would make every assertion below pass on nothing."""
    recorded = _recorded()
    assert len(recorded) >= 10, f"request_bodies.json holds only {len(recorded)} entries"
    assert any(e["keys"] is None for e in recorded), (
        "no call records `keys: null`. That is the 'sends no body at all' case, "
        "which is a different fact from `{}` and the distinction is the point."
    )


def test_every_required_field_is_a_key_the_web_sends(client: Any) -> None:
    routes = _routes(client.app)
    missing: list[str] = []
    unresolved: list[str] = []

    for entry in _recorded():
        dependant = routes.get((entry["method"], _shape(entry["path"])))
        if dependant is None:
            unresolved.append(f"{entry['method']} {entry['path']} ({entry['where']})")
            continue
        model, body_optional = _body_model(dependant)
        if model is None:
            continue
        required = {name for name, field in model.model_fields.items() if field.is_required()}
        if not required:
            continue

        sent = entry["keys"]
        if sent == "unreadable":
            continue
        if sent is None:
            # No body argument at all. Legal only if the body is optional AND
            # nothing inside it is required — which is exactly the trap.
            if not body_optional or required:
                missing.append(
                    f"{entry['method']} {entry['path']} sends no body, and "
                    f"{model.__name__} requires {sorted(required)} ({entry['where']})"
                )
            continue
        absent = required - set(sent)
        if absent:
            missing.append(
                f"{entry['method']} {entry['path']} sends {sorted(sent)}, and "
                f"{model.__name__} requires {sorted(absent)} ({entry['where']})"
            )

    assert not unresolved, "the web calls these and no route matches:\n  " + "\n  ".join(unresolved)
    assert not missing, (
        "every one of these answers 422 to the call the web actually makes:\n  "
        + "\n  ".join(missing)
        + "\n\nAn OPTIONAL BODY IS NOT AN OPTIONAL FIELD: `Model | None = None` "
        "accepts no body, and validates every field the moment a body arrives."
    )


def test_the_web_sends_no_key_the_model_does_not_declare(client: Any) -> None:
    """A key the server ignores is a belief the client holds and the server does not.

    Pydantic drops unknown keys silently, so this never shows up as an error —
    the request succeeds and the value is thrown away. That is how a field gets
    "sent" for a month without arriving.
    """
    routes = _routes(client.app)
    stray: list[str] = []
    for entry in _recorded():
        dependant = routes.get((entry["method"], _shape(entry["path"])))
        if dependant is None or not isinstance(entry["keys"], list):
            continue
        model, _ = _body_model(dependant)
        if model is None:
            continue
        extra = set(entry["keys"]) - set(model.model_fields)
        if extra:
            stray.append(
                f"{entry['method']} {entry['path']} sends {sorted(extra)}, which "
                f"{model.__name__} does not declare ({entry['where']})"
            )
    assert not stray, "these keys are discarded on arrival, silently:\n  " + "\n  ".join(stray)


def test_unreadable_bodies_are_counted_not_ignored() -> None:
    count = sum(1 for e in _recorded() if e["keys"] == "unreadable")
    assert count <= UNREADABLE_BODIES, (
        f"{count} bodies cannot be read statically, up from {UNREADABLE_BODIES}. "
        "Each is a call no guard covers. Write the literal at the call site, or "
        "raise this number deliberately."
    )


@pytest.mark.skipif(
    not Path(r"D:\LinkedIn OS\Carigma\v2\carigma-web").is_dir(),
    reason="the sibling web repo is not here (CI clones one repo)",
)
def test_the_manifest_is_not_stale() -> None:
    """Re-derive it where both repos exist, which is where commits happen.

    CI has one repository and must trust the committed file. The pre-commit hook
    has both, so a mutation added to the web without re-recording fails there —
    before it can reach CI as a green tick over a stale manifest.

    The first version asked `git diff` whether the file had changed. The file
    was untracked, `git diff` is silent about untracked files, and the check
    passed on a manifest deliberately made stale. `--check` compares the text
    instead, which does not care whether the file is tracked.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(SNAPSHOT), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
