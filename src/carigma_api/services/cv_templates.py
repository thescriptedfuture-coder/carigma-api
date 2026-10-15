"""The six CV layouts, ported from V1 rather than redrawn.

## Why a port and not a rewrite

`resumeiq/cv.py` is six fpdf2 classes measured in millimetres against fpdf2's
own text metrics. Redrawing them in another engine produces six layouts that
are *nearly* V1's, and "nearly" is the kind of difference a user notices on the
document they send to an employer. The stored kits already carry exactly the
fields these classes read — `name, tagline, email, phone, location, linkedin,
summary, coreSkills, experience, education, certifications, keyAchievements` —
verified against all nine, so this is a copy with its dependencies declared,
not a reinterpretation.

## What V2 does differently

- **No headshot.** V1's `generate_cv_pdf` takes a `headshot_path` and each
  template wraps the image call in `try/except/pass`. Nothing in V2 stores a
  headshot, so all of it is removed rather than carried: five silent
  swallows for a feature that cannot fire is the orphan class with a lint
  suppression on top, and the next reader would wire it up.
- **`_safe` matters more here than it did in V1.** fpdf2's core fonts are
  latin-1, and the live kits contain em dashes and rupee signs — `₹10-16 LPA`
  is in the data this renders. V1's normaliser is carried over unchanged, and
  `tests/test_cv_pdf.py` renders every template from a kit full of them.

## What is NOT carried over

V1's `TEMPLATE_META` colours are duplicated in `services/cv_pdf.py`, which is
what the API publishes to the client. The copy here is the drawing code only.
"""

import re
import unicodedata
from typing import Any

from fpdf import FPDF

# Map Unicode symbols that appear regularly in AI-generated CV content to
# readable ASCII/latin-1 equivalents, so nothing silently disappears from PDFs.
_UNICODE_TO_LATIN1 = {
    "–": "-",  # en dash  –
    "—": "--",  # em dash  —
    "―": "--",  # horizontal bar
    "‘": "'",  # left single quote  '
    "’": "'",  # right single quote '
    "‚": ",",  # single low-9 quote
    "“": '"',  # left double quote  "
    "”": '"',  # right double quote "
    "…": "...",  # ellipsis  …
    "•": "-",  # bullet  •
    "‣": ">",  # triangular bullet
    "▶": ">",  # black right-pointing triangle
    "→": "->",  # rightwards arrow  →
    "←": "<-",  # leftwards arrow   ←
    "⇒": "=>",  # rightwards double arrow
    "·": ".",  # middle dot  ·
    "−": "-",  # minus sign  −
    "×": "x",  # multiplication sign  ×
    "÷": "/",  # division sign  ÷
    "™": "(TM)",  # trade mark sign  ™
    "®": "(R)",  # registered sign  ®
    "©": "(C)",  # copyright sign  ©
    "₹": "Rs.",  # Indian rupee sign  ₹
    "★": "*",  # black star  ★
    "☆": "*",  # white star  ☆
    "✔": "v",  # heavy check mark  ✔
    "✓": "v",  # check mark  ✓
    "✘": "x",  # heavy ballot x  ✘
    " ": " ",  # non-breaking space
    "​": "",  # zero-width space
    "‑": "-",  # non-breaking hyphen
    "«": "<<",  # left-pointing double angle quotation mark  «
    "»": ">>",  # right-pointing double angle quotation mark  »
}


def _safe(s) -> str:
    """
    Convert any string to a latin-1-safe representation for fpdf2 built-in fonts.

    Strategy (three passes):
      1. Replace known Unicode symbols with readable ASCII equivalents.
      2. NFKD-decompose accented characters (e -> e, drop combining accents).
      3. Encode to latin-1, silently drop anything still outside the range.

    This ensures readable output rather than silently swallowing characters.
    """
    if not isinstance(s, str):
        s = str(s)
    # Pass 1 — explicit replacements
    for ch, replacement in _UNICODE_TO_LATIN1.items():
        s = s.replace(ch, replacement)
    # Pass 2 — decompose accents (e.g. é -> e + combining acute, then drop accent)
    s = unicodedata.normalize("NFKD", s)
    # Pass 3 — encode to latin-1, ignoring anything left that can't be encoded
    return s.encode("latin-1", "ignore").decode("latin-1")


BULLET = ""  # WinAnsi bullet byte - renders as a real bullet in fpdf core fonts

_LIST_MARKER = re.compile(r"^\s*(?:\[\*\]|[-*•▪●]|\d+[.)])\s+")


def _lt(text) -> str:
    """Marker-strip only (no bullet added) - for templates with their own
    list style (tech's ">>", plain indents). Bug 2.1."""
    return _safe(_LIST_MARKER.sub("", str(text or "").strip()))


