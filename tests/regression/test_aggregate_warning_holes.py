"""OPEN DEFECTS: three ways `recommend()` can pass a label whose warning was not checked.

`api/rules/aggregate.py` decides what the agent sees at the top of the screen. It is the
last function between a set of verdicts and the words "Ready to approve", and it defends
against exactly one degenerate input: an empty list. Everything else it trusts.

Three inputs it should not trust, found by the property test in
tests/properties/test_aggregate_properties.py:

1. **The warning row carrying `not_applicable` returns `ready_to_approve`.**
   `_SEVERITY[NOT_APPLICABLE] == 0`, so a Not-applicable warning is indistinguishable
   from a Match. The government warning is `REQUIRED` for all three commodities in the
   requirement matrix, so this verdict should be unreachable for it — and nothing in
   `recommend` says so.

2. **The warning row missing entirely returns `ready_to_approve`.** `_warning()` returns
   `None` and the disqualifying check is skipped. A caller that builds a checklist
   without the warning row gets a clean pass on a label whose most important element was
   never examined.

3. **Duplicate warning rows are resolved by list order.** `_warning()` takes the *first*
   match. Given a Match row followed by a Mismatch row, the disqualifying check reads the
   Match, skips, and the run falls through to Needs review — downgrading the single most
   serious finding the product can produce to the second-least serious advice, on nothing
   but the order two rows happened to be appended in.

**None of these is reachable through `POST /verify` today.** `api/verify.py` builds the
seven rows itself, once each, and `warning.evaluate` never returns Not applicable. They
are latent — each is one refactor, one new caller, or one merge step away, and the failure
mode is the single thing the PRD says this product must never do.

**The fix is small and belongs in `recommend()`**: require the warning row to be present,
treat any warning verdict other than Match as disqualifying a pass, and resolve duplicates
by worst-of rather than by position. `recommend` is the right place because it is the
choke point every caller already goes through.

All three are `xfail(strict=True)`. They fail today; fixing any one turns that test red,
which is the signal to widen `WARNING_VERDICTS` in the property file.
"""

from __future__ import annotations

import pytest

from api.models import FieldName, FieldResult, Recommendation, Verdict
from api.rules import aggregate as agg
from api.rules.commodity import REQUIREMENTS, Requirement

pytestmark = pytest.mark.regression


def _row(field: FieldName, verdict: Verdict) -> FieldResult:
    return FieldResult(
        field=field,
        verdict=verdict,
        extracted=None,
        expected=None,
        confidence=1.0,
        rationale="",
    )


def _clean_except_warning() -> list[FieldResult]:
    return [
        _row(field, Verdict.MATCH)
        for field in FieldName
        if field is not FieldName.GOVERNMENT_WARNING
    ]


# --------------------------------------------------------------------------------------
# Established first: the warning is required, so Not applicable is never right for it
# --------------------------------------------------------------------------------------


def test_the_warning_is_required_for_every_commodity() -> None:
    """The premise the three defects below violate.

    27 CFR 16.21 applies to every alcohol beverage label. If this ever legitimately
    changes, the defects stop being defects and this file needs rewriting rather than
    deleting.
    """
    for commodity, matrix in REQUIREMENTS.items():
        assert matrix[FieldName.GOVERNMENT_WARNING] is Requirement.REQUIRED, commodity


def test_a_clean_checklist_with_a_matching_warning_does_pass() -> None:
    """The control. Hardening `recommend` must not make a compliant label unapprovable."""
    results = [*_clean_except_warning(), _row(FieldName.GOVERNMENT_WARNING, Verdict.MATCH)]
    assert agg.recommend(results).recommendation is Recommendation.READY_TO_APPROVE


# --------------------------------------------------------------------------------------
# Defect 1: a Not-applicable warning passes
# --------------------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open): _SEVERITY[NOT_APPLICABLE] is 0, so a government-warning row "
        "carrying Not applicable is treated exactly like a Match and recommend() "
        "returns ready_to_approve. The warning is REQUIRED for all three commodities, "
        "so that verdict is never correct for this field. Fix: in recommend(), treat "
        "any government-warning verdict other than MATCH as forbidding a pass. "
        "Owner: api/rules/aggregate.py."
    ),
)
def test_a_not_applicable_warning_must_never_reach_a_pass() -> None:
    """A warning that "does not apply" is a false pass on the one field that cannot have one."""
    results = [
        *_clean_except_warning(),
        _row(FieldName.GOVERNMENT_WARNING, Verdict.NOT_APPLICABLE),
    ]
    assert agg.recommend(results).recommendation is not Recommendation.READY_TO_APPROVE


