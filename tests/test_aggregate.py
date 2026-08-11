"""Aggregate recommendation. Worst-of, with the warning statement ranked first."""

import pytest

from api.models import FieldName, FieldResult, Recommendation, Verdict
from api.rules import aggregate


def F(field: FieldName, verdict: Verdict) -> FieldResult:
    return FieldResult(
        field=field, verdict=verdict, extracted="x", expected="x",
        confidence=1.0, rationale="",
    )


ALL_MATCH = [F(f, Verdict.MATCH) for f in FieldName]


def _with(field: FieldName, verdict: Verdict) -> list[FieldResult]:
    return [F(f, verdict if f is field else Verdict.MATCH) for f in FieldName]


# --- clean ----------------------------------------------------------------------------

@pytest.mark.tc("TC-01")
def test_all_match_is_ready_to_approve() -> None:
    assert aggregate.recommend(ALL_MATCH).recommendation is Recommendation.READY_TO_APPROVE


def test_not_applicable_does_not_block_approval() -> None:
    results = _with(FieldName.COUNTRY_OF_ORIGIN, Verdict.NOT_APPLICABLE)
    assert aggregate.recommend(results).recommendation is Recommendation.READY_TO_APPROVE


# --- the warning outranks everything --------------------------------------------------

@pytest.mark.tc("TC-07")
def test_missing_warning_is_return_for_correction() -> None:
    results = _with(FieldName.GOVERNMENT_WARNING, Verdict.MISSING)
    agg = aggregate.recommend(results)
    assert agg.recommendation is Recommendation.RETURN_FOR_CORRECTION
    assert agg.driving_field is FieldName.GOVERNMENT_WARNING


@pytest.mark.tc("TC-03")
def test_warning_mismatch_is_return_for_correction() -> None:
    results = _with(FieldName.GOVERNMENT_WARNING, Verdict.MISMATCH)
    agg = aggregate.recommend(results)
    assert agg.recommendation is Recommendation.RETURN_FOR_CORRECTION
    assert agg.driving_field is FieldName.GOVERNMENT_WARNING


def test_warning_outranks_another_missing_field() -> None:
    """Two disqualifying fields — the warning is the one named."""
    results = [
        F(f, Verdict.MISSING if f in (FieldName.GOVERNMENT_WARNING, FieldName.BRAND_NAME)
          else Verdict.MATCH)
        for f in FieldName
    ]
    assert aggregate.recommend(results).driving_field is FieldName.GOVERNMENT_WARNING


def test_unreadable_warning_does_not_force_correction() -> None:
    """We could not read it. That needs eyes, not a rejection."""
    results = _with(FieldName.GOVERNMENT_WARNING, Verdict.UNREADABLE)
    assert aggregate.recommend(results).recommendation is Recommendation.NEEDS_REVIEW


# --- non-warning fields ---------------------------------------------------------------

@pytest.mark.tc("TC-08")
def test_non_warning_mismatch_is_needs_review_not_correction() -> None:
    """Deliberate: reserving the strongest label keeps it meaningful."""
    results = _with(FieldName.ALCOHOL_CONTENT, Verdict.MISMATCH)
    assert aggregate.recommend(results).recommendation is Recommendation.NEEDS_REVIEW


def test_missing_non_warning_field_is_return_for_correction() -> None:
    """A required element absent from the label is a different thing from a mismatch."""
    results = _with(FieldName.BRAND_NAME, Verdict.MISSING)
    agg = aggregate.recommend(results)
    assert agg.recommendation is Recommendation.RETURN_FOR_CORRECTION
    assert agg.driving_field is FieldName.BRAND_NAME


@pytest.mark.tc("TC-02")
def test_acceptable_variation_needs_review_never_silently_passes() -> None:
    """MATCH-9: the agent must see the judgment call."""
    results = _with(FieldName.BRAND_NAME, Verdict.ACCEPTABLE_VARIATION)
    assert aggregate.recommend(results).recommendation is Recommendation.NEEDS_REVIEW


