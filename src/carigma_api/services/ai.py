"""Anthropic client and robust JSON-mode helper.

PORTED VERBATIM from V1 `resumeiq/ai.py`. The only changes are mechanical:
Streamlit removed (no `st.secrets`, no `st.stop()`), and the API key comes from
settings instead of module-level lookup. **The JSON repair logic and the prompt
suffix are unchanged** — they are tuned behaviour, not scaffolding.
"""

from __future__ import annotations

import json
import re
from typing import Any

import anthropic

from carigma_api.services.constants import MODEL


class UpstreamError(Exception):
    """Claude was unreachable or returned something unusable.

    Raised so the caller can map it to a friendly message AND — critically —
    skip charging credits. See services/credits.py.
    """


def get_client(api_key: str) -> anthropic.Anthropic:
    if not api_key:
        raise UpstreamError("ANTHROPIC_API_KEY is not configured.")
    return anthropic.Anthropic(api_key=api_key)


def _repair_json(s: str) -> str:
    """Multi-pass JSON repair for common LLM output issues:

    1. Remove trailing commas before ] or }
    2. Fix bare (unescaped) newlines / tabs / carriage returns inside string
       values using a character-by-character state machine — the most reliable
       approach.
    """
    # Pass 1: trailing commas
    s = re.sub(r",\s*([}\]])", r"\1", s)

    # Pass 2: fix unescaped control characters inside JSON strings
    result: list[str] = []
    in_string = False
    escape_next = False
    escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    for ch in s:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == "\\" and in_string:
            result.append(ch)
            escape_next = True
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string and ch in escapes:
            result.append(escapes[ch])
        else:
            result.append(ch)
    return "".join(result)


def call_claude_json(
    system: str,
    user: str,
    max_tokens: int = 4096,
    *,
    api_key: str,
) -> Any:
    """Call Claude and return parsed JSON.

    Tries up to 3 times: direct parse → repair → retry the whole call.
    Raises UpstreamError when all three attempts fail — never returns a
    fabricated or partial result.
    """
    client = get_client(api_key)
    json_system = (
        system + "\n\nCRITICAL JSON FORMATTING RULES:\n"
        "- Return ONLY valid JSON — no prose, no markdown fences.\n"
        "- Inside every string value, represent line breaks as the two-char sequence \\n "
        "(backslash + n), NEVER as a real newline character.\n"
        "- Never use trailing commas.\n"
        "- All keys and string values must be enclosed in double quotes."
    )

    last_raw = ""
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=json_system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIError as exc:
            if attempt == 2:
                raise UpstreamError(str(exc)) from exc
            continue

        raw = resp.content[0].text  # type: ignore[union-attr]
        last_raw = raw
        cleaned = re.sub(r"```json\s*|```", "", raw).strip()
        match = re.search(r"(\[[\s\S]*\]|\{[\s\S]*\})", cleaned, re.DOTALL)
        if not match:
            if attempt == 2:
                raise UpstreamError(
                    f"No JSON structure found after 3 attempts. Last response: {last_raw[:400]}"
                )
            continue

        json_str = match.group(1)

        # Attempt A — parse as-is
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

        # Attempt B — repair then parse
        try:
            return json.loads(_repair_json(json_str))
        except json.JSONDecodeError as exc:
            if attempt == 2:
                raise UpstreamError(f"JSON parse failed after repair: {exc}") from exc
            # else loop and retry the whole API call

    raise UpstreamError("Exhausted all attempts without a parseable response.")
