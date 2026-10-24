"""`./run cutover-check` — the judgments, handed answers rather than the network.

Each judgment is the one place a wrong answer from a dashboard shows up before
a user meets it, so each is pinned on the answers that matter: what V1 looks
like from outside, a V2 build that is not the current one, a preflight that
does not name our origin.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

# The check is a script, not a package module.
_SPEC = importlib.util.spec_from_file_location(
    "cutover_check", Path(__file__).resolve().parents[1] / "scripts" / "cutover_check.py"
)
assert _SPEC and _SPEC.loader
check = importlib.util.module_from_spec(_SPEC)
sys.modules["cutover_check"] = check
_SPEC.loader.exec_module(check)

Mark = check.Mark
Phase = check.Phase
Response = check.Response
Unreachable = check.Unreachable

#: What app.carigma.in/build.json answered on 24 Oct: Streamlit's index page,
#: which it serves for every path.
STREAMLIT = Response(200, "<!--\n Copyright (c) Streamlit Inc. (2018-2022) ...", {})
SPA = Response(200, '<!doctype html><html><body><div id="root"></div></body></html>', {})


def _build(commit: str) -> object:
    return Response(200, json.dumps({"commit": commit, "branch": "main"}), {})


# ── app domain ─────────────────────────────────────────────────────────────


def test_v1_on_the_domain_is_expected_before_the_switch() -> None:
    result = check.judge_app_domain(STREAMLIT, "ba8c9a266e02", Phase.BEFORE)

    assert result.mark is Mark.NOTE
    assert "still V1" in result.summary


def test_v1_on_the_domain_after_the_switch_fails() -> None:
    result = check.judge_app_domain(STREAMLIT, "ba8c9a266e02", Phase.AFTER)

    assert result.mark is Mark.FAIL
    assert "DNS" in result.summary


def test_the_domain_passes_only_with_the_webs_exact_build() -> None:
    assert check.judge_app_domain(_build("ba8c9a266e02"), "ba8c9a266e02", Phase.AFTER).mark is (
        Mark.OK
    )


def test_a_v2_build_that_is_not_the_current_one_fails_in_either_phase() -> None:
    """A second V2 service, or a stale one, holding the domain would pass any
    check that only asked "is this V2"."""
    for phase in Phase:
        result = check.judge_app_domain(_build("0ld0ld0ld0ld"), "ba8c9a266e02", phase)
        assert result.mark is Mark.FAIL
        assert "not the one" in result.summary


def test_a_build_report_saying_unknown_is_not_a_build() -> None:
    assert check.judge_web(_build("unknown")).mark is Mark.FAIL


@pytest.mark.parametrize(
    "answer",
    [Unreachable("TLS: certificate verify failed"), Response(404, "", {}), Response(502, "", {})],
)
def test_neither_v1_nor_v2_fails_and_says_what_came_back(answer: object) -> None:
    result = check.judge_app_domain(answer, "ba8c9a266e02", Phase.BEFORE)

    assert result.mark is Mark.FAIL
    assert "neither" in result.summary


# ── deep links, cors, the way back, legal ──────────────────────────────────


def test_a_deep_link_that_is_not_the_app_fails_naming_the_path() -> None:
    result = check.judge_deep_links(
        check.APP, {"/auth/reset": Response(404, "Not Found", {}), "/today": SPA}
    )

    assert result.mark is Mark.FAIL
    assert "/auth/reset HTTP 404" in result.summary


def test_deep_links_pass_when_every_path_serves_the_app() -> None:
    assert check.judge_deep_links(check.WEB, {"/auth/reset": SPA, "/today": SPA}).mark is Mark.OK


def test_cors_passes_only_when_the_api_names_the_new_origin() -> None:
    named = Response(200, "OK", {"access-control-allow-origin": "https://app.carigma.in"})
    other = Response(200, "OK", {"access-control-allow-origin": "https://carigma-web.onrender.com"})
    # Starlette refuses a preflight from an origin it does not allow with 400
    # and no allow-origin header.
    refused = Response(400, "Disallowed CORS origin", {})

    assert check.judge_cors(named).mark is Mark.OK
    assert check.judge_cors(other).mark is Mark.FAIL
    assert "CORS_ORIGINS" in check.judge_cors(refused).summary


def test_no_way_back_means_do_not_switch() -> None:
    result = check.judge_v1(Unreachable("timed out"))

    assert result.mark is Mark.FAIL
    assert "do not switch" in result.summary


def test_a_missing_legal_page_fails() -> None:
    result = check.judge_legal({"/terms.html": SPA, "/privacy.html": Response(404, "", {})})

    assert result.mark is Mark.FAIL
    assert "/privacy.html HTTP 404" in result.summary


# ── the report ─────────────────────────────────────────────────────────────


def test_a_note_does_not_fail_the_run_and_a_failure_does() -> None:
    note = check.Result("app domain", Mark.NOTE, "still V1")
    fail = check.Result("cors", Mark.FAIL, "not allowed")

    assert check.report([note], Phase.BEFORE, io.StringIO()) is True
    assert check.report([note, fail], Phase.BEFORE, io.StringIO()) is False


def test_the_report_prints_on_a_windows_console_and_lists_what_it_cannot_see() -> None:
    """Same rule as `deployed.py`: a cp1252 console must be able to print the
    failures. `strict`, so a regression crashes here rather than in mojibake."""
    results = [
        check.judge_app_domain(STREAMLIT, "abc", Phase.AFTER),
        check.judge_cors(Response(400, "", {})),
        check.judge_v1(Unreachable("timed out")),
        check.judge_legal({"/terms.html": Response(404, "", {})}),
        check.judge_deep_links(check.APP, {"/today": Response(404, "", {})}),
    ]
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")

    passed = check.report(results, Phase.AFTER, console)
    console.flush()

    assert passed is False
    text = raw.getvalue().decode("cp1252")
    assert "The switch is NOT complete." in text
    assert "Supabase > Auth > URL configuration" in text
