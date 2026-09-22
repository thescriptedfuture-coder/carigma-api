"""The two entry points must offer the same commands.

`run` is bash; `run.cmd` is for cmd.exe, which is the shell this project is
actually driven from. Commands were handed over as `./run ...` for weeks and
none of them could run — correct instructions, unrunnable, and the person
typing them assumed the fault was theirs.

Two dispatchers are two declarations of one thing, which is the drift this
codebase keeps finding (the contract and its mocks, V1's DDL and the live
database). So they are compared here, and every command has to reach a file
that exists.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = ROOT / "run"
WINDOWS = ROOT / "run.cmd"


def bash_commands() -> set[str]:
    """The labels of the `case` arms, minus the catch-alls."""
    body = BASH.read_text(encoding="utf-8")
    block = body[body.index('case "$cmd" in') : body.index("esac")]
    found = set(re.findall(r"^\s{2}([a-z]+)\)", block, re.M))
    return found


def windows_commands() -> set[str]:
    body = WINDOWS.read_text(encoding="utf-8")
    return set(re.findall(r'if /i "%CMD%"=="([a-z]+)"', body))


def scripts_named() -> dict[str, set[str]]:
    return {
        "run": set(re.findall(r"scripts/([\w.]+\.py)", BASH.read_text(encoding="utf-8"))),
        "run.cmd": set(re.findall(r"scripts\\([\w.]+\.py)", WINDOWS.read_text(encoding="utf-8"))),
    }


def test_both_dispatchers_were_read() -> None:
    """A regex that matches nothing would make every comparison below pass."""
    assert len(bash_commands()) >= 6, bash_commands()
    assert len(windows_commands()) >= 6, windows_commands()


def test_the_same_commands_exist_in_both() -> None:
    only_bash = sorted(bash_commands() - windows_commands())
    only_windows = sorted(windows_commands() - bash_commands())

    assert only_bash == [], f"`./run {only_bash}` works in bash and not in cmd"
    assert only_windows == [], f"`run {only_windows}` works in cmd and not in bash"


def test_every_dispatched_script_exists() -> None:
    missing = {
        where: sorted(name for name in names if not (ROOT / "scripts" / name).exists())
        for where, names in scripts_named().items()
    }
    assert missing == {"run": [], "run.cmd": []}, missing


def test_both_dispatch_the_same_scripts() -> None:
    named = scripts_named()
    assert named["run"] == named["run.cmd"], (
        f"only in run: {sorted(named['run'] - named['run.cmd'])}; "
        f"only in run.cmd: {sorted(named['run.cmd'] - named['run'])}"
    )


def test_the_windows_usage_lists_what_it_dispatches() -> None:
    """The usage line is what someone reads when they do not know the command,
    and `smoke` was asked for by name while no dispatcher had it."""
    body = WINDOWS.read_text(encoding="utf-8")
    usage = body[body.index(":usage") :]
    for command in sorted(windows_commands()):
        assert command in usage, f"`{command}` dispatches but the usage never mentions it"
