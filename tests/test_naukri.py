"""The Naukri tune-up scorer.

The rule that shapes every test here: **a dimension we cannot score honestly
scores nothing.** Not zero, not a guess, not a middle value — it leaves the
model, and the total says how much of the model it was computed from.

That is `ProviderUnavailable != empty` applied to a number. A user reading "62"
must not be reading a figure a fifth of which was invented.
"""

from __future__ import annotations

import pytest

from carigma_api.services.naukri import (
    FILTER_FIELDS,
    MAX_SKILL_REPEATS,
    WEIGHTS,
    Confidence,
    Dimension,
    NaukriScore,
    NaukriState,
    cap_repeats,
    score_filter_fields,
    score_headline,
    score_key_skills,
    score_parseability,
)

FULL_PROFILE = {
    "location": "Bengaluru",
    "total_experience": "5",
    "current_ctc": "18 LPA",
    "notice_period": "30 days",
    "education": "B.Tech",
}


# ── The model ──────────────────────────────────────────────────────────────


def test_the_seven_weights_are_the_roadmap_model_and_sum_to_100() -> None:
    assert WEIGHTS == {
        "key_skills": 25,
        "headline": 20,
        "parseability": 20,
        "completeness": 12,
        "filter_fields": 10,
        "designation": 8,
        "summary_recency": 5,
    }
    assert sum(WEIGHTS.values()) == 100


def test_the_payload_says_the_weights_are_ours_not_naukris() -> None:
    """Presenting an estimate as the platform's own arithmetic would be the
    same lie as fabricating a job."""
    note = NaukriScore(dimensions=[_measured("headline", 80)]).as_dict()["model_note"]

    assert "estimate" in note.lower()
    assert "not naukri's formula" in note.lower()


def _measured(key: str, score: int) -> Dimension:
    return Dimension.measured(key, key, score, "a receipt")


# ── Unavailable is not zero ────────────────────────────────────────────────


def test_an_unassessable_dimension_leaves_the_model_entirely() -> None:
    """Not scored zero — which would read as "you did badly at this" — and not
    scored full, which would flatter. It is simply not counted."""
    score = NaukriScore(
        dimensions=[
            _measured("headline", 100),
            Dimension.unavailable(
                "key_skills", "Key Skills", "no JD corpus yet", unlocked_by="run Career Scout"
            ),
        ]
    )

    # 20 of the 45 available weight, all of it earned.
    assert score.assessable_weight == 20
    assert score.score == 100, "the unavailable 25% must not drag the score down"


def test_the_payload_states_how_much_of_the_model_was_assessed() -> None:
    """So no surface can render 62 as 62/100 when a fifth was not measured."""
    payload = NaukriScore(
        dimensions=[
            _measured("headline", 60),
            Dimension.unavailable(
                "parseability", "Parse-ability", "no resume yet", unlocked_by="upload a resume"
            ),
        ]
    ).as_dict()

    assert payload["assessed_weight"] == 20
    assert "20% of the model" in payload["coverage_note"]


def test_nothing_assessable_scores_None_not_zero() -> None:
    """A brand-new user has not scored badly. They have not been scored."""
    score = NaukriScore(
        dimensions=[Dimension.unavailable("headline", "H", "no data", unlocked_by="add one")]
    )

    assert score.score is None
    assert score.assessable_weight == 0


def test_a_measured_dimension_cannot_exist_without_a_receipt() -> None:
    """Same rule as the weekly review's facts: the type refuses a claim with no
    evidence behind it."""
    with pytest.raises(ValueError, match="receipt"):
        Dimension.measured("headline", "Headline", 70, "")


# ── Key skills: coverage of TRUE skills ────────────────────────────────────


def test_no_jd_corpus_means_unavailable_not_a_guess() -> None:
    """Scoring coverage against nothing produces a number with no referent."""
    dimension, fix = score_key_skills(("SQL",), ())

    assert dimension.confidence is Confidence.UNAVAILABLE
    assert dimension.score is None
    assert fix is None
    assert "Career Scout" in (dimension.unlocked_by or "")


def test_missing_skills_are_candidates_the_user_must_confirm() -> None:
    """Every suggested skill must be confirmable as TRUE. We propose; the user
    ticks. Nothing here is written to a profile."""
    _, fix = score_key_skills(("SQL",), ("SQL", "dbt", "Airflow"))

    assert fix is not None
    assert fix.requires_confirmation is True
    assert set(fix.candidate_skills) == {"dbt", "Airflow"}


