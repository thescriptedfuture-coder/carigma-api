#!/usr/bin/env python
"""Verify a guard by breaking the code — without ever risking the working tree.

## Why this exists

"Verify by breaking" is the rule that a check nobody has watched fail is not a
check. Practising it by hand means editing tracked source and then putting it
back, and putting it back is where it went wrong: a `git checkout <file>` used
as a tidy-up discarded an entire uncommitted rewrite. **The suite stayed green,
because the file it reverted to was the old fixture the tests had been written
against.** The safety net confirmed the destruction.

So the restore must not be something anyone remembers to do correctly:

- The original is copied OUTSIDE the repo before anything is touched.
- The restore runs in `finally`, so it happens on failure, on exception, and
  on Ctrl-C.
- The restore is verified byte-for-byte, and says so if it could not be done.
- Git is never consulted. Uncommitted work is exactly what a scratch break sits
  on top of, so "restore what is committed" is the wrong question.

It also closes the other half of the same mistake: **the edit that silently did
not apply.** An anchor that does not match is an error here, not a no-op that
reports success.

## Use

    python scripts/break_check.py \\
        --file src/carigma_api/services/payments.py \\
        --old "if balance is None:" \\
        --new "if False:" \\
        --run ".venv\\Scripts\\python.exe -m pytest -q tests/test_payments_roundtrip.py"

`--run` goes through the platform shell, which on Windows is cmd.exe — so the
interpreter path needs BACKSLASHES even when you are typing in bash. The first
run of this tool got that wrong, cmd refused to launch anything, and the
non-zero exit was reported as "the guard works". Hence the baseline below.

Exit 0 means the break was applied AND the command failed — the guard works.
Exit 1 means the command still passed, which is the finding: nothing checks it.
Exit 2 means nothing could be concluded (bad anchor, or a failing baseline).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--old", required=True, help="exact text to replace; must be present")
    ap.add_argument("--new", required=True, help="what to put there instead")
    ap.add_argument("--run", required=True, help="the command that SHOULD now fail")
    ap.add_argument(
        "--expect",
        choices=("fail", "pass"),
        default="fail",
        help="what the command should do while broken (default: fail)",
    )
    args = ap.parse_args()

    target: Path = args.file
    if not target.is_file():
        print(f"break_check: no such file: {target}", file=sys.stderr)
        return 2

    original = target.read_bytes()
    source = target.read_text(encoding="utf-8")

    # A baseline, BEFORE anything is touched.
    #
    # Without it, a command that cannot even start looks exactly like a guard
    # doing its job: the first run of this tool reported "the guard works" when
    # the real cause was cmd.exe refusing the interpreter path. A non-zero exit
    # only means "the guard caught it" if zero was reachable in the first place.
    print("break_check: baseline — the command must pass on unmodified code...", flush=True)
    # S602: running an arbitrary command is the entire job of this tool, and the
    # command comes from the developer's own shell, not from any input.
    if subprocess.run(args.run, shell=True).returncode != 0:  # noqa: S602
        print(
            "\nbreak_check: THE COMMAND ALREADY FAILS ON UNMODIFIED CODE.\n"
            "            Nothing was broken and nothing can be concluded — a failure\n"
            "            after the break would prove only that it was failing before.",
            file=sys.stderr,
        )
        return 2
    print()

    count = source.count(args.old)
    if count == 0:
        # The failure this tool exists to make loud. A replacement whose anchor
        # does not match changes nothing and then reports success.
        print(f"break_check: anchor not found in {target} — nothing was changed", file=sys.stderr)
        return 2
    if count > 1:
        print(
            f"break_check: anchor matches {count} times in {target}; make it unique",
            file=sys.stderr,
        )
        return 2

    # The copy lives outside the repo, so nothing in the tree can clobber it and
    # no git operation can be mistaken for the restore.
    with tempfile.TemporaryDirectory(prefix="break_check_") as tmp:
        backup = Path(tmp) / target.name
        backup.write_bytes(original)

        broken = False
        try:
            target.write_text(source.replace(args.old, args.new), encoding="utf-8")
            broken = True
            print(f"break_check: broke {target}; running the command...\n", flush=True)
            result = subprocess.run(args.run, shell=True)  # noqa: S602
            failed = result.returncode != 0
        finally:
            restored = True
            if broken:
                target.write_bytes(backup.read_bytes())
                restored = target.read_bytes() == original
                if not restored:
                    # Never silent. A half-restored file that nobody is told
                    # about is worse than the break.
                    print(
                        f"break_check: RESTORE FAILED for {target}. "
                        f"The original is at {backup} — copy it back before doing anything else.",
                        file=sys.stderr,
                    )
                    # Hold the temp directory open long enough to be read. NOT a
                    # `return`: returning from `finally` swallows whatever
                    # exception was in flight, so a crash mid-run would be
                    # reported as an orderly exit code. Ruff caught this in the
                    # tool whose whole purpose is not losing work.
                    input("press enter once you have recovered the file...")

        if not restored:
            return 3

    print()
    wanted_failure = args.expect == "fail"
    if failed == wanted_failure:
        verb = "failed" if failed else "passed"
        print(f"break_check: the command {verb} while broken, as expected. The guard works.")
        return 0

    if wanted_failure:
        print(
            "break_check: THE COMMAND STILL PASSED WITH THE CODE BROKEN.\n"
            "            Nothing checks this behaviour.",
            file=sys.stderr,
        )
    else:
        print("break_check: the command failed, but was expected to pass.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
