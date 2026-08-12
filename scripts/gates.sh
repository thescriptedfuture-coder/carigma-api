#!/usr/bin/env bash
#
# Every gate CI runs, in one command that cannot lie about the result.
#
# This exists because a commit went in with a failing test. The gates had been
# run as:
#
#     pytest ... | tail -1 && git commit ...
#
# and a pipeline's exit status is the LAST command's — `tail` succeeded, so the
# `&&` proceeded. Nothing was wrong with the gate; the shell reported it wrong.
#
# `set -o pipefail` makes a pipeline fail if ANY stage fails, and `set -e` stops
# on the first non-zero. Together they make "the gates passed" mean it. Piping
# INSIDE this script is then safe, which matters — the habit that caused the
# problem was wanting shorter output, and a rule that fights convenience loses.
#
# What this CANNOT fix: a caller writing `bash scripts/gates.sh | tail -1 &&
# git commit`. That `&&` still reads `tail`'s status, and no amount of care in
# here changes the caller's shell. The mitigation is that there is now one
# command to run and it prints an unmistakable final line, so there is no
# reason to pipe it. Run it bare.
#
# Usage:
#     bash scripts/gates.sh          all gates
#     bash scripts/gates.sh fast     skip the slow ones
#
set -euo pipefail

cd "$(dirname "$0")/.."

PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY=".venv/bin/python"
[ -x "$PY" ] || PY="python"

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

step "ruff check"
"$PY" -m ruff check .

step "ruff format --check"
"$PY" -m ruff format --check .

step "mypy"
"$PY" -m mypy src

step "pytest"
"$PY" -m pytest -q

# The live probes are NOT part of the default gates: they hit the real database
# and need credentials CI does not have. Run them deliberately, and read them.
if [ "${1:-}" = "live" ]; then
    step "verify_rls (anonymous boundary)"
    "$PY" scripts/verify_rls.py

    step "verify_rls_crossuser (authenticated boundary)"
    "$PY" scripts/verify_rls_crossuser.py
fi

printf '\n\033[32mAll gates passed.\033[0m\n'