def _li(text) -> str:
    """One clean list line for the PDF (bug 2.1): strips any marker the AI put
    at the front of the item (-, *, bullet, "1.", literal [*]) and prefixes a
    REAL bullet, so resumes never show raw markdown artifacts."""
    t = _LIST_MARKER.sub("", str(text or "").strip())
    return BULLET + "  " + _safe(t)


class ModernCV(FPDF):
    """Modern CV: dark header, indigo accents."""

    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=12)
        self.set_margins(left=15, top=10, right=15)
        self.usable_width = 180

    def render(self, cv: dict) -> bytes:
        self.add_page()

        # ── Dark header band ──────────────────────────────────
        self.set_fill_color(15, 23, 42)
        self.rect(0, 0, 210, 42, "F")

        self.set_xy(15, 11)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 22)
        self.cell(145, 9, _safe(cv.get("name", "")), ln=True)

        self.set_xy(15, 23)
        self.set_text_color(148, 163, 184)
        self.set_font("Helvetica", "", 10)
        self.cell(145, 5, _safe(cv.get("tagline", "")), ln=True)

        # Contact row
        self.set_xy(15, 31)
        self.set_text_color(100, 116, 139)
        self.set_font("Helvetica", "", 8.5)
        contact = "  |  ".join(
            filter(
                None,
                [
                    _safe(cv.get("email", "")),
                    _safe(cv.get("phone", "")),
                    _safe(cv.get("location", "")),
                    _safe(cv.get("linkedin", "")),
                ],
            )
        )
        self.cell(145, 5, contact, ln=True)

        # ── Summary ───────────────────────────────────────────
        self.set_y(50)
        self.set_text_color(55, 65, 81)
        self.set_font("Helvetica", "", 10)
        self.set_x(15)
        self.multi_cell(self.usable_width, 5.5, _safe(cv.get("summary", "")))

        # ── Core Skills ───────────────────────────────────────
        self._section("CORE SKILLS")
        skill_text = "   ·   ".join(_safe(s) for s in cv.get("coreSkills", []))
        self.set_x(15)
        self.set_text_color(55, 65, 81)
        self.set_font("Helvetica", "", 9)
        self.multi_cell(self.usable_width, 5, skill_text)
        self.ln(1)

        # ── Experience ────────────────────────────────────────
        self._section("EXPERIENCE")
        for exp in cv.get("experience", []):
            self.set_x(15)
            self.set_text_color(15, 23, 42)
            self.set_font("Helvetica", "B", 11)
            self.cell(130, 6, _safe(exp.get("title", ""))[:55])
            self.set_text_color(100, 116, 139)
            self.set_font("Helvetica", "", 9)
            self.cell(50, 6, _safe(exp.get("duration", "")), ln=True, align="R")

            self.set_x(15)
            self.set_text_color(99, 102, 241)
            self.set_font("Helvetica", "I", 10)
            co_loc = _safe(exp.get("company", ""))
            if exp.get("location"):
                co_loc += "  ·  " + _safe(exp.get("location", ""))
            self.cell(self.usable_width, 5, co_loc, ln=True)

            self.set_text_color(55, 65, 81)
            self.set_font("Helvetica", "", 9)
            for bullet in exp.get("bullets", []):
                self.set_x(20)
                self.multi_cell(175, 4.8, _li(bullet))

            if exp.get("keyImpact"):
                self.set_x(20)
                self.set_fill_color(238, 242, 255)
                self.set_text_color(67, 56, 202)
                self.set_font("Helvetica", "I", 8.5)
                self.multi_cell(175, 4.5, _li(exp["keyImpact"]), fill=True)
            self.ln(2)

        # ── Education & Certifications side-by-side ───────────
        self._section("EDUCATION & CERTIFICATIONS")
        y_start = self.get_y()

        # Left: Education
        self.set_xy(15, y_start)
        self.set_text_color(99, 102, 241)
        self.set_font("Helvetica", "B", 8.5)
        self.cell(85, 5, "EDUCATION", ln=True)
        for edu in cv.get("education", []):
            self.set_x(15)
            self.set_text_color(15, 23, 42)
            self.set_font("Helvetica", "B", 9)
            self.multi_cell(85, 4.5, _safe(edu.get("degree", "")))
            self.set_x(15)
            self.set_text_color(100, 116, 139)
            self.set_font("Helvetica", "", 8)
            self.multi_cell(
                85, 4, _safe(edu.get("institution", "")) + "  " + _safe(edu.get("year", ""))
            )
            self.ln(1)
        left_y = self.get_y()

        # Right: Certifications
        self.set_xy(105, y_start)
        self.set_text_color(99, 102, 241)
        self.set_font("Helvetica", "B", 8.5)
        self.cell(90, 5, "CERTIFICATIONS", ln=True)
        right_y = y_start + 6
        for cert in cv.get("certifications", []):
            self.set_xy(105, right_y)
            self.set_text_color(55, 65, 81)
            self.set_font("Helvetica", "", 8)
            self.multi_cell(90, 4, _li(cert))
            right_y = self.get_y() + 0.5

        self.set_y(max(left_y, right_y))
        self.ln(3)

        # ── Key Achievements ──────────────────────────────────
        if cv.get("keyAchievements"):
            self._section("KEY ACHIEVEMENTS")
            for ach in cv["keyAchievements"]:
                self.set_x(15)
                self.set_fill_color(243, 244, 246)
                self.set_text_color(55, 65, 81)
                self.set_font("Helvetica", "", 9)
                self.multi_cell(self.usable_width, 5, _li(ach), fill=True)
                self.ln(1)

        return bytes(self.output())

    def _section(self, title: str):
        self.ln(3)
        self.set_x(15)
        self.set_text_color(99, 102, 241)
        self.set_font("Helvetica", "B", 8.5)
        self.cell(self.usable_width, 5, title, ln=True)
        self.set_draw_color(226, 232, 240)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(2)


