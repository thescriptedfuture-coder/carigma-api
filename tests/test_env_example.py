"""`.env.example` must stay in sync with `Settings`.

A field added to `Settings` but missing from `.env.example` is invisible until
someone deploys and wonders why a feature is off. A field left in
`.env.example` after being removed from `Settings` is worse — it invites
someone to set a secret that nothing reads, which looks configured but isn't.

These tests make both cases fail in CI instead of in production.
"""

from __future__ import annotations

import re
from pathlib import Path

from carigma_api.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _documented_keys(*, include_commented: bool = False) -> set[str]:
    """Keys present in .env.example.

    `include_commented` also picks up the "not read yet" block, which is
    deliberately commented out so setting it has no effect today.
    """
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if not include_commented:
                continue
            stripped = stripped.lstrip("#").strip()
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", stripped)
        if match:
            keys.add(match.group(1))
    return keys


def _settings_keys() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_env_example_exists_and_is_tracked() -> None:
    assert ENV_EXAMPLE.is_file(), f"missing {ENV_EXAMPLE}"


def test_every_settings_field_is_documented() -> None:
    """If the API reads it, the template must mention it.

    Commented mentions count: some fields (SUPABASE_JWKS_URL) are genuine
    override-only options that should ship commented out. What matters is that
    the reader can discover the field exists.
    """
    missing = _settings_keys() - _documented_keys(include_commented=True)
    assert not missing, (
        "Settings fields absent from .env.example: "
        + ", ".join(sorted(missing))
        + ". Add them with a comment saying what they are and where to get them."
    )


def test_no_documented_key_is_silently_unread() -> None:
    """An active (uncommented) key that Settings does not read looks configured
    but does nothing — the most confusing possible state."""
    unread = _documented_keys() - _settings_keys()
    assert not unread, (
        "Active keys in .env.example that Settings does not read: "
        + ", ".join(sorted(unread))
        + ". Either wire them up, or move them to the commented "
        "'NOT READ YET' block."
    )


def test_no_hs256_secret_is_reintroduced() -> None:
    """Regression guard, in the template as well as the code.

    An HS256 shared secret both verifies AND mints tokens. A verifier that can
    mint can forge a session for any user, so the field must not come back —
    and must not be suggested by the template either.
    """
    content = ENV_EXAMPLE.read_text(encoding="utf-8")
    active = [
        line for line in content.splitlines() if line.strip().startswith("SUPABASE_JWT_SECRET")
    ]
    assert not active, "SUPABASE_JWT_SECRET reintroduced in .env.example"
    assert "SUPABASE_JWT_SECRET" in content, (
        "The template should still EXPLAIN why the secret is absent — silence "
        "invites someone to add it back."
    )


def test_template_carries_no_real_secret_values() -> None:
    """The template must ship blank. A committed key is a leaked key."""
    content = ENV_EXAMPLE.read_text(encoding="utf-8")

    for pattern, label in [
        (r"sk-ant-[A-Za-z0-9\-_]{10,}", "Anthropic key"),
        (r"eyJ[A-Za-z0-9\-_]{20,}", "JWT / Supabase key"),
        (r"rzp_(live|test)_[A-Za-z0-9]{10,}", "Razorpay key"),
    ]:
        assert not re.search(pattern, content), f"{label} committed in .env.example"


def test_service_key_is_marked_server_only() -> None:
    """The single most dangerous value in the project must be labelled as such,
    right where someone is about to paste it."""
    content = ENV_EXAMPLE.read_text(encoding="utf-8")
    idx = content.find("SUPABASE_SERVICE_KEY=")
    assert idx > 0
    preceding = content[:idx].lower()
    assert "never put this in carigma-web" in preceding
    assert "bypasses all row-level security" in preceding


def test_no_module_reads_the_environment_behind_Settings_back() -> None:
    """Every env var must arrive through `Settings`, so `.env.example` can be
    checked against it.

    This closes the gap that let the SMTP fields exist for a whole phase
    without appearing in `.env.example`: the sync test above compares
    `Settings` to the template, so a module calling `os.getenv("SMTP_HOST")`
    directly is invisible to BOTH sides of that comparison. Adding the fields
    fixed the symptom; this fixes the hole.

    It is the same blind spot as a guard that checked AST identifiers while the
    violation lived in a string literal — a check that inspects one
    representation and not the other.

    `config.py` is the one legitimate reader. Scripts are included: the digest
    cron is where the SMTP fields were read from in the first place.
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []

    for path in sorted([*(root / "src").rglob("*.py"), *(root / "scripts").rglob("*.py")]):
        if path.name == "config.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # os.getenv(...) / os.environ.get(...) / environ[...]
            reads_env = (
                isinstance(func, ast.Attribute)
                and func.attr in {"getenv"}
                or (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                )
            )
            if reads_env:
                name = (
                    node.args[0].value
                    if node.args and isinstance(node.args[0], ast.Constant)
                    else "<dynamic>"
                )
                offenders.append(f"{path.relative_to(root)}: reads {name!r} directly")

    assert not offenders, (
        "env vars must come through Settings so .env.example can be verified:\n  "
        + "\n  ".join(offenders)
    )
