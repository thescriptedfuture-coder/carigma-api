"""Which CV layouts exist, and rendering a stored kit into one.

The six layouts themselves are in `cv_templates.py`, ported from V1. This is
the part the API owns: the list the client offers, and the one function that
turns a stored `apply_kit.cv` into a file.

## Free, and why that is not an oversight

Generating the kit cost 15 credits and already happened. Re-rendering data we
hold into a different layout is not a run — it calls no model, reaches no
provider, and produces nothing that did not already exist. V1 priced it the
same way: *"template switching free (local re-render)"*.

So the picker cannot cost anything, which also means it cannot fail for lack of
credits, which is why there is no balance check on this path and no test
asserting one.

## A kit with no CV is a 404, never an empty document

Eighty-four of the ninety-three stored rows have no kit at all. A PDF rendered
from nothing is a page with a name on it and no career, and it would arrive
looking like a product that works.
"""

from __future__ import annotations

from typing import Any

from carigma_api.services.cv_templates import generate_cv_pdf

#: The six, in the order the picker shows them. Labels and descriptions are
#: V1's `TEMPLATE_META`, carried over so a user who has seen both products is
#: choosing between the same six things by the same names.
TEMPLATES: tuple[dict[str, str], ...] = (
    {"key": "modern", "label": "Modern", "description": "Dark header, indigo accents"},
    {"key": "classic", "label": "Classic", "description": "Traditional, ATS-safe, serif"},
    {"key": "executive", "label": "Executive", "description": "Navy header, gold accents"},
    {"key": "minimal", "label": "Minimal", "description": "Pure white, no colour, ultra-clean"},
    {"key": "tech", "label": "Tech", "description": "Charcoal header, teal accents"},
    {"key": "sidebar", "label": "Sidebar", "description": "Two-column dark left panel"},
)

TEMPLATE_KEYS: frozenset[str] = frozenset(t["key"] for t in TEMPLATES)

DEFAULT_TEMPLATE = "modern"


def resolve(template: str | None) -> str:
    """A known template, or the default.

    An unknown key falls back rather than failing. V1's dispatcher does the
    same, and a stale link in somebody's bookmarks should hand them a CV in the
    default layout rather than an error about a layout they never chose.
    """
    return template if template in TEMPLATE_KEYS else DEFAULT_TEMPLATE


def filename_for(cv: dict[str, Any], template: str) -> str:
    """`Ravi-Kumar-Modern-CV.pdf`, or something a file manager can hold.

    The name reaches an employer's downloads folder, so it carries the person's
    name rather than a job id. Non-ASCII and path separators are stripped
    because this value goes into a `Content-Disposition` header.
    """
    raw = str(cv.get("name") or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in " -" else "" for ch in raw).strip()
    stem = "-".join(part for part in safe.split() if part) or "CV"
    return f"{stem}-{template.capitalize()}-CV.pdf"


def render(cv: dict[str, Any], template: str | None) -> tuple[bytes, str, str]:
    """`(pdf bytes, the template used, a filename)`.

    The caller gets the resolved template back rather than the one it asked
    for, so a response can say which layout it actually produced.
    """
    chosen = resolve(template)
    return generate_cv_pdf(cv, chosen), chosen, filename_for(cv, chosen)


__all__ = [
    "DEFAULT_TEMPLATE",
    "TEMPLATES",
    "TEMPLATE_KEYS",
    "filename_for",
    "render",
    "resolve",
]