class ClassicCV(FPDF):
    """Classic CV: traditional centered layout, ATS-safe."""

    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=15)
        self.set_margins(left=20, top=18, right=20)
        self.usable_width = 170

    def render(self, cv: dict) -> bytes:
        self.add_page()
        self.set_text_color(17, 24, 39)

        self.set_font("Times", "B", 20)
        self.cell(self.usable_width, 10, _safe(cv.get("name", "")).upper(), ln=True, align="C")
        self.set_font("Times", "I", 11)
        self.cell(self.usable_width, 6, _safe(cv.get("tagline", "")), ln=True, align="C")
        self.set_font("Times", "", 9)
        contact = "  |  ".join(
            filter(
                None,
                [
                    _safe(cv.get("email", "")),
                    _safe(cv.get("phone", "")),
                    _safe(cv.get("location", "")),
                ],
            )
        )
        self.cell(self.usable_width, 5, contact, ln=True, align="C")
        self.ln(2)
        self.line(20, self.get_y(), 190, self.get_y())
        self.ln(4)

        self.set_x(20)
        self.set_font("Times", "", 10)
        self.multi_cell(self.usable_width, 5.5, _safe(cv.get("summary", "")))
        self.ln(2)

        self._section("CORE COMPETENCIES")
        self.set_x(20)
        self.set_font("Times", "", 10)
        self.multi_cell(
            self.usable_width, 5, "  ·  ".join(_safe(s) for s in cv.get("coreSkills", []))
        )

        self._section("PROFESSIONAL EXPERIENCE")
        for exp in cv.get("experience", []):
            self.set_x(20)
            self.set_font("Times", "B", 11)
            title_co = _safe(exp.get("title", "")) + "  |  " + _safe(exp.get("company", ""))
            self.cell(125, 5.5, title_co[:65])
            self.set_font("Times", "I", 10)
            self.cell(45, 5.5, _safe(exp.get("duration", "")), ln=True, align="R")
            self.set_font("Times", "", 10)
            for bullet in exp.get("bullets", []):
                self.set_x(20)
                self.multi_cell(self.usable_width, 5, "  " + _li(bullet))
            self.ln(1)

        self._section("EDUCATION")
        for edu in cv.get("education", []):
            self.set_x(20)
            self.set_font("Times", "B", 10)
            self.cell(self.usable_width, 5, _safe(edu.get("degree", "")), ln=True)
            self.set_x(20)
            self.set_font("Times", "", 10)
            self.cell(
                self.usable_width,
                5,
                _safe(edu.get("institution", "")) + "  -  " + _safe(edu.get("year", "")),
                ln=True,
            )
            self.ln(1)

        self._section("CERTIFICATIONS")
        self.set_font("Times", "", 10)
        for cert in cv.get("certifications", []):
            self.set_x(20)
            self.multi_cell(self.usable_width, 5, _li(cert))

        if cv.get("keyAchievements"):
            self._section("KEY ACHIEVEMENTS")
            self.set_font("Times", "", 10)
            for ach in cv["keyAchievements"]:
                self.set_x(20)
                self.multi_cell(self.usable_width, 5, _li(ach))

        return bytes(self.output())

    def _section(self, title: str):
        self.ln(3)
        self.set_x(20)
        self.set_font("Times", "B", 11)
        self.cell(self.usable_width, 6, title, ln=True)
        self.line(20, self.get_y(), 190, self.get_y())
        self.ln(2)


