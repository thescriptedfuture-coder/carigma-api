"""What a model may claim about a person: only what the person told us.

Shared by every writer that puts words under a user's name — posts
(`content_writer`) and CVs (`cv_writer`). One rule in one place, because two
copies of "never invent a number" are two chances for one of them to drift.

A prompt asks the model not to invent. These functions are how that request is
CHECKED, against the profile text the model was given.
"""

from __future__ import annotations

import re

#: A blank someone would have to fill: `[Company]`, `<role>`, `{name}`, `XX%`.
BLANK = re.compile(r"\[[^\]\n]{1,40}\]|<[^>\n]{1,40}>|\{[^}\n]{1,40}\}|\bX{2,}\b", re.I)

#: The figures that get invented: percentages, rupee and crore/lakh amounts,
#: multipliers. Plain counts ("five years", "40 field offices") are left alone —
#: they are usually read straight off the experience, and flagging them would
#: drop honest writing.
_FIGURE = re.compile(
    # A word boundary before rs/inr, or "five years 2019" reads as a rupee amount.
    r"(?:₹|\brs\.?|\binr)\s?\d[\d,.]*\s*(?:crore|cr|lakh|lac|k|mn|million|bn|billion)?"
    r"|\d[\d,.]*\s?%"
    r"|\d[\d,.]*\s?(?:crore|cr|lakh|lac)\b"
    r"|\b\d+(?:\.\d+)?x\b",
    re.I,
)


def _normalise(raw: str) -> str:
    """One spelling per figure, so "₹2 crore" in a profile matches "Rs 2 Cr"
    in a draft. A check that a respelling defeats would drop honest writing."""
    text = re.sub(r"[\s,]", "", raw.lower())
    # The amount and its unit are the claim; "₹", "Rs" and none at all are one.
    text = re.sub(r"^(?:₹|rs\.?|inr)", "", text)
    text = re.sub(r"(?:crore|cr)$", "cr", text)
    return re.sub(r"(?:lakh|lac)$", "lakh", text)


def figures(text: str) -> list[str]:
    """Every checkable figure in `text`, normalised."""
    return [_normalise(m.group(0)) for m in _FIGURE.finditer(text)]


def invented(text: str, known: frozenset[str]) -> list[str]:
    """Figures in `text` that are not among `known` — the ones the person
    never gave us."""
    return [figure for figure in figures(text) if figure not in known]


def blank_in(text: str) -> str | None:
    """The first blank-to-fill in `text`, or None."""
    match = BLANK.search(text)
    return match.group(0) if match else None


_WORD = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> set[str]:
    """Lowercased words of three letters or more, cut to five letters, so
    "visualisation" and "visualization" — and "analyst" and "analytics" — meet.
    Short words ("bi", "of") carry no evidence either way."""
    return {word[:5] for word in _WORD.findall(text.lower()) if len(word) >= 3}


def supported(claim: str, corpus: set[str], *, at_least: float = 1.0) -> bool:
    """Whether enough of `claim`'s substantive words appear in what the person
    told us. A claim with no substantive word ("BI") is let through: there is
    nothing in it to contradict.

    `at_least` is per kind of claim, because they vary differently. A skill is
    one or two words and must all be theirs. An employer gets decorated —
    "Controller General of Defence Accounts (CGDA), Ministry of Defence" for a
    profile that never says "Ministry" — and requiring every word would drop
    the one real employer a person has.
    """
    words = tokens(claim)
    if not words:
        return True
    return len(words & corpus) / len(words) >= at_least


__all__ = ["BLANK", "blank_in", "figures", "invented", "supported", "tokens"]
