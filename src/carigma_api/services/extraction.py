"""Resume and LinkedIn-export extraction. Ported from V1's `resumeiq/parsing.py`.

The parsing is the IP, so the prompts are carried across **verbatim**. Same
libraries too — a different PDF reader extracts subtly different text, and the
analyst downstream would see a different profile for the same file.

## What V1 does, and what this keeps

Three inputs, one output shape:

    a PDF   ─┐
    a .docx ─┼─→ raw text ─→ `parse_profile_text` ─→ the profile fields
    pasted  ─┘

The paste path exists because a scanned resume produces no text and a LinkedIn
export is not something everyone can produce on a phone. It is not a fallback
for failure — it is a first-class input, and V1 learned that the hard way.

## What is deliberately NOT ported

`fetch_linkedin_profile` — the Proxycurl URL sync. It was removed from V1's UI
(bugfix #1: "PDF only") because it needs a paid per-lookup key that is not
configured, and a control that cannot work is the §25 bug class. Porting dead
code so V2 can also not use it would be worse than leaving it in V1's history.

## The size limit is here, not only in the route

A 40MB PDF is a memory problem before it is a validation problem, and the
service is the layer both the route and any future worker share.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Bigger than any real resume and small enough that ten at once is survivable.
MAX_BYTES = 10 * 1024 * 1024

#: V1's threshold, kept. Below this there is not enough to extract anything
#: honest from, and guessing at a near-empty document is how a profile gets
#: invented.
MIN_TEXT = 80

#: What the extractor is given. V1 truncates at the same point.
MAX_PROMPT_CHARS = 12000

#: The fields the extractor returns. Named here so the route can check the
#: response against a declaration rather than trusting the model's key
#: spelling — the renaming-boundary rule, one boundary further out.
EXTRACTED_FIELDS: tuple[str, ...] = (
    "name",
    "currentRole",
    "linkedinHeadline",
    "aboutSection",
    "skills",
    "experience",
    "education",
    "certifications",
    "industry",
    "targetRoles",
    "location",
)


class Unreadable(ValueError):
    """The file or text could not be turned into enough words to work with.

    Carries a sentence the user can act on. Not an error in the "something
    broke" sense — a scanned PDF is a normal thing to upload, and the answer
    is "paste it instead", not an apology.
    """


class Extractor(Protocol):
    """The JSON-returning model call. Injected so tests never hit the API."""

    def __call__(self, system: str, user: str, max_tokens: int) -> dict[str, Any]: ...


def extract_pdf_text(data: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n\n".join(parts).strip()


def extract_docx_text(data: bytes) -> str:
    """Paragraphs AND tables — V1 learned that resumes hide half their content
    in table cells, and a paragraphs-only read loses entire employment
    histories without appearing to fail."""
    import docx

    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(x for x in parts if x and x.strip()).strip()


def text_from_upload(data: bytes, filename: str) -> str:
    """Route a file to the right reader by extension, as V1 does.

    A failure here is `Unreadable` with the sentence for that input type, never
    a raw library exception: "PdfminerException" tells the user nothing they
    can act on, and the useful advice differs by format.
    """
    if len(data) > MAX_BYTES:
        raise Unreadable(
            "That file is larger than 10MB. A resume export is usually well under 1MB — "
            "if yours is scanned images, paste your profile text instead."
        )
    name = (filename or "").lower()
    try:
        text = extract_docx_text(data) if name.endswith(".docx") else extract_pdf_text(data)
    except Unreadable:
        raise
    except Exception as exc:
        logger.info("extraction failed for %s: %s", name or "(unnamed)", exc)
        raise Unreadable(
            "We couldn't read that file. Try re-exporting from LinkedIn "
            "(Profile → More → Save to PDF), or paste your profile text instead."
        ) from exc
    return text


def parse_profile_text(text: str, *, extractor: Extractor) -> dict[str, Any]:
    """Raw text to structured fields. V1's prompt, unchanged.

    The `targetRoles` and `location` keys are in the resume prompt but not in
    V1's LinkedIn-PDF prompt; this uses the resume one for every input, which
    is what V1's `parse_resume` and its paste path both do.
    """
    text = (text or "").strip()
    if len(text) < MIN_TEXT:
        raise Unreadable(
            "Couldn't read enough text. Use a text-based PDF or Word resume "
            "(a scanned image won't work), or paste more of your profile."
        )

    system = (
        "You extract structured career data from a resume or LinkedIn export. "
        "Return ONLY a raw JSON object. No prose, no markdown. Start with { end with }."
    )
    user = f"""Extract profile data from this resume / LinkedIn text. Return a JSON object with EXACT keys:
- name (string)
- currentRole (string — latest job title AND company)
- linkedinHeadline (string — a professional tagline; infer one if not explicit)
- aboutSection (string — a summary/objective if present, else "")
- skills (string — comma-separated list of ALL skills/tools mentioned)
- experience (string — detailed summary of work history with achievements)
- education (string — degrees and institutions)
- certifications (string — licenses, certs, courses with issuers/dates)
- industry (string — inferred from their roles)
- targetRoles (string — roles they appear to be targeting; infer from recent titles if unclear)
- location (string — city/country if present, else "")

If a field is missing, use empty string "". Return ONLY the JSON object.

RESUME / PROFILE TEXT:
{text[:MAX_PROMPT_CHARS]}"""

    raw = extractor(system, user, 3000)
    return clean(raw)


def clean(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep the fields we declared, as strings, dropping the empty ones.

    Two rules, both learned elsewhere in this codebase:

    **Unknown keys are dropped, not merged.** The model is asked for an exact
    set; anything else is a hallucinated field, and `profile_to_db` would
    (correctly) raise on it. Dropping here means a stray key costs a field, not
    the whole upload.

    **Empty strings are dropped rather than written.** The prompt asks for `""`
    on a missing field, and writing that over an existing value would let a
    thin re-upload erase a profile the user had already filled in.
    """
    out: dict[str, Any] = {}
    for key in EXTRACTED_FIELDS:
        value = (raw or {}).get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


__all__ = [
    "EXTRACTED_FIELDS",
    "MAX_BYTES",
    "MIN_TEXT",
    "Extractor",
    "Unreadable",
    "clean",
    "extract_docx_text",
    "extract_pdf_text",
    "parse_profile_text",
    "text_from_upload",
]