class ExecutiveCV(FPDF):
    """Executive CV: navy header, gold accents, formal serif style."""

    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=15)
        self.set_margins(left=18, top=10, right=18)
        self.usable_width = 174

    def render(self, cv: dict) -> bytes:
        self.add_page()

        # Navy header
        self.set_fill_color(30, 58, 95)
        self.rect(0, 0, 210, 48, "F")

        # Gold accent stripe
        self.set_fill_color(201, 168, 76)
        self.rect(0, 48, 210, 2.5, "F")

        self.set_xy(18, 13)
        self.set_text_color(255, 255, 255)
        self.set_font("Times", "B", 22)
        self.cell(140, 9, _safe(cv.get("name", "")), ln=True)

        self.set_xy(18, 26)
        self.set_text_color(201, 168, 76)
        self.set_font("Times", "I", 11)
        self.cell(140, 6, _safe(cv.get("tagline", "")), ln=True)

        self.set_xy(18, 37)
        self.set_text_color(180, 200, 220)
        self.set_font("Times", "", 9)
        contact = "  |  ".join(
            filter(
                None,
                [
                    _safe(cv.get("email", "")),
                    _safe(cv.get("phone", "")),
                    _safe(cv.get("location", "")),
                    _safe(cv.get("linkedin", "")),
                ],
            )
        )
        self.cell(140, 5, contact, ln=True)

        self.set_y(58)

        self.set_x(18)
        self.set_text_color(55, 65, 81)
        self.set_font("Times", "", 10.5)
        self.multi_cell(self.usable_width, 5.5, _safe(cv.get("summary", "")))

        self._section("CORE COMPETENCIES")
        skill_text = "   |   ".join(_safe(s) for s in cv.get("coreSkills", []))
        self.set_x(18)
        self.set_text_color(55, 65, 81)
        self.set_font("Times", "", 10)
        self.multi_cell(self.usable_width, 5, skill_text)
        self.ln(1)

        self._section("PROFESSIONAL EXPERIENCE")
        for exp in cv.get("experience", []):
            self.set_x(18)
            self.set_text_color(30, 58, 95)
            self.set_font("Times", "B", 11)
            self.cell(120, 6, _safe(exp.get("title", ""))[:55])
            self.set_text_color(100, 116, 139)
            self.set_font("Times", "I", 10)
            self.cell(54, 6, _safe(exp.get("duration", "")), ln=True, align="R")

            self.set_x(18)
            self.set_text_color(201, 168, 76)
            self.set_font("Times", "I", 10)
            co_loc = _safe(exp.get("company", ""))
            if exp.get("location"):
                co_loc += "  -  " + _safe(exp.get("location", ""))
            self.cell(self.usable_width, 5, co_loc, ln=True)

            self.set_text_color(55, 65, 81)
            self.set_font("Times", "", 10)
            for bullet in exp.get("bullets", []):
                self.set_x(23)
                self.multi_cell(169, 5, _li(bullet))

            if exp.get("keyImpact"):
                self.set_x(23)
                self.set_fill_color(254, 249, 231)
                self.set_text_color(120, 90, 20)
                self.set_font("Times", "I", 9)
                self.multi_cell(169, 4.5, _li(exp["keyImpact"]), fill=True)
            self.ln(2)

        self._section("EDUCATION & CERTIFICATIONS")
        y_start = self.get_y()

        self.set_xy(18, y_start)
        self.set_text_color(30, 58, 95)
        self.set_font("Times", "B", 9)
        self.cell(83, 5, "EDUCATION", ln=True)
        for edu in cv.get("education", []):
            self.set_x(18)
            self.set_text_color(17, 24, 39)
            self.set_font("Times", "B", 10)
            self.multi_cell(83, 4.5, _safe(edu.get("degree", "")))
            self.set_x(18)
            self.set_text_color(100, 116, 139)
            self.set_font("Times", "", 9)
            self.multi_cell(
                83, 4, _safe(edu.get("institution", "")) + "  " + _safe(edu.get("year", ""))
            )
            self.ln(1)
        left_y = self.get_y()

        self.set_xy(108, y_start)
        self.set_text_color(30, 58, 95)
        self.set_font("Times", "B", 9)
        self.cell(84, 5, "CERTIFICATIONS", ln=True)
        right_y = y_start + 6
        for cert in cv.get("certifications", []):
            self.set_xy(108, right_y)
            self.set_text_color(55, 65, 81)
            self.set_font("Times", "", 9)
            self.multi_cell(84, 4, _li(cert))
            right_y = self.get_y() + 0.5

        self.set_y(max(left_y, right_y) + 3)

        if cv.get("keyAchievements"):
            self._section("KEY ACHIEVEMENTS")
            for ach in cv["keyAchievements"]:
                self.set_x(18)
                self.set_fill_color(252, 248, 240)
                self.set_text_color(55, 65, 81)
                self.set_font("Times", "", 9.5)
                self.multi_cell(self.usable_width, 5, _li(ach), fill=True)
                self.ln(1)

        return bytes(self.output())

    def _section(self, title: str):
        self.ln(3)
        self.set_x(18)
        self.set_text_color(201, 168, 76)
        self.set_font("Times", "B", 10)
        self.cell(self.usable_width, 5.5, title, ln=True)
        self.set_draw_color(201, 168, 76)
        self.line(18, self.get_y(), 192, self.get_y())
        self.ln(2)


