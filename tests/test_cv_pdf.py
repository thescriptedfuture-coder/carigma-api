"""Every layout renders a real stored kit into a real file.

The six classes are a PORT of V1's `resumeiq/cv.py`, carried over rather than
redrawn so the document a user sends to an employer stays identical. A port is
only worth anything if it runs on the data it was ported for, so the fixture
here is the live kit shape — objects in `experience` and `education`, a rupee
sign, em dashes and an empty `grade` — not a tidy invention.

fpdf2's core fonts are latin-1. `₹10-16 LPA` and `2014 — 2018` are both in the
stored kits, and either would raise on a renderer that did not normalise. V1's
`_safe` came across unchanged; these tests are what says so.
"""

from __future__ import annotations

from typing import Any

import pytest

from carigma_api.services.cv_pdf import (
    DEFAULT_TEMPLATE,
    TEMPLATE_KEYS,
    TEMPLATES,
    filename_for,
    render,
    resolve,
)

#: Shaped from the nine live kits, including the characters that break a
#: latin-1 renderer.
KIT: dict[str, Any] = {
    "name": "Ravi Kumar",
    "tagline": "Data Analyst | Government Finance ERP | Power BI & SQL",
    "email": "ravi@example.com",
    "phone": "+91 XXXXX XXXXX",
    "location": "Delhi, India",
    "linkedin": "linkedin.com/in/ravikumar",
    "summary": "Delhi-based analyst turning operational data into decisions — ₹10-16 LPA band.",
    "coreSkills": ["SQL", "Power BI", "Stakeholder reporting", "PFMS"],
    "experience": [
        {
            "title": "Senior Auditor (Business Analyst — ERP & Public Finance)",
            "company": "CGDA",
            "location": "New Delhi",
            "duration": "2019 — now",
            "bullets": [
                "Sole functional owner of the Defence Travel System — a mission-critical platform.",
                "Cut monthly reporting from three days to four hours.",
            ],
            "keyImpact": "₹2.4 crore of reconciliation errors caught before payment.",
        }
    ],
    "education": [
        {
            "degree": "B.A. Honours in Business Economics",
            "institution": "University of Delhi (DU)",
            "year": "2011 — 2013",
            # Empty on every live row.
            "grade": "",
        }
    ],
    "certifications": ["Google Data Analytics Professional Certificate — in progress"],
    "keyAchievements": ["Dashboard cut reporting effort by 40%"],
}

#: A PDF starts `%PDF-` and ends `%%EOF`. Checking both means the renderer
#: finished rather than threw halfway and handed back a prefix.
MAGIC = b"%PDF-"


@pytest.mark.parametrize("template", sorted(TEMPLATE_KEYS))
def test_every_template_renders_a_whole_file(template: str) -> None:
    pdf, chosen, _name = render(KIT, template)

    assert chosen == template
    assert pdf.startswith(MAGIC), f"{template} did not produce a PDF"
    assert pdf.rstrip().endswith(b"%%EOF"), f"{template} stopped mid-document"
    # A one-page CV is a few KB. A few hundred bytes is an empty document that
    # happens to be well-formed.
    assert len(pdf) > 2000, f"{template} produced {len(pdf)} bytes"


def test_the_picker_offers_exactly_what_renders() -> None:
    """A key in the list that does not render is a template a user can choose
    and never receive."""
    assert {t["key"] for t in TEMPLATES} == set(TEMPLATE_KEYS)
    assert len(TEMPLATES) == 6
    for entry in TEMPLATES:
        assert entry["label"] and entry["description"]


def test_an_unknown_template_falls_back_rather_than_failing() -> None:
    """A stale link in somebody's bookmarks should hand them a CV in the
    default layout, not an error about a layout they never chose."""
    assert resolve("sideways") == DEFAULT_TEMPLATE
    assert resolve(None) == DEFAULT_TEMPLATE
    assert resolve("executive") == "executive"


def test_a_kit_with_characters_a_latin1_font_cannot_hold_still_renders() -> None:
    """`₹` and `—` are both in the live data. Either raises on a renderer that
    does not normalise, and V1's `_safe` is the reason it does not."""
    assert "₹" in KIT["summary"]
    assert "—" in KIT["education"][0]["year"]
    pdf, _chosen, _name = render(KIT, "modern")
    assert pdf.startswith(MAGIC)


def test_a_kit_missing_most_of_itself_still_produces_a_document() -> None:
    """Not every stored kit is complete. A renderer that needs all twelve
    fields turns a thin CV into an error page."""
    thin = {"name": "A Person"}
    pdf, _chosen, _name = render(thin, "classic")
    assert pdf.startswith(MAGIC)


def test_the_filename_carries_the_person_not_the_job_id() -> None:
    """It lands in an employer's downloads folder."""
    assert filename_for(KIT, "modern") == "Ravi-Kumar-Modern-CV.pdf"


def test_the_filename_survives_a_name_that_cannot_go_in_a_header() -> None:
    """This value goes into `Content-Disposition`. A quote or a slash in it is
    a broken header, and a name we cannot use is not a reason to fail."""
    assert filename_for({"name": 'Ravi "The Analyst" Kumar/Singh'}, "tech") == (
        "Ravi-The-Analyst-KumarSingh-Tech-CV.pdf"
    )
    assert filename_for({}, "modern") == "CV-Modern-CV.pdf"
    assert filename_for({"name": "★★★"}, "minimal") == "CV-Minimal-CV.pdf"
