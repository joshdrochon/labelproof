"""Properties of the aggregate recommendation — where `ready_to_approve` can come from.

**This is the file that guards the invariant above all others.** The system must never
produce a false pass on the government warning statement. `recommend()` is the last
function between a set of field verdicts and the words "Ready to approve" on an agent's
screen, so the central property here is stated as an implication over *every*
well-formed result set rather than as a handful of examples:

    recommendation is ready_to_approve
      =>  the government warning row is present, and its verdict is Match,
      and no other field is anything but Match or Not applicable.

An example set can only ever say "these seventeen combinations are safe". The property
says "no combination is unsafe", and hypothesis spends its budget hunting for the one
that is.

A "well-formed result set" means exactly the seven fields, each once — which is what
`api/verify.py` builds. Degenerate inputs (the warning row absent, the warning row
duplicated) are *not* covered by these properties, because `recommend` does not defend
against them today. They are pinned as defects in
tests/regression/test_aggregate_warning_holes.py rather than quietly asserted away.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api.models import FieldName, FieldResult, Recommendation, Verdict
from api.rules import aggregate as agg

pytestmark = pytest.mark.property

SETTINGS = settings(max_examples=500, deadline=None)

VERDICTS = st.sampled_from(list(Verdict))

#: Verdicts that mean "this field is fine and needs nobody's attention".
CLEAN = frozenset({Verdict.MATCH, Verdict.NOT_APPLICABLE})


def _result(field: FieldName, verdict: Verdict) -> FieldResult:
    return FieldResult(
        field=field,
        verdict=verdict,
        extracted=None,
        expected=None,
        confidence=1.0,
        rationale="",
    )


#: The verdicts the warning field can actually carry. `warning.evaluate` returns exactly
#: these five; the health warning is required on all three commodities, so nothing in
#: the engine produces Not applicable for it.
#:
#: **The exclusion is a defect, not a modelling choice.** Feeding this field a Not
#: applicable verdict makes `recommend` return `ready_to_approve` on the strength of a
#: warning nobody checked — a false pass, one line of code away. It is pinned as
#: `test_a_not_applicable_warning_must_never_reach_a_pass` in
#: tests/regression/test_aggregate_warning_holes.py, marked xfail(strict=True) so that
#: fixing it turns that test red and forces this generator to widen.
WARNING_VERDICTS = st.sampled_from(
    [
        Verdict.MATCH,
        Verdict.ACCEPTABLE_VARIATION,
        Verdict.MISMATCH,
        Verdict.MISSING,
        Verdict.UNREADABLE,
    ]
)

#: A complete checklist: all seven mandatory elements, each exactly once, with an
#: independently chosen verdict. This is the shape `api/verify.py` produces.
COMPLETE_CHECKLIST = st.fixed_dictionaries(
    {
        field: (WARNING_VERDICTS if field is FieldName.GOVERNMENT_WARNING else VERDICTS)
        for field in FieldName
    }
).map(lambda verdicts: [_result(f, v) for f, v in verdicts.items()])


# --------------------------------------------------------------------------------------
# THE invariant
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-01")
@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_ready_to_approve_requires_the_warning_to_have_matched(
    results: list[FieldResult],
) -> None:
    """Ready to approve implies the government warning verdict is exactly Match.

    Not "not Missing". Not "not Mismatch". Match. Acceptable variation on the warning
    means the wording was right but some formatting could not be confirmed from the
    photograph — the warning fails closed, so that is Needs review and never a pass.
    Unreadable means it was not checked at all.

    This is the single assertion that the whole product is built to keep true.
    """
    aggregate = agg.recommend(results)
    if aggregate.recommendation is not Recommendation.READY_TO_APPROVE:
        return
    warning = next(r for r in results if r.field is FieldName.GOVERNMENT_WARNING)
    assert warning.verdict is Verdict.MATCH, (
        f"ready_to_approve returned with the warning at {warning.verdict.value}"
    )


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_ready_to_approve_requires_every_field_to_be_clean(
    results: list[FieldResult],
) -> None:
    """One field needing attention is enough. Nine good ones do not dilute it.

    Worst-of, stated as a property: there is no averaging, no quorum, and no field
    whose failure another field can outvote.
    """
    aggregate = agg.recommend(results)
    if aggregate.recommendation is not Recommendation.READY_TO_APPROVE:
        return
    assert all(r.verdict in CLEAN for r in results), [
        (r.field.value, r.verdict.value) for r in results if r.verdict not in CLEAN
    ]


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_a_mismatch_anywhere_forbids_a_pass(results: list[FieldResult]) -> None:
    """The converse, asserted directly rather than inferred.

    Stated this way round it survives a refactor that changes what `CLEAN` means: any
    Mismatch on any field, and the answer is not a pass.
    """
    if any(r.verdict is Verdict.MISMATCH for r in results):
        assert agg.recommend(results).recommendation is not Recommendation.READY_TO_APPROVE


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_an_unreadable_field_forbids_a_pass(results: list[FieldResult]) -> None:
    """"We could not check this" can never come out as "this is fine".

    The pre-gate and the budget stop both return Unreadable on every row. If Unreadable
    could reach a pass, a timed-out verification would approve a label nobody looked at.
    """
    if any(r.verdict is Verdict.UNREADABLE for r in results):
        assert agg.recommend(results).recommendation is not Recommendation.READY_TO_APPROVE


# --------------------------------------------------------------------------------------
# The warning outranks everything
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-07")
@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_a_missing_or_wrong_warning_always_drives_return_for_correction(
    results: list[FieldResult],
) -> None:
    """WARN-6, MATCH-10: the warning is disqualifying on its own terms.

    Whatever else is on the label, an absent or non-verbatim health warning is a return
    for correction, and the warning is the field named as driving it. Anything softer
    would let a Missing warning share a screen with six Match rows and read as a
    paperwork nit.
    """
    warning = next(r for r in results if r.field is FieldName.GOVERNMENT_WARNING)
    if warning.verdict not in {Verdict.MISSING, Verdict.MISMATCH}:
        return
    aggregate = agg.recommend(results)
    assert aggregate.recommendation is Recommendation.RETURN_FOR_CORRECTION
    assert aggregate.driving_field is FieldName.GOVERNMENT_WARNING


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_the_warning_outranks_a_missing_element_elsewhere(
    results: list[FieldResult],
) -> None:
    """When several fields are equally disqualifying, the warning is the one named.

    The driving field is what the UI puts at the top and what the rationale sentence
    talks about. A missing brand name and a missing warning are both returns; only one
    of them is the reason a regulator would look twice.
    """
    warning = next(r for r in results if r.field is FieldName.GOVERNMENT_WARNING)
    if warning.verdict not in {Verdict.MISSING, Verdict.MISMATCH}:
        return
    assert agg.recommend(results).driving_field is FieldName.GOVERNMENT_WARNING


# --------------------------------------------------------------------------------------
# Monotonicity — a worse field can never improve the advice
# --------------------------------------------------------------------------------------

#: Ascending seriousness, mirroring `aggregate._SEVERITY`. Written out rather than
#: imported so that reordering the private table breaks this test instead of being
#: silently ratified by it.
_ORDER = [
    Verdict.MATCH,
    Verdict.ACCEPTABLE_VARIATION,
    Verdict.UNREADABLE,
    Verdict.MISMATCH,
    Verdict.MISSING,
]

_ADVICE_ORDER = {
    Recommendation.READY_TO_APPROVE: 0,
    Recommendation.NEEDS_REVIEW: 1,
    Recommendation.RETURN_FOR_CORRECTION: 2,
}


@SETTINGS
@given(
    COMPLETE_CHECKLIST,
    st.sampled_from(list(FieldName)),
    st.integers(min_value=0, max_value=len(_ORDER) - 1),
)
def test_worsening_any_field_never_improves_the_recommendation(
    results: list[FieldResult], field: FieldName, index: int
) -> None:
    """Making one field worse can only hold the advice steady or make it worse.

    Non-monotonicity would mean a label could be *approved because* one of its fields
    got worse — the exact shape of bug that produces a false pass nobody can explain
    afterwards.
    """
    before = agg.recommend(results)
    current = next(r for r in results if r.field is field)
    if current.verdict is Verdict.NOT_APPLICABLE or current.verdict not in _ORDER:
        return
    worse = _ORDER[max(index, _ORDER.index(current.verdict))]
    mutated = [r if r.field is not field else _result(field, worse) for r in results]
    after = agg.recommend(mutated)
    assert _ADVICE_ORDER[after.recommendation] >= _ADVICE_ORDER[before.recommendation]


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_the_recommendation_does_not_depend_on_the_order_of_the_rows(
    results: list[FieldResult],
) -> None:
    """Reversing the checklist cannot change the advice.

    `verify()` builds the list in a fixed order and `triage_order` reorders it for
    display. If the recommendation depended on list order, those two would disagree
    about the same label.
    """
    assert agg.recommend(results) == agg.recommend(list(reversed(results)))


# --------------------------------------------------------------------------------------
# Every recommendation is advice, and every one explains itself
# --------------------------------------------------------------------------------------


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_every_rationale_leaves_the_decision_with_the_agent(
    results: list[FieldResult],
) -> None:
    """HITL-1, SCOPE-3: the app recommends and never decides.

    Every sentence this module can emit ends by saying so. A recommendation phrased as
    a determination is a compliance problem, not a copy problem — the agent is the
    approving official and the record has to read that way.
    """
    aggregate = agg.recommend(results)
    assert "The final decision is yours." in aggregate.rationale


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_a_needs_review_recommendation_names_a_field_to_look_at(
    results: list[FieldResult],
) -> None:
    """"Something needs your eyes" without saying which row is not a triage tool."""
    aggregate = agg.recommend(results)
    if aggregate.recommendation is Recommendation.NEEDS_REVIEW:
        assert aggregate.driving_field is not None


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_a_pass_names_no_driving_field(results: list[FieldResult]) -> None:
    """Nothing drives a clean result, so nothing is highlighted."""
    aggregate = agg.recommend(results)
    if aggregate.recommendation is Recommendation.READY_TO_APPROVE:
        assert aggregate.driving_field is None


def test_an_empty_checklist_is_never_a_pass() -> None:
    """Nothing checked is not the same as nothing wrong.

    A blank checklist reads as "fine" at a glance, which is the opposite of fine.
    """
    aggregate = agg.recommend([])
    assert aggregate.recommendation is Recommendation.NEEDS_REVIEW
    assert "Nothing has been verified" in aggregate.rationale


# --------------------------------------------------------------------------------------
# Triage order
# --------------------------------------------------------------------------------------


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_triage_order_is_a_permutation(results: list[FieldResult]) -> None:
    """Sorting for display must not drop or duplicate a row.

    The checklist is the record of what was checked. A field that vanished between
    computing the verdict and rendering the table is a field an agent will never know
    was examined.
    """
    ordered = agg.triage_order(results)
    assert len(ordered) == len(results)
    assert {r.field for r in ordered} == {r.field for r in results}


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_the_warning_is_always_the_first_row(results: list[FieldResult]) -> None:
    """WARN-6: the warning is shown first whatever its verdict.

    Sorting it below six Match rows because it happens to match is how it stops being
    the thing the agent looks at.
    """
    assert agg.triage_order(results)[0].field is FieldName.GOVERNMENT_WARNING


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_triage_order_is_deterministic(results: list[FieldResult]) -> None:
    """The same verdicts always render in the same order, whatever order they arrived.

    Two agents looking at the same label see the same table. Row order that shifted
    run to run would make "the third row" mean nothing.
    """
    assert [r.field for r in agg.triage_order(results)] == [
        r.field for r in agg.triage_order(list(reversed(results)))
    ]


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_rows_needing_attention_are_ordered_by_seriousness(
    results: list[FieldResult],
) -> None:
    """After the warning, the most serious row comes first.

    An agent reading top-down should hit the reason for the verdict before the
    supporting detail.
    """
    severity = {v: i for i, v in enumerate([*_ORDER, Verdict.NOT_APPLICABLE])}
    tail = agg.triage_order(results)[1:]
    scores = [severity[r.verdict] for r in tail]
    normalised = [
        0 if r.verdict is Verdict.NOT_APPLICABLE else s
        for r, s in zip(tail, scores, strict=True)
    ]
    assert normalised == sorted(normalised, reverse=True)


@SETTINGS
@given(COMPLETE_CHECKLIST)
def test_attention_fields_are_exactly_the_rows_that_are_not_clean(
    results: list[FieldResult],
) -> None:
    """The UI quiets clean rows. It must not quiet anything else.

    Five Match rows carrying the same visual weight as the one Mismatch is what buries
    a finding — but a row dropped from this list is a finding an agent never sees.
    """
    attention = agg.attention_fields(results)
    assert {r.field for r in attention} == {r.field for r in results if r.verdict not in CLEAN}