class MinimalCV(FPDF):
    """Minimal CV: pure white, no colour accents, maximum readability."""

    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=12)
        self.set_margins(left=18, top=15, right=18)
        self.usable_width = 174

    def render(self, cv: dict) -> bytes:
        self.add_page()
        self.set_text_color(17, 24, 39)

        self.set_font("Helvetica", "B", 20)
        self.cell(145, 9, _safe(cv.get("name", "")), ln=True)

        self.set_font("Helvetica", "", 10)
        self.set_text_color(75, 85, 99)
        self.cell(145, 5.5, _safe(cv.get("tagline", "")), ln=True)

        self.set_font("Helvetica", "", 8.5)
        self.set_text_color(107, 114, 128)
        contact = "  |  ".join(
            filter(
                None,
                [
                    _safe(cv.get("email", "")),
                    _safe(cv.get("phone", "")),
                    _safe(cv.get("location", "")),
                    _safe(cv.get("linkedin", "")),
                ],
            )
        )
        self.cell(145, 4.5, contact, ln=True)
        self.ln(2)

        self.set_draw_color(209, 213, 219)
        self.line(18, self.get_y(), 192, self.get_y())
        self.ln(4)

        self.set_x(18)
        self.set_text_color(55, 65, 81)
        self.set_font("Helvetica", "", 10)
        self.multi_cell(self.usable_width, 5.2, _safe(cv.get("summary", "")))

        self._section("SKILLS")
        self.set_x(18)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(55, 65, 81)
        self.multi_cell(
            self.usable_width, 5, "  -  ".join(_safe(s) for s in cv.get("coreSkills", []))
        )

        self._section("EXPERIENCE")
        for exp in cv.get("experience", []):
            self.set_x(18)
            self.set_text_color(17, 24, 39)
            self.set_font("Helvetica", "B", 10.5)
            self.cell(120, 5.5, _safe(exp.get("title", ""))[:55])
            self.set_text_color(107, 114, 128)
            self.set_font("Helvetica", "", 9)
            self.cell(54, 5.5, _safe(exp.get("duration", "")), ln=True, align="R")

            self.set_x(18)
            self.set_text_color(75, 85, 99)
            self.set_font("Helvetica", "I", 9.5)
            co_loc = _safe(exp.get("company", ""))
            if exp.get("location"):
                co_loc += "  -  " + _safe(exp.get("location", ""))
            self.cell(self.usable_width, 4.5, co_loc, ln=True)

            self.set_text_color(55, 65, 81)
            self.set_font("Helvetica", "", 9)
            for bullet in exp.get("bullets", []):
                self.set_x(23)
                self.multi_cell(169, 4.8, _li(bullet))

            if exp.get("keyImpact"):
                self.set_x(23)
                self.set_text_color(75, 85, 99)
                self.set_font("Helvetica", "I", 8.5)
                self.multi_cell(169, 4.5, "  " + _lt(exp["keyImpact"]))
            self.ln(2)

        self._section("EDUCATION")
        for edu in cv.get("education", []):
            self.set_x(18)
            self.set_text_color(17, 24, 39)
            self.set_font("Helvetica", "B", 9.5)
            self.cell(self.usable_width, 5, _safe(edu.get("degree", "")), ln=True)
            self.set_x(18)
            self.set_text_color(107, 114, 128)
            self.set_font("Helvetica", "", 9)
            self.cell(
                self.usable_width,
                4.5,
                _safe(edu.get("institution", "")) + "  " + _safe(edu.get("year", "")),
                ln=True,
            )
            self.ln(1)

        self._section("CERTIFICATIONS")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(55, 65, 81)
        for cert in cv.get("certifications", []):
            self.set_x(18)
            self.multi_cell(self.usable_width, 4.8, _li(cert))

        if cv.get("keyAchievements"):
            self._section("KEY ACHIEVEMENTS")
            self.set_font("Helvetica", "", 9)
            for ach in cv["keyAchievements"]:
                self.set_x(18)
                self.multi_cell(self.usable_width, 5, _li(ach))

        return bytes(self.output())

    def _section(self, title: str):
        self.ln(3)
        self.set_x(18)
        self.set_text_color(17, 24, 39)
        self.set_font("Helvetica", "B", 8)
        self.cell(self.usable_width, 5, title, ln=True)
        self.set_draw_color(209, 213, 219)
        self.line(18, self.get_y(), 192, self.get_y())
        self.ln(2)


