"""Everything we use from the Anthropic SDK, checked against what is installed.

## The bug this exists for

A Render cron build pulled `anthropic 1.0.0` five days after the API build
pulled `0.122.0`, because the spec was `>=0.40` with no ceiling. Three
environments — local, API, cron — ran three different versions of the same SDK,
and **none of them matched what the test suite had verified**.

1.0 turned out not to change anything we call. That is luck, not a process: a
major version is exactly where a library is entitled to change its interface.

## Why this file rather than a version assertion

Pinning a version tells you the number changed. This tells you whether the
NUMBER MATTERS — it names the four things we actually touch, so raising the
ceiling later is a decision someone can check in a minute instead of a leap.

Signatures and symbols only. No network, no key, no cost.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import anthropic


def test_the_client_constructor_is_unchanged() -> None:
    assert hasattr(anthropic, "Anthropic")
    assert "api_key" in inspect.signature(anthropic.Anthropic.__init__).parameters


def test_the_error_we_catch_still_exists() -> None:
    """`services/ai.py` catches `anthropic.APIError` and retries three times.

    If this moved, every upstream failure would become an unhandled exception
    on the extraction path.
    """
    assert hasattr(anthropic, "APIError")
    assert issubclass(anthropic.APIError, BaseException)


def test_messages_create_takes_the_four_arguments_we_pass() -> None:
    client = anthropic.Anthropic(api_key="sk-ant-not-a-real-key")
    params = inspect.signature(client.messages.create).parameters

    for kw in ("model", "max_tokens", "system", "messages"):
        assert kw in params, f"messages.create no longer takes {kw}"


def test_the_response_shape_we_read_is_unchanged() -> None:
    """We read `resp.content[0].text` and nothing else."""
    from anthropic.types import Message, TextBlock

    assert "content" in Message.model_fields
    assert "text" in TextBlock.model_fields


def test_this_file_covers_every_symbol_the_code_actually_uses() -> None:
    """The guard on the guard.

    A file listing four symbols is only worth having if four is still the
    number. This walks `services/ai.py` for every `anthropic.<name>` reference
    and fails when one appears that nothing above checks — otherwise a fifth
    call site would be silently uncovered, and the next major bump would be
    verified against a stale list.
    """
    source = Path(__file__).parents[1] / "src" / "carigma_api" / "services" / "ai.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "anthropic"
    }

    assert used, "no anthropic.* usage found — this guard would prove nothing"
    covered = {"Anthropic", "APIError"}
    assert used <= covered, (
        f"services/ai.py uses anthropic symbols this file does not check: {sorted(used - covered)}"
    )