def test_full_coverage_offers_no_fix() -> None:
    dimension, fix = score_key_skills(("SQL", "dbt"), ("SQL", "dbt"))

    assert dimension.score == 100
    assert fix is None


def test_the_key_skills_reason_explains_absence_not_ranking() -> None:
    """The mechanic matters: a missing true skill is not a ranking penalty, it
    is exclusion from the result set. A user who understands that will fix it."""
    _, fix = score_key_skills((), ("SQL",))

    assert fix is not None
    assert "before" in fix.why_it_helps.lower()
    assert "entirely" in fix.why_it_helps.lower()


# ── No keyword stuffing ────────────────────────────────────────────────────


def test_repetition_is_capped_not_merely_discouraged() -> None:
    """RChilli normalises repeats to zero gain and recruiters reject stuffed
    profiles. The cap is enforced so no caller can opt out."""
    capped = cap_repeats(("SQL", "SQL", "SQL", "SQL", "dbt"))

    assert capped.count("SQL") == MAX_SKILL_REPEATS
    assert "dbt" in capped


def test_the_cap_is_case_insensitive() -> None:
    """ "SQL, sql, Sql" is stuffing with extra steps."""
    assert len(cap_repeats(("SQL", "sql", "Sql", "SqL"))) == MAX_SKILL_REPEATS


# ── Parse-ability ──────────────────────────────────────────────────────────


def test_no_resume_analysed_is_unavailable_not_unparseable() -> None:
    """A user who has not uploaded is not a user with a broken resume."""
    dimension, fix = score_parseability(None)

    assert dimension.confidence is Confidence.UNAVAILABLE
    assert fix is None


def test_a_clean_resume_scores_full_and_offers_no_fix() -> None:
    dimension, fix = score_parseability(())

    assert dimension.score == 100
    assert fix is None


def test_each_parse_hazard_is_explained_by_its_consequence(  # noqa: D103
) -> None:
    _, fix = score_parseability(("multi_column", "scanned"))

    assert fix is not None
    # Not the jargon — what the parser actually does with it.
    assert "interleaved" in fix.why_it_helps
    assert "no text layer" in fix.why_it_helps


# ── Filter fields gate, they do not rank ───────────────────────────────────


def test_all_filters_filled_scores_full() -> None:
    dimension, fix = score_filter_fields(dict(FULL_PROFILE))

    assert dimension.score == 100
    assert fix is None


def test_blank_filters_are_explained_as_exclusion(  # noqa: D103
) -> None:
    dimension, fix = score_filter_fields({**FULL_PROFILE, "notice_period": ""})

    assert dimension.score == round(100 * (len(FILTER_FIELDS) - 1) / len(FILTER_FIELDS))
    assert fix is not None
    assert "BEFORE ranking" in fix.why_it_helps
    assert "never sees" in fix.why_it_helps


def test_ctc_and_notice_require_confirmation() -> None:
    """Facts only the user knows. We must never fill them in."""
    _, fix = score_filter_fields({**FULL_PROFILE, "current_ctc": ""})

    assert fix is not None and fix.requires_confirmation is True


# ── Every fix teaches ──────────────────────────────────────────────────────


def test_every_fix_explains_why_rather_than_asserting() -> None:
    """7.1: explain why each fix helps, tied to Resdex mechanics. A fix without
    a reason cannot be judged, and teaches nothing after we stop saying it."""
    fixes = [
        score_key_skills((), ("SQL",))[1],
        score_headline("")[1],
        score_parseability(("scanned",))[1],
        score_filter_fields({})[1],
    ]

    for fix in fixes:
        assert fix is not None
        assert len(fix.why_it_helps) > 60, f"{fix.key} asserts without explaining"
        assert fix.title, f"{fix.key} has no title"


def test_no_fix_ever_claims_to_have_applied_itself() -> None:
    """Carigma proposes; the user decides. A fix that says "done" would be
    claiming to have edited someone's Naukri profile, which we cannot do.

    The banned list is FIRST-PERSON COMPLETION CLAIMS, not bare verbs. The
    first version forbade "applied" and tripped on "these are applied before
    ranking" — a true sentence about Naukri's filters, not a claim about us.

    This is the limit of the AST rule: the thing being asserted absent is
    prose, and prose has no parse tree. The discipline that survives is the
    same one — name the actual violation ("we applied") rather than a token
    that appears in it.
    """
    claims = (
        "we've updated",
        "we have updated",
        "we applied",
        "we've applied",
        "done for you",
        "we changed",
        "has been updated",
    )
    for fix in (score_key_skills((), ("SQL",))[1], score_filter_fields({})[1]):
        assert fix is not None
        text = f"{fix.title} {fix.why_it_helps}".lower()
        for claim in claims:
            assert claim not in text, f"{fix.key} claims to have done the work: {claim!r}"