class TechCV(FPDF):
    """Tech CV: dark charcoal header, teal accents, contemporary feel."""

    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=12)
        self.set_margins(left=15, top=10, right=15)
        self.usable_width = 180

    def render(self, cv: dict) -> bytes:
        self.add_page()

        # Charcoal header
        self.set_fill_color(31, 41, 55)
        self.rect(0, 0, 210, 44, "F")

        # Teal accent stripe
        self.set_fill_color(13, 148, 136)
        self.rect(0, 44, 210, 3, "F")

        self.set_xy(15, 12)
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 21)
        self.cell(145, 8, _safe(cv.get("name", "")), ln=True)

        self.set_xy(15, 23)
        self.set_text_color(13, 148, 136)
        self.set_font("Helvetica", "", 10)
        self.cell(145, 5, _safe(cv.get("tagline", "")), ln=True)

        self.set_xy(15, 33)
        self.set_text_color(156, 163, 175)
        self.set_font("Helvetica", "", 8.5)
        contact = "  |  ".join(
            filter(
                None,
                [
                    _safe(cv.get("email", "")),
                    _safe(cv.get("phone", "")),
                    _safe(cv.get("location", "")),
                    _safe(cv.get("linkedin", "")),
                ],
            )
        )
        self.cell(145, 4.5, contact, ln=True)

        self.set_y(54)

        self.set_x(15)
        self.set_text_color(55, 65, 81)
        self.set_font("Helvetica", "", 10)
        self.multi_cell(self.usable_width, 5.5, _safe(cv.get("summary", "")))

        self._section("TECHNICAL SKILLS")
        skill_text = "   //   ".join(_safe(s) for s in cv.get("coreSkills", []))
        self.set_x(15)
        self.set_text_color(55, 65, 81)
        self.set_font("Helvetica", "", 9)
        self.multi_cell(self.usable_width, 5, skill_text)
        self.ln(1)

        self._section("EXPERIENCE")
        for exp in cv.get("experience", []):
            self.set_x(15)
            self.set_text_color(31, 41, 55)
            self.set_font("Helvetica", "B", 11)
            self.cell(130, 6, _safe(exp.get("title", ""))[:55])
            self.set_text_color(107, 114, 128)
            self.set_font("Helvetica", "", 9)
            self.cell(50, 6, _safe(exp.get("duration", "")), ln=True, align="R")

            self.set_x(15)
            self.set_text_color(13, 148, 136)
            self.set_font("Helvetica", "I", 10)
            co_loc = _safe(exp.get("company", ""))
            if exp.get("location"):
                co_loc += "  //  " + _safe(exp.get("location", ""))
            self.cell(self.usable_width, 5, co_loc, ln=True)

            self.set_text_color(55, 65, 81)
            self.set_font("Helvetica", "", 9)
            for bullet in exp.get("bullets", []):
                self.set_x(20)
                self.multi_cell(175, 4.8, ">>  " + _lt(bullet))

            if exp.get("keyImpact"):
                self.set_x(20)
                self.set_fill_color(236, 253, 251)
                self.set_text_color(15, 118, 110)
                self.set_font("Helvetica", "I", 8.5)
                self.multi_cell(175, 4.5, _li(exp["keyImpact"]), fill=True)
            self.ln(2)

        self._section("EDUCATION & CERTIFICATIONS")
        y_start = self.get_y()

        self.set_xy(15, y_start)
        self.set_text_color(13, 148, 136)
        self.set_font("Helvetica", "B", 8.5)
        self.cell(85, 5, "EDUCATION", ln=True)
        for edu in cv.get("education", []):
            self.set_x(15)
            self.set_text_color(17, 24, 39)
            self.set_font("Helvetica", "B", 9)
            self.multi_cell(85, 4.5, _safe(edu.get("degree", "")))
            self.set_x(15)
            self.set_text_color(107, 114, 128)
            self.set_font("Helvetica", "", 8)
            self.multi_cell(
                85, 4, _safe(edu.get("institution", "")) + "  " + _safe(edu.get("year", ""))
            )
            self.ln(1)
        left_y = self.get_y()

        self.set_xy(105, y_start)
        self.set_text_color(13, 148, 136)
        self.set_font("Helvetica", "B", 8.5)
        self.cell(90, 5, "CERTIFICATIONS", ln=True)
        right_y = y_start + 6
        for cert in cv.get("certifications", []):
            self.set_xy(105, right_y)
            self.set_text_color(55, 65, 81)
            self.set_font("Helvetica", "", 8)
            self.multi_cell(90, 4, ">>  " + _lt(cert))
            right_y = self.get_y() + 0.5

        self.set_y(max(left_y, right_y) + 3)

        if cv.get("keyAchievements"):
            self._section("KEY ACHIEVEMENTS")
            for ach in cv["keyAchievements"]:
                self.set_x(15)
                self.set_fill_color(236, 253, 251)
                self.set_text_color(15, 118, 110)
                self.set_font("Helvetica", "", 9)
                self.multi_cell(self.usable_width, 5, _li(ach), fill=True)
                self.ln(1)

        return bytes(self.output())

    def _section(self, title: str):
        self.ln(3)
        self.set_x(15)
        self.set_text_color(13, 148, 136)
        self.set_font("Helvetica", "B", 8.5)
        self.cell(self.usable_width, 5, title, ln=True)
        self.set_draw_color(167, 243, 208)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(2)