@pytest.mark.tc("TC-12")
def test_unreadable_field_is_needs_review() -> None:
    results = _with(FieldName.NET_CONTENTS, Verdict.UNREADABLE)
    assert aggregate.recommend(results).recommendation is Recommendation.NEEDS_REVIEW


def test_mismatch_outranks_acceptable_variation_as_driving_field() -> None:
    results = [
        F(FieldName.BRAND_NAME, Verdict.ACCEPTABLE_VARIATION),
        F(FieldName.NET_CONTENTS, Verdict.MISMATCH),
    ]
    assert aggregate.recommend(results).driving_field is FieldName.NET_CONTENTS


# --- phrasing -------------------------------------------------------------------------

def test_every_recommendation_defers_to_the_agent() -> None:
    """HITL-1 / SCOPE-3 — the app recommends, it never decides."""
    for results in [ALL_MATCH,
                    _with(FieldName.GOVERNMENT_WARNING, Verdict.MISSING),
                    _with(FieldName.BRAND_NAME, Verdict.MISMATCH),
                    _with(FieldName.NET_CONTENTS, Verdict.UNREADABLE)]:
        assert "decision is yours" in aggregate.recommend(results).rationale


def test_rationale_counts_the_rows_needing_attention() -> None:
    results = [
        F(FieldName.BRAND_NAME, Verdict.ACCEPTABLE_VARIATION),
        F(FieldName.NET_CONTENTS, Verdict.UNREADABLE),
        F(FieldName.CLASS_TYPE, Verdict.MATCH),
    ]
    assert "2 rows need" in aggregate.recommend(results).rationale


def test_single_row_is_phrased_in_the_singular() -> None:
    results = _with(FieldName.BRAND_NAME, Verdict.ACCEPTABLE_VARIATION)
    assert "1 row needs" in aggregate.recommend(results).rationale


def test_no_rationale_uses_jargon() -> None:
    for results in [ALL_MATCH, _with(FieldName.GOVERNMENT_WARNING, Verdict.MISSING)]:
        rationale = aggregate.recommend(results).rationale
        assert "_" not in rationale
        assert "verdict" not in rationale.lower()


# --- triage ordering ------------------------------------------------------------------

def test_warning_sorts_first_even_when_it_matched() -> None:
    ordered = aggregate.triage_order(ALL_MATCH)
    assert ordered[0].field is FieldName.GOVERNMENT_WARNING


def test_more_serious_rows_sort_above_less_serious() -> None:
    results = [
        F(FieldName.BRAND_NAME, Verdict.MATCH),
        F(FieldName.NET_CONTENTS, Verdict.MISSING),
        F(FieldName.CLASS_TYPE, Verdict.ACCEPTABLE_VARIATION),
    ]
    ordered = aggregate.triage_order(results)
    assert [r.verdict for r in ordered] == [
        Verdict.MISSING, Verdict.ACCEPTABLE_VARIATION, Verdict.MATCH
    ]


def test_attention_fields_excludes_everything_clean() -> None:
    results = [
        F(FieldName.BRAND_NAME, Verdict.MATCH),
        F(FieldName.CLASS_TYPE, Verdict.NOT_APPLICABLE),
        F(FieldName.NET_CONTENTS, Verdict.MISMATCH),
    ]
    attention = aggregate.attention_fields(results)
    assert [r.field for r in attention] == [FieldName.NET_CONTENTS]


def test_triage_order_is_stable_and_total() -> None:
    assert len(aggregate.triage_order(ALL_MATCH)) == len(ALL_MATCH)


# --- degenerate -----------------------------------------------------------------------

def test_no_results_never_reports_ready_to_approve() -> None:
    """Nothing checked must not read as everything passed."""
    assert aggregate.recommend([]).recommendation is not Recommendation.READY_TO_APPROVE


def test_every_verdict_has_a_severity() -> None:
    """A verdict with no severity would sort unpredictably and silently."""
    for verdict in Verdict:
        assert verdict in aggregate._SEVERITY
