"""The Naukri model.

Rewritten when the lens was cut back to what it actually computes. The tests
that went with it asserted things about `score_key_skills`, `score_parseability`
and `cap_repeats`, none of which exist any more — they scored dimensions no
data source could ever feed, which put 45 points into a denominator nobody
could earn.

What is held here now:

- **The denominator is what we compute**, never 100.
- **"We don't assess this" and "we couldn't assess this for you" are different
  states**, and the types make them impossible to confuse.
- A `NOT_BUILT` dimension carries no action, because there is nothing the user
  could do.
- An `UNAVAILABLE` one always carries one, because there is.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from carigma_api.services.naukri import (
    FILTER_FIELDS,
    MODEL_NOTE,
    NOT_YET_MODELLED,
    TOTAL_WEIGHT,
    TUNEUP_CREDITS,
    WEIGHTS,
    Confidence,
    CycleState,
    Dimension,
    NaukriScore,
    NaukriState,
    UnlockAction,
    scope_note,
    score_filter_fields,
    score_headline,
    should_persist,
    starts_new_cycle,
)

FULL_PROFILE = {
    "linkedinHeadline": "Business Analyst | 5 years | SQL, Power BI | Ops analytics",
    "location": "Bengaluru",
    "education": "B.Tech",
}


def _scored(profile: dict[str, object] | None = None) -> NaukriScore:
    p = dict(FULL_PROFILE if profile is None else profile)
    dimensions = []
    fixes = []
    for dim, fix in (score_headline(str(p.get("linkedinHeadline") or "")), score_filter_fields(p)):
        dimensions.append(dim)
        if fix is not None:
            fixes.append(fix)
    dimensions.extend(Dimension.not_built(k) for k in NOT_YET_MODELLED)
    return NaukriScore(dimensions=dimensions, fixes=fixes)


# ── The model describes what it computes ───────────────────────────────────


def test_the_denominator_is_what_we_compute_not_one_hundred() -> None:
    """A model advertising points it cannot award is a ceiling nobody reaches.

    `WEIGHTS` used to declare seven dimensions summing to 100 while four had no
    scorer and two had no data source, so `COMPLETE` was unreachable by
    construction and every user sat permanently below a line.
    """
    assert TOTAL_WEIGHT == sum(WEIGHTS.values())
    assert set(WEIGHTS) == {"headline", "filter_fields"}
    assert TOTAL_WEIGHT != 100, "the denominator drifted back to a number we cannot award"


def test_complete_is_actually_reachable() -> None:
    score = _scored()

    assert score.assessable_weight == TOTAL_WEIGHT
    assert score.state is NaukriState.COMPLETE


def test_no_weight_is_declared_twice() -> None:
    """A key in both dicts would be scored and disclaimed at the same time."""
    assert not set(WEIGHTS) & set(NOT_YET_MODELLED)


def test_the_scope_note_is_derived_from_the_code_not_written_out() -> None:
    """ "The seven dimensions" survived in the copy while four had no scorer,
    because the sentence and the code had no connection."""
    note = scope_note()
    total = len(WEIGHTS) + len(NOT_YET_MODELLED)

    assert f"{len(WEIGHTS)} of the {total}" in note

    # Walk the AST, skipping the docstring. The first version of this guard
    # read `inspect.getsource` and fired on its own explanation of the bug —
    # a guard over prose, which is the thing CONTRIBUTING says not to build.
    fn = ast.parse(inspect.getsource(scope_note)).body[0]
    assert isinstance(fn, ast.FunctionDef)
    body = fn.body[1:] if ast.get_docstring(fn) else fn.body
    literals = [
        n.value.lower()
        for stmt in body
        for n in ast.walk(stmt)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    for spelled in ("two", "five", "seven"):
        assert not any(spelled in lit for lit in literals), (
            f"the count is spelled {spelled!r} in the copy instead of computed"
        )


def test_the_payload_says_the_weights_are_ours_not_naukris() -> None:
    payload = _scored().as_dict()

    assert payload["model_note"] == MODEL_NOTE
    assert "not Naukri's formula" in payload["model_note"]


def test_the_payload_carries_both_halves_of_the_fraction() -> None:
    """A score with no denominator is the "62/100" misreading waiting to happen."""
    payload = _scored().as_dict()

    assert payload["assessed_weight"] == TOTAL_WEIGHT
    assert payload["total_weight"] == TOTAL_WEIGHT


# ── The two states that must never merge ───────────────────────────────────


def test_not_built_and_unavailable_are_different_states() -> None:
    """ "We don't assess this yet" and "we couldn't assess this for you" are
    different statements. A user reading one as the other draws the wrong
    conclusion about their own profile."""
    assert Confidence.NOT_BUILT is not Confidence.UNAVAILABLE

    not_built = Dimension.not_built("key_skills")
    unavailable = Dimension.unavailable(
        "headline",
        "Resume headline",
        "Nothing read yet.",
        unlocked_by="Run the tune-up.",
        action=UnlockAction(label="Run it", route="/signal/naukri"),
    )

    assert not_built.confidence is Confidence.NOT_BUILT
    assert unavailable.confidence is Confidence.UNAVAILABLE


def test_a_not_built_dimension_offers_no_action_at_all() -> None:
    """The thing standing in the way is our roadmap, not the user's profile.

    An action here would be a lie with a click target — exactly what the
    `key_skills` "Run Career Scout" button was, pointing at an endpoint that
    does not exist.
    """
    for key in NOT_YET_MODELLED:
        dim = Dimension.not_built(key)
        assert dim.unlock_action is None, f"{key} offers a button for something we have not built"
        assert dim.unlocked_by is None, f"{key} tells the user to do something about our gap"


def test_a_not_built_dimension_says_what_WE_would_need() -> None:
    """It is a statement about our roadmap, so it reads as one."""
    for key in NOT_YET_MODELLED:
        assert Dimension.not_built(key).needs, f"{key} is absent with no explanation"


def test_a_not_built_dimension_cannot_drag_the_score_down() -> None:
    """Weight zero, so it is outside the arithmetic entirely rather than a zero
    inside it."""
    score = _scored()

    assert all(d.weight == 0 for d in score.not_built)
    assert score.assessable_weight == TOTAL_WEIGHT
    # And the same score with the not-built cards removed is identical.
    without = NaukriScore(dimensions=[d for d in score.dimensions if d.weight], fixes=score.fixes)
    assert without.score == score.score
    assert without.state is score.state


def test_an_unavailable_dimension_cannot_be_built_without_a_way_out() -> None:
    with pytest.raises(ValueError, match="dead end"):
        Dimension.unavailable("headline", "Resume headline", "Nope.", unlocked_by="   ")


def test_a_measured_dimension_cannot_exist_without_a_receipt() -> None:
    with pytest.raises(ValueError, match="receipt"):
        Dimension.measured("headline", "Resume headline", 70, "")


# ── The dimensions we do assess ────────────────────────────────────────────


def test_a_full_headline_scores_and_shows_its_working() -> None:
    dim, fix = score_headline(FULL_PROFILE["linkedinHeadline"])

    assert dim.score is not None and dim.score > 0
    assert dim.receipt
    assert fix is None or fix.why_it_helps


def test_an_empty_headline_is_a_real_zero_not_an_absence() -> None:
    """We CAN read an empty headline. Nothing is unavailable about it."""
    dim, fix = score_headline("")

    assert dim.confidence is Confidence.MEASURED
    assert dim.score == 0
    assert fix is not None


def test_the_filter_fields_are_only_ones_a_user_can_actually_fill() -> None:
    """`total_experience`, `current_ctc` and `notice_period` were here with no
    column, no input, and no way for any run to fill them — so the dimension
    was capped for every user forever and its fix named three fields nobody
    could supply."""
    assert set(FILTER_FIELDS) == {"location", "education"}
    for gone in ("total_experience", "current_ctc", "notice_period"):
        assert gone not in FILTER_FIELDS


def test_blank_filters_are_explained_as_exclusion_not_deduction() -> None:
    dim, fix = score_filter_fields({"location": "Bengaluru"})

    assert dim.score == 50
    assert fix is not None
    assert "BEFORE ranking" in fix.why_it_helps


def test_all_filters_filled_scores_full_and_offers_no_fix() -> None:
    dim, fix = score_filter_fields({"location": "Bengaluru", "education": "B.Tech"})

    assert dim.score == 100
    assert fix is None


# ── Fixes describe; they never apply ───────────────────────────────────────


def test_every_fix_explains_why_rather_than_asserting() -> None:
    for profile in ({}, {"location": "Bengaluru"}, FULL_PROFILE):
        for fix in _scored(profile).fixes:
            assert len(fix.why_it_helps) > 40, f"{fix.key} asserts without explaining"


def test_every_fix_points_at_where_the_change_is_made() -> None:
    """A fix with no destination is the same dead end as an absence with no
    action — the user is told what to change and left to find it."""
    for fix in _scored({}).fixes:
        assert fix.action is not None, f"{fix.key} names no destination"
        assert fix.action.route.startswith("/")


def test_no_fix_ever_claims_to_have_applied_itself() -> None:
    """Prose has no parse tree, so this is the one guard that must match
    strings. The discipline holds in a weaker form: ban the CLAIM, not a token
    inside it. A version banning "applied" fired on "these are applied before
    ranking" — a true sentence about Naukri's filters.
    """
    claims = ("we applied", "we've applied", "we have applied", "we updated", "we changed")
    for profile in ({}, FULL_PROFILE):
        for fix in _scored(profile).fixes:
            blob = f"{fix.title} {fix.why_it_helps}".lower()
            for claim in claims:
                assert claim not in blob, f"{fix.key} says {claim!r}"


def test_nothing_in_this_module_writes_to_a_profile() -> None:
    """Walks the AST rather than grepping: a string match can only enumerate
    the spellings someone already thought of."""
    from carigma_api.services import naukri as module

    tree = ast.parse(inspect.getsource(module))
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            referenced.add(node.value)

    for forbidden in ("save", "upsert", "insert", "update", "profiles", "table"):
        assert forbidden not in referenced, f"{forbidden!r} appears in a module that only describes"


# ── Coverage copy ──────────────────────────────────────────────────────────


def test_a_never_run_lens_does_not_talk_about_percentages() -> None:
    """ "Scored on the 0% of the model we could assess" is accurate and reads
    like a bug — a percentage invites zero to be read as a result."""
    note = NaukriScore().as_dict()["coverage_note"]

    assert "%" not in note
    assert "hasn't run" in note


def test_a_partial_lens_says_how_much_of_what_we_assess_was_read() -> None:
    partial = NaukriScore(
        dimensions=[
            Dimension.measured("headline", "Resume headline", 80, "ok"),
            Dimension.unavailable(
                "filter_fields",
                "Filter fields",
                "Nothing read.",
                unlocked_by="Run the tune-up.",
            ),
        ]
    )
    payload = partial.as_dict()

    assert payload["state"] == "partial"
    # 20 of 30 — a percentage of what we assess, never of 100.
    assert "67%" in payload["coverage_note"]


def test_the_state_is_carried_not_inferred_from_a_null_score() -> None:
    """Two surfaces inferring the same thing eventually spell it differently."""
    assert NaukriScore().as_dict()["state"] == "never_run"
    assert _scored().as_dict()["state"] == "complete"


# ── The cycle and its single charge ────────────────────────────────────────


def test_the_tuneup_is_ten_credits_per_CYCLE() -> None:
    assert TUNEUP_CREDITS == 10


def test_a_first_run_starts_a_cycle_and_charges() -> None:
    assert starts_new_cycle(None) is True


def test_re_running_MID_cycle_is_free() -> None:
    assert starts_new_cycle(CycleState.NEEDS_TUNEUP) is False


def test_running_again_after_finishing_starts_a_NEW_cycle() -> None:
    assert starts_new_cycle(CycleState.OPTIMIZED) is True


def test_a_never_run_score_is_not_persisted() -> None:
    """A null score in `naukri_scores` is something every later trend line has
    to special-case forever, and it claims in the record that we assessed a
    profile we did not."""
    assert should_persist(NaukriScore()) is False


def test_a_score_with_anything_measured_IS_persisted() -> None:
    assert should_persist(_scored()) is True


def test_never_run_is_not_a_value_that_lives_in_a_row() -> None:
    """The absence of a row IS the never-run state. One representation, so
    nothing can disagree with it."""
    assert should_persist(NaukriScore()) is False
    assert CycleState.NEVER_RUN not in (CycleState.NEEDS_TUNEUP, CycleState.OPTIMIZED)


def test_a_lens_of_only_not_built_dimensions_is_still_never_run() -> None:
    """The unbuilt cards must not make an empty lens look like a run."""
    only_absent = NaukriScore(dimensions=[Dimension.not_built(k) for k in NOT_YET_MODELLED])

    assert only_absent.state is NaukriState.NEVER_RUN
    assert only_absent.score is None
    assert should_persist(only_absent) is False