class SidebarCV(FPDF):
    """Sidebar CV: dark navy left panel, two-column layout."""

    SW = 65  # sidebar width mm
    BX = 70  # body column X start mm
    BW = 132  # body column width mm

    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=False, margin=0)
        self.set_margins(left=0, top=0, right=0)

    def render(self, cv: dict) -> bytes:
        self.add_page()

        # Full-height sidebar
        self.set_fill_color(15, 23, 42)
        self.rect(0, 0, self.SW, 297, "F")

        # Indigo accent stripe along sidebar edge
        self.set_fill_color(99, 102, 241)
        self.rect(self.SW, 0, 2, 297, "F")

        # ── SIDEBAR CONTENT ──────────────────────────────────
        sy = 12

        # Name — word by word to stay in sidebar
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 13)
        name_parts = _safe(cv.get("name", "")).split()
        line = ""
        for part in name_parts:
            test = (line + " " + part).strip()
            if len(test) > 16 and line:
                self.set_xy(5, sy)
                self.cell(58, 6, line, ln=True)
                sy += 6
                line = part
            else:
                line = test
        if line:
            self.set_xy(5, sy)
            self.cell(58, 6, line, ln=True)
            sy += 6
        sy += 1

        # Tagline
        tagline_words = _safe(cv.get("tagline", "")).split()
        tline = ""
        self.set_text_color(99, 102, 241)
        self.set_font("Helvetica", "", 7.5)
        for w in tagline_words:
            test = (tline + " " + w).strip()
            if len(test) > 25 and tline:
                self.set_xy(5, sy)
                self.cell(58, 4.5, tline, ln=True)
                sy += 4.5
                tline = w
            else:
                tline = test
        if tline:
            self.set_xy(5, sy)
            self.cell(58, 4.5, tline, ln=True)
            sy += 4.5
        sy += 3

        def sb_section(title, y):
            self.set_xy(5, y)
            self.set_text_color(99, 102, 241)
            self.set_font("Helvetica", "B", 8)
            self.cell(58, 5, title, ln=True)
            self.set_draw_color(40, 55, 100)
            self.line(5, y + 5.5, 63, y + 5.5)
            return y + 8

        # Contact
        sy = sb_section("CONTACT", sy)
        for item in filter(None, [cv.get("email"), cv.get("phone"), cv.get("location")]):
            if sy > 270:
                break
            txt = _safe(item)
            if len(txt) > 30:
                txt = txt[:28] + ".."
            self.set_xy(5, sy)
            self.set_text_color(148, 163, 184)
            self.set_font("Helvetica", "", 7)
            self.cell(58, 4.5, txt, ln=True)
            sy += 4.8
        sy += 2

        # Skills
        sy = sb_section("SKILLS", sy)
        for skill in cv.get("coreSkills", []):
            if sy > 230:
                break
            txt = _safe(skill)
            if len(txt) > 30:
                txt = txt[:28] + ".."
            self.set_xy(5, sy)
            self.set_fill_color(25, 40, 75)
            self.set_text_color(203, 213, 225)
            self.set_font("Helvetica", "", 7.5)
            self.cell(58, 5.5, txt, ln=True, fill=True)
            sy += 6
        sy += 2

        # Certifications
        certs = cv.get("certifications", [])
        if certs and sy < 245:
            sy = sb_section("CERTIFICATIONS", sy)
            for cert in certs[:5]:
                if sy > 268:
                    break
                txt = _lt(cert)
                if len(txt) > 36:
                    txt = txt[:34] + ".."
                self.set_xy(5, sy)
                self.set_text_color(148, 163, 184)
                self.set_font("Helvetica", "", 6.5)
                self.cell(58, 4.2, txt, ln=True)
                sy += 4.5

        # ── BODY (RIGHT COLUMN) ──────────────────────────────
        by = 14

        self.set_xy(self.BX, by)
        self.set_text_color(55, 65, 81)
        self.set_font("Helvetica", "", 9.5)
        self.multi_cell(self.BW, 5.2, _safe(cv.get("summary", "")))
        by = self.get_y() + 4

        def body_section(title, y):
            if y > 270:
                return y
            self.set_xy(self.BX, y)
            self.set_text_color(99, 102, 241)
            self.set_font("Helvetica", "B", 8.5)
            self.cell(self.BW, 5, title, ln=True)
            self.set_draw_color(226, 232, 240)
            self.line(self.BX, self.get_y(), 207, self.get_y())
            return self.get_y() + 3

        by = body_section("EXPERIENCE", by)
        for exp in cv.get("experience", []):
            if by > 265:
                break
            self.set_xy(self.BX, by)
            self.set_text_color(15, 23, 42)
            self.set_font("Helvetica", "B", 10.5)
            self.cell(self.BW - 42, 5.5, _safe(exp.get("title", ""))[:42])
            self.set_text_color(107, 114, 128)
            self.set_font("Helvetica", "", 8.5)
            self.cell(42, 5.5, _safe(exp.get("duration", "")), ln=True, align="R")
            by = self.get_y()

            self.set_xy(self.BX, by)
            self.set_text_color(99, 102, 241)
            self.set_font("Helvetica", "I", 9.5)
            co_loc = _safe(exp.get("company", ""))
            if exp.get("location"):
                co_loc += "  -  " + _safe(exp.get("location", ""))
            self.cell(self.BW, 4.5, co_loc, ln=True)
            by = self.get_y()

            self.set_text_color(55, 65, 81)
            self.set_font("Helvetica", "", 8.5)
            for bullet in exp.get("bullets", []):
                if by > 265:
                    break
                self.set_xy(self.BX + 3, by)
                self.multi_cell(self.BW - 3, 4.5, _li(bullet))
                by = self.get_y()

            if exp.get("keyImpact") and by < 260:
                self.set_xy(self.BX + 3, by)
                self.set_fill_color(238, 242, 255)
                self.set_text_color(67, 56, 202)
                self.set_font("Helvetica", "I", 8)
                self.multi_cell(self.BW - 3, 4.2, _li(exp["keyImpact"]), fill=True)
                by = self.get_y()
            by += 2

        if by < 255:
            by = body_section("EDUCATION", by)
            for edu in cv.get("education", []):
                if by > 270:
                    break
                self.set_xy(self.BX, by)
                self.set_text_color(15, 23, 42)
                self.set_font("Helvetica", "B", 9.5)
                self.cell(self.BW, 5, _safe(edu.get("degree", "")), ln=True)
                by = self.get_y()
                self.set_xy(self.BX, by)
                self.set_text_color(107, 114, 128)
                self.set_font("Helvetica", "", 8.5)
                self.cell(
                    self.BW,
                    4.5,
                    _safe(edu.get("institution", "")) + "  " + _safe(edu.get("year", "")),
                    ln=True,
                )
                by = self.get_y() + 2

        if cv.get("keyAchievements") and by < 250:
            by = body_section("KEY ACHIEVEMENTS", by)
            for ach in cv["keyAchievements"]:
                if by > 268:
                    break
                self.set_xy(self.BX, by)
                self.set_fill_color(243, 244, 246)
                self.set_text_color(55, 65, 81)
                self.set_font("Helvetica", "", 9)
                self.multi_cell(self.BW, 4.8, _li(ach), fill=True)
                by = self.get_y() + 1

        return bytes(self.output())


