"""Record the key paths the API actually emits, so the web can be checked.

## The boundary this exists for

The web declares TypeScript interfaces for every payload it reads. Those
interfaces and the MSW fixtures beside them **agreed with each other and both
disagreed with the server** — `naukriScore` where the API sends `score`, `max`
where it sends `weight`, `feedback` where it sends `receipt`. Every web test
passed. Every API test passed. The screen rendered beautifully and described a
payload that has never existed.

A check of types against fixtures would not have caught it: they were wrong
together. The only declaration that settles it is **what the server actually
sends**, so that is what this records.

## How

Every response body produced through the `client` fixture is walked and its
key paths collected, per `METHOD /path`. Nothing is written by hand — the
manifest is a transcript of the real test suite hitting real routes, and it
grows automatically as endpoints gain coverage.

An endpoint the web believes in but no API test exercises has NO entry, and the
web-side check fails loudly rather than passing over the gap. That is the right
answer: a shape nobody server-side verifies is a shape nobody knows.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "contract_keys.json"

#: Set once from the app, so a recorded path resolves to its ROUTE, not to
#: whatever id the test happened to use. `/posts/week/THU` and `/posts/week/MON`
#: are one endpoint, and matching on the literal made them two.
ROUTES: list[tuple[Any, str]] = []

#: METHOD /path -> sorted key paths. Filled during the run.
RECORDED: dict[str, set[str]] = {}


def use_routes(app: Any) -> None:
    """Teach the recorder the app's route table.

    Recursive, because `app.routes` holds the router wrappers `include_router`
    creates, not routes. Iterating one level found three entries — the docs
    endpoints — and every real path fell through to its literal self, which is
    the failure this whole exercise is about: a lookup that quietly matches
    nothing looks exactly like a lookup that matches everything correctly.
    """
    ROUTES.clear()

    def walk(routes: Any) -> None:
        for route in routes:
            regex = getattr(route, "path_regex", None)
            fmt = getattr(route, "path_format", None)
            if regex is not None and fmt:
                ROUTES.append((regex, fmt))
            # `include_router` wraps each router in an `_IncludedRouter` that
            # exposes neither `.routes` nor `.path_format` — the real routes
            # hang off `.original_router`. Walking only `.routes` found three
            # entries (the docs endpoints) and silently matched nothing else,
            # which looks identical to matching everything correctly.
            nested = getattr(route, "routes", None) or getattr(
                getattr(route, "original_router", None), "routes", None
            )
            if nested:
                walk(nested)

    walk(app.routes)


def normalise(path: str) -> str:
    """A concrete path -> its route template, with parameter names dropped.

    `{day}` becomes `{}` because the web spells its parameters differently
    (`/posts/week/${day}` vs `${slot}`), and a boundary check that depended on
    two sides choosing the same variable name would fail on correct code.
    """
    for regex, fmt in ROUTES:
        if regex.match(path):
            return re.sub(r"\{[^}]+\}", "{}", fmt)
    return path


def key_paths(value: Any, prefix: str = "") -> set[str]:
    """Every readable path in a payload, including through lists.

    `dimensions[].unlock_action.route` is a path the web reads and a path a
    rename can break, so it is recorded as precisely as it is consumed. A
    top-level-keys-only manifest would have missed every field in the Naukri
    dimension list, which is where the drift actually was.
    """
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            here = f"{prefix}.{key}" if prefix else key
            found.add(here)
            found |= key_paths(child, here)
    elif isinstance(value, list):
        for item in value:
            found |= key_paths(item, f"{prefix}[]")
    return found


def record(method: str, path: str, status: int, body: Any) -> None:
    # Error bodies carry `detail`, not the success shape. Recording them would
    # put a 402's keys into the manifest for the endpoint's happy path.
    if status >= 400 or not isinstance(body, dict | list):
        return
    RECORDED.setdefault(f"{method.upper()} {normalise(path)}", set()).update(key_paths(body))


def as_manifest() -> dict[str, list[str]]:
    return {endpoint: sorted(paths) for endpoint, paths in sorted(RECORDED.items())}


def load_manifest() -> dict[str, list[str]]:
    if not MANIFEST.exists():
        return {}
    return dict(json.loads(MANIFEST.read_text(encoding="utf-8")))


def write_manifest() -> None:
    MANIFEST.write_text(json.dumps(as_manifest(), indent=2) + "\n", encoding="utf-8")
