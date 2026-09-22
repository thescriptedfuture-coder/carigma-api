"""`scripts/smoke.py` — the parts that can be checked without production.

The script itself is the missing layer: both suites run against fakes, so
nothing before it had ever watched the deployed API answer a real user. These
cover the pieces that would otherwise only fail in front of somebody: the PDF it
uploads, the surfaces it claims to cover, and its own report.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from carigma_api.services import extraction

ROOT = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location("smoke", ROOT / "scripts" / "smoke.py")
assert _SPEC and _SPEC.loader
smoke = importlib.util.module_from_spec(_SPEC)
sys.modules["smoke"] = smoke
_SPEC.loader.exec_module(smoke)


def test_the_pdf_it_uploads_is_one_the_api_can_read() -> None:
    """Built in the script, read here by the REAL extractor. A malformed PDF
    would fail on production as "we couldn't read that file" and be reported as
    a product defect.

    Scope, measured by breaking it: deleting the xref table entirely still
    passes, because `pdfplumber` reconstructs one. So this proves the file is
    READABLE, not that its structure is correct — which is the property that
    matters here, and the stronger claim is not available from this test.
    """
    text = extraction.text_from_upload(smoke._pdf(smoke.SMOKE_PROFILE), "smoke-profile.pdf")

    assert "Senior Data Analyst" in text
    assert "SMOKE TEST PROFILE" in text, "the upload must be recognisable as test data"


def test_the_upload_says_it_is_not_a_real_person() -> None:
    """It lands on a real profile row in the live database. Anyone reading that
    row later should be able to tell where it came from."""
    assert "not a real person" in smoke.SMOKE_PROFILE[0].lower()


def test_it_reads_the_surfaces_the_client_reads() -> None:
    """Derived from `contract_keys.json`, which records what the API actually
    answered, so a new user-facing GET cannot quietly go unchecked."""
    recorded = {
        path
        for key in json.loads((ROOT / "contract_keys.json").read_text(encoding="utf-8"))
        for method, path in [key.split(" ", 1)]
        if method == "GET"
    }
    # Not a user's surfaces: admin screens, the auth probes, and the two that
    # are reached by a token in an email rather than by a signed-in session.
    not_a_users = {
        path
        for path in recorded
        if path.startswith(("/admin", "/auth/")) or path.startswith("/unsubscribe/") or "{}" in path
    }
    covered = {path for path, _key in smoke.READS}

    missed = sorted(recorded - not_a_users - covered)
    assert missed == [], f"a signed-in user can reach these and the smoke test does not: {missed}"


def test_every_surface_it_names_is_one_the_api_serves() -> None:
    recorded = {
        key.split(" ", 1)[1]
        for key in json.loads((ROOT / "contract_keys.json").read_text(encoding="utf-8"))
    }
    unknown = sorted({path for path, _key in smoke.READS} - recorded)

    assert unknown == [], f"the smoke test reads paths the API does not serve: {unknown}"


def test_a_200_with_the_wrong_shape_is_a_failure_not_a_pass() -> None:
    """The failure this exists for. `/today` answering 200 with a body the web
    cannot read looks healthy to a status check and empty to a person."""
    checks = [
        smoke.Check("GET /today", False, "200 but no 'primary_action' — got ['detail']", 120),
    ]

    assert smoke.report(checks, io.StringIO()) is False


def test_the_report_prints_failures_on_a_windows_console() -> None:
    """Strict cp1252: `deployed.py` once crashed printing exactly the verdicts
    it existed to deliver, and this runs on the same console."""
    checks = [
        smoke.Check("GET /today", True, "200", 340),
        smoke.Check("POST /onboarding/extract", False, "503: we couldn't save your setup", 900),
    ]

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    passed = smoke.report(checks, console)
    console.flush()

    assert passed is False
    assert b"FAIL" in raw.getvalue()


def test_it_refuses_to_run_without_credentials() -> None:
    """Rather than reporting every surface as broken, which is what an
    unauthenticated run would look like."""
    from carigma_api.config import Settings

    with pytest.raises(SystemExit) as caught:
        smoke.sign_in(Settings(smoke_email="", smoke_password=""))

    assert "SMOKE_EMAIL" in str(caught.value)