# --------------------------------------------------------------------------------------
# Defect 2: an absent warning row passes
# --------------------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open): _warning() returns None when no government-warning row is "
        "present and the disqualifying check is skipped entirely, so a checklist "
        "missing the row returns ready_to_approve. Fix: recommend() should require the "
        "row and return needs_review naming the warning when it is absent — a checklist "
        "that never examined the warning has not verified the label. "
        "Owner: api/rules/aggregate.py."
    ),
)
def test_a_checklist_with_no_warning_row_must_never_reach_a_pass() -> None:
    """Six Match rows and no warning row is not a verified label.

    This is the shape a partial merge, a batch worker shortcut, or a future caller
    assembling rows from cached per-field results would most plausibly produce.
    """
    assert (
        agg.recommend(_clean_except_warning()).recommendation
        is not Recommendation.READY_TO_APPROVE
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open): same absent-row defect. Nothing tells the agent the warning was "
        "not on the checklist, so the pass looks fully evidenced. "
        "Owner: api/rules/aggregate.py."
    ),
)
def test_an_absent_warning_row_is_named_in_the_rationale() -> None:
    aggregate = agg.recommend(_clean_except_warning())
    assert "warning" in aggregate.rationale.lower()


# --------------------------------------------------------------------------------------
# Defect 3: duplicate warning rows are resolved by position
# --------------------------------------------------------------------------------------


def test_duplicate_warning_rows_currently_change_the_answer_with_their_order() -> None:
    """Diagnostic: the same two rows, two orders, two different recommendations.

    Not marked xfail — it documents the *current* behaviour so the asymmetry is visible
    in the report. The assertion that this is wrong is the next test.
    """
    match_first = [
        *_clean_except_warning(),
        _row(FieldName.GOVERNMENT_WARNING, Verdict.MATCH),
        _row(FieldName.GOVERNMENT_WARNING, Verdict.MISMATCH),
    ]
    mismatch_first = [
        *_clean_except_warning(),
        _row(FieldName.GOVERNMENT_WARNING, Verdict.MISMATCH),
        _row(FieldName.GOVERNMENT_WARNING, Verdict.MATCH),
    ]
    assert agg.recommend(match_first).recommendation is Recommendation.NEEDS_REVIEW
    assert (
        agg.recommend(mismatch_first).recommendation
        is Recommendation.RETURN_FOR_CORRECTION
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open): _warning() takes the FIRST government-warning row, so a Match "
        "listed before a Mismatch hides the Mismatch from the disqualifying check and "
        "the run degrades to needs_review. The advice a label gets should not depend on "
        "the order two rows were appended in. Fix: resolve duplicates worst-of rather "
        "than first-of. Owner: api/rules/aggregate.py."
    ),
)
def test_a_warning_mismatch_drives_the_verdict_whatever_its_position() -> None:
    """Worst-of must hold within a field as well as across fields."""
    results = [
        *_clean_except_warning(),
        _row(FieldName.GOVERNMENT_WARNING, Verdict.MATCH),
        _row(FieldName.GOVERNMENT_WARNING, Verdict.MISMATCH),
    ]
    assert agg.recommend(results).recommendation is Recommendation.RETURN_FOR_CORRECTION


# --------------------------------------------------------------------------------------
# The live path is unaffected — which is why these are latent rather than shipped bugs
# --------------------------------------------------------------------------------------


def test_the_verification_pipeline_always_produces_exactly_one_warning_row() -> None:
    """Why none of the above is reachable through `POST /verify` today.

    `verify()` assembles the seven rows itself, in order, once each. This test is the
    reason the defects are latent — and the reason they stop being latent the moment
    somebody assembles a checklist anywhere else.
    """
    import inspect

    from api import verify as verify_module

    source = inspect.getsource(verify_module.verify)
    assert source.count("_warning_result(") == 1


def test_the_warning_evaluator_never_returns_not_applicable() -> None:
    """The other half of why defect 1 is latent.

    `warning.evaluate` has five return paths and none of them is Not applicable. If a
    sixth ever appears, defect 1 becomes live the same day.
    """
    import inspect

    from api.rules import warning as warning_module

    source = inspect.getsource(warning_module.evaluate)
    assert "NOT_APPLICABLE" not in source