TEMPLATE_META = {
    "modern": {
        "label": "Modern",
        "desc": "Dark header, indigo accents",
        "hdr": "#0f172a",
        "acc": "#6366f1",
    },
    "classic": {
        "label": "Classic",
        "desc": "Traditional, ATS-safe, serif",
        "hdr": "#f8f8f8",
        "acc": "#1a1a1a",
    },
    "executive": {
        "label": "Executive",
        "desc": "Navy header, gold accents",
        "hdr": "#1e3a5f",
        "acc": "#c9a84c",
    },
    "minimal": {
        "label": "Minimal",
        "desc": "Pure white, no colour, ultra-clean",
        "hdr": "#ffffff",
        "acc": "#374151",
    },
    "tech": {
        "label": "Tech",
        "desc": "Charcoal header, teal accents",
        "hdr": "#1f2937",
        "acc": "#0d9488",
    },
    "sidebar": {
        "label": "Sidebar",
        "desc": "Two-column dark left panel",
        "hdr": "#0f172a",
        "acc": "#6366f1",
    },
}


def generate_cv_pdf(cv_data: dict[str, Any], template: str) -> bytes:
    mapping = {
        "modern": ModernCV,
        "classic": ClassicCV,
        "executive": ExecutiveCV,
        "minimal": MinimalCV,
        "tech": TechCV,
        "sidebar": SidebarCV,
    }
    cls = mapping.get(template, ModernCV)
    return cls().render(cv_data)
