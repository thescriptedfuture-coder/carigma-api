"""Every import must be a declared dependency.

## The bug this exists for

The first container deploy crashed at startup:

    ModuleNotFoundError: No module named 'supabase'

`supabase` was never in `pyproject.toml`. It worked locally for months because
it was in the venv from earlier work, and **a local venv is a superset of what
you declared** — it accumulates whatever anything ever installed. A container
installs only the declaration, so the gap only appears the first time you
deploy.

Auditing the rest found `anthropic` missing too, and that one is worse:
importing it lazily meant the process would have STARTED, served `/health`,
and crashed on the first profile extraction. Which is to say, on the export
session, against a real file, with somebody watching.

## Same species as `.env.example` drifting from `Settings`

Two declarations of the same fact — what this program needs — kept in sync by
memory. The fix is the same: compare them mechanically.

## Why the import name is not the package name

`docx` ships as `python-docx`, `jwt` as `pyjwt`, `pydantic_settings` as
`pydantic-settings`. A naive string comparison would report three false
failures and get deleted within a week, so the aliases are declared below and
every entry has to be justified.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: import name → distribution name, where they differ. Each one is a real
#: PyPI package whose module is spelled differently; nothing here is a way to
#: silence a missing dependency.
ALIASES = {
    "docx": "python-docx",
    "jwt": "pyjwt",
    "pydantic_settings": "pydantic-settings",
}

# Three more were written here on the first draft — python-dotenv,
# python-multipart, python-dateutil — for modules nothing imports. The
# dead-entry test below caught them immediately. An alias nobody needs is a
# name that was once a guess, and guesses are where a real one hides.

#: First-party or otherwise not a dependency.
OURS = {"carigma_api", "tests", "__future__", "conftest"}


def declared() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    raw = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        raw.extend(group)
    # "uvicorn[standard]>=0.32" → "uvicorn"
    return {re.split(r"[\[><=!;\s]", spec, maxsplit=1)[0].strip().lower() for spec in raw}


def imports_in(*dirs: str) -> dict[str, set[str]]:
    """Top-level third-party module → the files that import it."""
    found: dict[str, set[str]] = {}
    for name in dirs:
        base = ROOT / name
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module.split(".")[0]]
                else:
                    continue
                for mod in mods:
                    if mod in sys.stdlib_module_names or mod in OURS:
                        continue
                    # A sibling script importing another script by filename.
                    if (base / f"{mod}.py").exists():
                        continue
                    found.setdefault(mod, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_import_is_declared() -> None:
    """The guard proper.

    Covers `scripts/` as well as `src/`: the cron runs in the same container,
    and a dependency only the Sunday job needs is one that fails on a Sunday.
    """
    have = declared()
    used = imports_in("src", "scripts")

    assert used, "no third-party imports found — this guard would prove nothing"

    missing = {
        module: sorted(files)
        for module, files in sorted(used.items())
        if ALIASES.get(module, module).lower() not in have
    }

    assert missing == {}, (
        "imported but NOT declared in pyproject.toml. This works locally — a "
        "venv accumulates whatever anything ever installed — and fails on the "
        f"first container deploy: {missing}"
    )


def test_the_alias_map_has_no_dead_entries() -> None:
    """Every exemption must prove its entry is real.

    An alias for a package nobody imports is a name that was once wrong and is
    now noise, and noise is where a real alias hides.
    """
    used = set(imports_in("src", "scripts"))
    dead = sorted(k for k in ALIASES if k not in used)
    assert dead == [], f"aliases for modules nothing imports: {dead}"


def test_the_interpreter_is_pinned_in_the_repo() -> None:
    """Render chose 3.14 on the first deploy because nothing said otherwise.

    `.python-version` is the one that SELECTS, and it is committed so it cannot
    drift per-environment the way a dashboard variable can. A `PYTHON_VERSION`
    env var would have worked and would have lived in one deployment's
    settings, invisible to the repo and to every other environment.
    """
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert pinned.startswith("3.11"), f".python-version says {pinned!r}"


def test_requires_python_refuses_the_version_render_picked() -> None:
    """The second net. `.python-version` selects; this one refuses.

    pip will not install into an interpreter outside the range, so a
    mis-selected runtime fails at BUILD time with a clear message rather than
    at runtime with a missing wheel.
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = data["project"]["requires-python"]

    assert ">=3.11" in spec
    # An upper bound is the half that matters here: without it, 3.14 satisfies
    # ">=3.11" and the deploy that just failed would have been allowed again.
    assert "<3.13" in spec, f"no upper bound in {spec!r} — 3.14 would satisfy it"


def test_the_running_interpreter_matches_the_pin() -> None:
    """So the gates cannot pass on a version production will never run."""
    pinned = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert running == pinned, f"tests are running on {running}, the repo pins {pinned}"
