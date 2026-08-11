"""Shape guards on the domain types."""

from api.models import Commodity, FieldName, Recommendation, Verdict


def test_verdict_taxonomy_is_exactly_six_values() -> None:
    """MATCH-1. A seventh verdict is a product decision, not a convenience."""
    assert len(list(Verdict)) == 6


def test_verdict_values_match_the_prd() -> None:
    assert {v.value for v in Verdict} == {
        "match",
        "acceptable_variation",
        "mismatch",
        "missing",
        "unreadable",
        "not_applicable",
    }


def test_recommendation_is_three_states() -> None:
    assert {r.value for r in Recommendation} == {
        "ready_to_approve",
        "needs_review",
        "return_for_correction",
    }


def test_seven_mandatory_fields() -> None:
    """The brief's list of common required elements."""
    assert len(list(FieldName)) == 7


def test_three_commodities() -> None:
    assert {c.value for c in Commodity} == {"spirits", "wine", "malt"}
