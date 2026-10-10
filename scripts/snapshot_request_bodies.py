"""Record every request body the WEB sends, so the API can be checked against it.

    run python scripts/snapshot_request_bodies.py

## The defect this exists for

`POST /weekly/contract/re-entry` answered **422 `cadence: Field required`** for
as long as it had existed. The route declares `body: CadenceRequest | None =
None`, so no body at all is accepted — and the instant a body IS present, every
field in it is required again. The web sends `{}`, because the button has
nothing to choose. `None` and `{}` are different bodies.

Nothing could have seen it from one side. The API's own tests call the route the
way the route wants to be called. The web's tests call MSW, which validates
nothing. The smoke test reads 18 surfaces and makes exactly one write, by
design. So the only check that could ever have caught this is one that compares
the two repositories: **the keys the web actually sends against the fields the
API actually requires.**

## What is recorded, and why only keys

Keys, not values. A recorded value would have to be synthesised, and a
synthesised value fails type validation for reasons that have nothing to do
with the defect — a guard that fires on correct code gets deleted. The
assertion that matters is a set comparison: every required field of the model
must be a key the client sends.

`keys: null` means the call passes no body argument at all, which is a
different fact from `keys: []` (an empty object literal) and the distinction is
the whole point.

A body this scanner cannot read statically — a variable, a spread, a function
call — is recorded as `"unreadable"` rather than skipped, so it shows up in the
manifest as something no guard covers instead of vanishing from it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

WEB = Path(r"D:\LinkedIn OS\Carigma\v2\carigma-web")
OUT = Path(__file__).resolve().parents[1] / "request_bodies.json"

#: Below this, assume the scan broke rather than that the web stopped mutating.
MINIMUM = 5

CALL = re.compile(r"\bapi\s*\.\s*(post|patch|put|delete)\b")


def _skip_generic(src: str, i: int) -> int:
    """Step over `<{ contract: Contract }>` between the method and its `(`."""
    while i < len(src) and src[i].isspace():
        i += 1
    if i >= len(src) or src[i] != "<":
        return i
    depth = 0
    while i < len(src):
        if src[i] == "<":
            depth += 1
        elif src[i] == ">":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def _arguments(src: str, open_paren: int) -> list[str] | None:
    """The call's arguments, split at depth zero. Strings and templates are opaque."""
    depth = 0
    quote: str | None = None
    args: list[str] = []
    start = open_paren + 1
    i = open_paren
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            elif quote == "`" and src.startswith("${", i):
                # A template hole can hold anything, including quotes.
                depth += 1
                i += 2
                continue
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0 and ch == ")":
                args.append(src[start:i])
                return [a.strip() for a in args if a.strip()]
        elif ch == "," and depth == 1:
            args.append(src[start:i])
            start = i + 1
        i += 1
    return None


def _path_of(argument: str) -> str | None:
    """`` `/posts/week/${day}/skip` `` → `/posts/week/{day}/skip`."""
    text = argument.strip()
    if not (text[:1] in "\"'`" and text[-1:] == text[:1]):
        return None
    inner = text[1:-1]
    inner = re.sub(r"\$\{[^}]*\}", "{x}", inner)
    if not inner.startswith("/"):
        return None
    return inner


def _keys_of(argument: str) -> list[str] | str:
    """The top-level keys of an object literal, or `"unreadable"`."""
    text = argument.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return "unreadable"
    body = text[1:-1].strip()
    if not body:
        return []
    keys: list[str] = []
    depth = 0
    quote: str | None = None
    piece = ""
    for ch in body + ",":
        if quote:
            piece += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'`":
            quote = ch
            piece += ch
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            name = piece.split(":", 1)[0].strip()
            if name.startswith("..."):
                return "unreadable"
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
                return "unreadable"
            keys.append(name)
            piece = ""
            continue
        piece += ch
    return keys


def mutations() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    src_root = WEB / "src"
    for file in sorted(src_root.rglob("*.ts")) + sorted(src_root.rglob("*.tsx")):
        if ".test." in file.name or "mocks" in file.parts:
            continue
        source = file.read_text(encoding="utf-8")
        for match in CALL.finditer(source):
            i = _skip_generic(source, match.end())
            if i >= len(source) or source[i] != "(":
                continue
            args = _arguments(source, i)
            if not args:
                continue
            path = _path_of(args[0])
            if path is None:
                continue
            keys: list[str] | str | None = _keys_of(args[1]) if len(args) > 1 else None
            found.append(
                {
                    "method": match.group(1).upper(),
                    "path": path,
                    "keys": keys,
                    # The FILE, not the line. The first version recorded
                    # `path.tsx:127`, so adding an import anywhere above a
                    # mutation made the manifest "stale" without a single
                    # request changing — a guard that cries wolf is a guard
                    # that gets its number raised until it says nothing. The
                    # method and path below locate the call precisely enough.
                    "where": file.relative_to(WEB).as_posix(),
                }
            )
    return found


def main(argv: list[str] | None = None) -> int:
    """`--check` reports whether the manifest on disk is current, and writes nothing.

    The first version of the staleness test shelled out to `git diff` after
    rewriting the file. `request_bodies.json` was new and therefore UNTRACKED,
    and `git diff` says nothing about an untracked file — so the check passed
    on a manifest deliberately made stale. Comparing the text to what the scan
    produces needs no VCS and cannot be fooled by a file's tracked state.
    """
    check = "--check" in (argv if argv is not None else sys.argv[1:])
    if not WEB.is_dir():
        print(f"carigma-web is not at {WEB}", file=sys.stderr)
        return 2
    found = mutations()
    if len(found) < MINIMUM:
        print(
            f"only {len(found)} mutation(s) found, expected at least {MINIMUM}.\n"
            "A half-read manifest would make the guard pass on everything, so "
            "nothing was written.",
            file=sys.stderr,
        )
        return 1
    rendered = json.dumps(found, indent=2) + "\n"
    if check:
        on_disk = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if on_disk != rendered:
            print(
                f"{OUT.name} is out of date: the web makes a request it does not "
                "record. Re-run scripts/snapshot_request_bodies.py and commit it.",
                file=sys.stderr,
            )
            return 1
        print(f"{OUT.name} is current ({len(found)} mutations)")
        return 0
    OUT.write_text(rendered, encoding="utf-8")
    print(f"{OUT.name}: {len(found)} mutation(s)")
    for entry in found:
        keys = entry["keys"]
        shown = "no body" if keys is None else keys if isinstance(keys, str) else sorted(keys)
        print(f"  {entry['method']:6} {entry['path']:42} {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
