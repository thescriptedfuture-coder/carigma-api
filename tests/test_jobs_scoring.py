"""Ranking real postings, and refusing to invent a score for one it cannot.

V1's ranking pass falls back to **70% and a sentence nobody wrote** for any job
the model did not return:

    "matchScore": int(a.get("matchScore") or 70),
    "whyMatch":   a.get("whyMatch") or
                  "A real, current opening matching your target role and location."

Its own comment records that firing in the first live run, when the JSON array
truncated and every job came back at 70. These tests are the port refusing to
carry it.
"""

from __future__ import annotations

from typing import Any

from carigma_api.services.jobs.models import NormalizedJob
from carigma_api.services.jobs.scoring import Annotation, merge, score

PROFILE = {
    "name": "Ravi Kumar",
    "currentRole": "Business Analyst",
    "targetRoles": "Data Analyst",
    "skills": "SQL, Power BI",
    "location": "Delhi",
}


def job(title: str = "Data Analyst", company: str = "Zomato") -> NormalizedJob:
    return NormalizedJob(
        source="jsearch",
        source_id=f"{title}-{company}",
        title=title,
        company=company,
        location="Gurugram, India",
        apply_url="https://example.com/j",
        description="SQL and Power BI, reporting to the finance team.",
    )


def scorer_returning(payload: Any) -> Any:
    def call(_system: str, _user: str, _max_tokens: int) -> Any:
        return payload

    return call


def scorer_raising() -> Any:
    def call(_system: str, _user: str, _max_tokens: int) -> Any:
        raise RuntimeError("anthropic is down")

    return call


# ── The difference from V1 ─────────────────────────────────────────────────


def test_a_failed_call_leaves_every_job_unscored_rather_than_at_seventy() -> None:
    """V1 showed a fabricated 70% with invented copy. A real posting is worth
    showing without the fit copy; the absence has to look like absence."""
    jobs = [job(), job("BI Analyst")]

    out = score(jobs, PROFILE, scorer=scorer_raising())

    assert out == {}
    # And merging nothing leaves the row with no score at all.
    assert merge({"title": "Data Analyst"}, out.get(0)) == {"title": "Data Analyst"}


def test_a_truncated_response_scores_what_arrived_and_no_more() -> None:
    """The exact shape of V1's live failure: the array stops early."""
    jobs = [job(), job("BI Analyst"), job("Product Analyst")]

    out = score(
        jobs,
        PROFILE,
        scorer=scorer_returning({"items": [{"id": 0, "match_score": 88, "why_match": "SQL."}]}),
    )

    assert set(out) == {0}
    assert out[0].match_score == 88
    assert 1 not in out and 2 not in out


def test_a_response_with_no_items_array_scores_nothing() -> None:
    assert score([job()], PROFILE, scorer=scorer_returning({"oops": []})) == {}
    assert score([job()], PROFILE, scorer=scorer_returning(None)) == {}


# ── What the model is allowed to say ───────────────────────────────────────


def test_an_annotation_is_read_field_by_field() -> None:
    out = score(
        [job()],
        PROFILE,
        scorer=scorer_returning(
            {
                "items": [
                    {
                        "id": 0,
                        "match_score": 82,
                        "why_match": "Your SQL and Power BI depth maps onto their stack.",
                        "matched_skills": ["SQL", "Power BI"],
                        "missing_skills": ["dbt"],
                        "key_requirements": ["3+ years", "SQL"],
                    }
                ]
            }
        ),
    )

    assert out[0] == Annotation(
        match_score=82,
        why_match="Your SQL and Power BI depth maps onto their stack.",
        matched_skills=["SQL", "Power BI"],
        missing_skills=["dbt"],
        key_requirements=["3+ years", "SQL"],
    )


def test_an_empty_skills_list_is_kept_because_it_is_an_answer() -> None:
    """`[]` means "compared, found nothing". Seventeen live rows are in that
    state and it is NOT the same as never having compared."""
    out = score(
        [job()],
        PROFILE,
        scorer=scorer_returning(
            {"items": [{"id": 0, "match_score": 40, "matched_skills": [], "missing_skills": []}]}
        ),
    )

    assert out[0].matched_skills == []
    assert out[0].missing_skills == []


def test_a_score_outside_the_range_is_clamped_not_trusted() -> None:
    out = score(
        [job(), job("B")],
        PROFILE,
        scorer=scorer_returning(
            {"items": [{"id": 0, "match_score": 140}, {"id": 1, "match_score": -5}]}
        ),
    )
    assert out[0].match_score == 100
    assert out[1].match_score == 0


def test_a_non_numeric_score_is_absent_rather_than_coerced() -> None:
    out = score(
        [job()],
        PROFILE,
        scorer=scorer_returning(
            {"items": [{"id": 0, "match_score": "very good", "why_match": "Fine."}]}
        ),
    )
    assert out[0].match_score is None
    # The sentence survives; only the number is missing.
    assert out[0].why_match == "Fine."


def test_an_id_outside_the_batch_is_dropped_not_merged() -> None:
    """An id for a posting that is not in this batch is the model talking about
    something it invented. Merging it would attach that annotation to whatever
    real job happens to sit at that index."""
    out = score(
        [job()],
        PROFILE,
        scorer=scorer_returning(
            {"items": [{"id": 7, "match_score": 99, "why_match": "Perfect fit."}]}
        ),
    )
    assert out == {}


def test_an_empty_why_is_absent_rather_than_an_empty_sentence() -> None:
    out = score(
        [job()],
        PROFILE,
        scorer=scorer_returning({"items": [{"id": 0, "match_score": 70, "why_match": "   "}]}),
    )
    assert out[0].why_match is None


def test_no_jobs_means_no_call_at_all() -> None:
    called: list[int] = []

    def counting(_s: str, _u: str, _m: int) -> Any:
        called.append(1)
        return {"items": []}

    assert score([], PROFILE, scorer=counting) == {}
    assert called == []


# ── Merging onto the row ───────────────────────────────────────────────────


def test_merge_writes_only_what_the_model_actually_said() -> None:
    """A missing `matched_skills` reads as "never compared" downstream, so an
    absent annotation field must not be written as an empty one."""
    row = merge(
        {"title": "Data Analyst", "details": {"source": "jsearch"}},
        Annotation(match_score=82, why_match="Good.", matched_skills=["SQL"]),
    )

    assert row["match_score"] == 82
    assert row["why_match"] == "Good."
    assert row["details"]["matched_skills"] == ["SQL"]
    assert "missing_skills" not in row["details"]
    assert "keyRequirements" not in row["details"]
    # What was already there survives.
    assert row["details"]["source"] == "jsearch"


def test_merge_never_mutates_the_row_it_was_given() -> None:
    original = {"title": "Data Analyst", "details": {"source": "jsearch"}}
    merge(original, Annotation(match_score=90))
    assert original == {"title": "Data Analyst", "details": {"source": "jsearch"}}