def test_every_unavailable_dimension_says_what_would_unlock_it() -> None:
    """An absence stated without a next action is a dead end. Stated with one
    it becomes the co-pilot model: "we can't measure this yet, and here is what
    would let us."

    Enforced on every unavailable dimension the scorers can produce, not just
    the two that happen to have good copy today.
    """
    unavailable = [
        score_key_skills(("SQL",), ())[0],
        score_parseability(None)[0],
    ]

    for dimension in unavailable:
        assert dimension.confidence is Confidence.UNAVAILABLE
        assert dimension.unlocked_by, f"{dimension.key} is a dead end"
        # A next action, not a restatement of the absence.
        assert len(dimension.unlocked_by) > 20
        assert dimension.unlocked_by != dimension.unavailable_reason


def test_an_unavailable_dimension_cannot_be_built_without_a_way_out() -> None:
    """The type refuses the dead-end version, exactly as `measured` refuses a
    score with no receipt."""
    with pytest.raises(ValueError, match="dead end"):
        Dimension.unavailable("headline", "Headline", "no data", unlocked_by="")


# ── The never-run state: the PRIMARY path, not an edge case ────────────────
#
# `naukri_scores` is empty and V1 never had Naukri scoring, so every existing
# user lands here at cutover. It gets tested like the main path it is.


def _never_run() -> NaukriScore:
    """Exactly what a real user sees today: nothing measurable."""
    return NaukriScore(
        dimensions=[
            score_key_skills((), ())[0],
            score_parseability(None)[0],
        ]
    )


def test_a_never_run_lens_does_not_talk_about_percentages() -> None:
    """ "Scored on the 0% of the model we could assess" is accurate and reads
    like a bug. A lens that has not started has not scored badly on nothing."""
    payload = _never_run().as_dict()

    assert payload["state"] == str(NaukriState.NEVER_RUN)
    assert payload["coverage_note"] == "Not scored yet — the tune-up hasn't run."
    assert "0%" not in payload["coverage_note"]
    assert payload["score"] is None


def test_the_state_is_carried_not_inferred_from_a_null_score() -> None:
    """Two surfaces inferring `score is None` would eventually spell it
    differently. The API decides."""
    assert _never_run().state is NaukriState.NEVER_RUN
    assert NaukriScore(dimensions=[_measured("headline", 50)]).state is NaukriState.PARTIAL
    assert NaukriScore(dimensions=[_measured(k, 50) for k in WEIGHTS]).state is NaukriState.COMPLETE


def test_a_partial_lens_still_says_how_much_was_assessed() -> None:
    """The percentage framing is right where it is true — it just is not the
    never-run sentence."""
    payload = NaukriScore(
        dimensions=[
            _measured("headline", 60),
            Dimension.unavailable("key_skills", "K", "no corpus", unlocked_by="run the scout"),
        ]
    ).as_dict()

    assert payload["state"] == str(NaukriState.PARTIAL)
    assert "20% of the model" in payload["coverage_note"]


def test_every_unlock_on_the_never_run_screen_is_somewhere_you_can_GO() -> None:
    """The user's point: "run Career Scout" must be a thing they can do from
    where they are standing, not a sentence describing something elsewhere.

    This is the whole never-run screen — if these are not actionable, the
    primary path is a dead end with good grammar.
    """
    for dimension in _never_run().dimensions:
        assert dimension.confidence is Confidence.UNAVAILABLE
        assert dimension.unlock_action is not None, f"{dimension.key} is a signpost with no road"
        assert dimension.unlock_action.label
        assert dimension.unlock_action.route.startswith("/")


def test_the_never_run_payload_carries_both_the_sentence_and_the_button() -> None:
    payload = _never_run().as_dict()

    for dim in payload["dimensions"]:
        assert dim["unlocked_by"], "the sentence"
        assert dim["unlock_action"]["route"], "the road"
        # Distinct jobs: prose explains, the action is clickable.
        assert dim["unlock_action"]["label"] != dim["unlocked_by"]


def test_a_never_run_lens_offers_no_fixes_to_pretend_with() -> None:
    """Nothing measured means nothing to fix. Inventing a fix here would be
    fabricating a finding about a profile we have never assessed."""
    assert _never_run().fixes == []
