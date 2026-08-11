"""Field comparators. The precedence order — Unreadable > Not applicable > Missing — is
the product, not an implementation detail, so it is tested directly."""

import pytest

from api.models import Commodity, ExtractedField, Verdict
from api.rules import compare
from api.rules.commodity import LabelContext


def E(value: str | None, *, legible: bool = True, confidence: float = 0.95) -> ExtractedField:
    return ExtractedField(value=value, legible=legible, confidence=confidence)


# --- brand name -----------------------------------------------------------------------

def test_identical_brand_is_a_match() -> None:
    r = compare.compare_brand_name(E("OLD TOM DISTILLERY"), "OLD TOM DISTILLERY")
    assert r.verdict is Verdict.MATCH
    assert r.tier == 1


@pytest.mark.tc("TC-02")
def test_stones_throw_is_acceptable_variation_with_a_note() -> None:
    r = compare.compare_brand_name(E("STONE’S THROW"), "Stone's Throw")
    assert r.verdict is Verdict.ACCEPTABLE_VARIATION
    assert r.tier == 2
    assert r.rationale
    assert r.rationale != "The label matches the application."


def test_different_brand_is_a_mismatch_quoting_both() -> None:
    r = compare.compare_brand_name(E("Young Tom"), "Old Tom")
    assert r.verdict is Verdict.MISMATCH
    assert "Young Tom" in r.rationale
    assert "Old Tom" in r.rationale


def test_absent_required_brand_is_missing() -> None:
    assert compare.compare_brand_name(E(None), "Old Tom").verdict is Verdict.MISSING


def test_illegible_brand_is_unreadable_not_missing() -> None:
    r = compare.compare_brand_name(E(None, legible=False), "Old Tom")
    assert r.verdict is Verdict.UNREADABLE
    assert "clearer image" in r.rationale


def test_unreadable_outranks_missing() -> None:
    """Precedence: could-not-read beats is-not-there even with no value."""
    r = compare.compare_brand_name(E(None, legible=False), "Old Tom")
    assert r.verdict is not Verdict.MISSING


def test_label_states_a_value_the_application_omits() -> None:
    r = compare.compare_brand_name(E("Old Tom"), "")
    assert r.verdict is Verdict.MISMATCH


# --- producer -------------------------------------------------------------------------

def test_producer_matches_when_name_and_address_agree() -> None:
    r = compare.compare_producer(
        E("Old Tom Distillery, Bardstown, Kentucky"), "Old Tom Distillery", "Bardstown, Kentucky"
    )
    assert r.verdict is Verdict.MATCH


def test_producer_tolerates_state_abbreviation() -> None:
    """`Bardstown, KY` and `Bardstown, Kentucky` are the same address."""
    r = compare.compare_producer(
        E("Old Tom Distillery, Bardstown, KY"), "Old Tom Distillery", "Bardstown, Kentucky"
    )
    assert r.verdict in (Verdict.MATCH, Verdict.ACCEPTABLE_VARIATION)


def test_producer_mismatch_on_different_city() -> None:
    r = compare.compare_producer(
        E("Old Tom Distillery, Louisville, KY"), "Old Tom Distillery", "Bardstown, Kentucky"
    )
    assert r.verdict is Verdict.MISMATCH


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP (LP-045). expand_state_abbreviations() rewrites any standalone word "
        "matching a state code, so 'Gin or Vodka' becomes 'gin oregon vodka' and "
        "'Made in Kentucky' becomes 'made indiana kentucky'. It is not a live false pass "
        "today because the normalizer is applied to BOTH sides of every comparison, so "
        "the corruption is symmetric; it becomes one the moment either side is expanded "
        "differently. The real fix needs address position (a code is a state only after "
        "a comma, or before a ZIP), which is a comparator decision this branch does not "
        "own. strict=True so this flips red the day someone fixes it and forgets to "
        "delete the marker."
    ),
)
def test_state_expansion_is_word_bounded() -> None:
    """`or` is Oregon's code but also an English word — must not corrupt other text."""
    assert "oregon" not in compare.expand_state_abbreviations("Gin or Vodka")
    expanded = compare.expand_state_abbreviations("Portland, OR")
    assert "oregon" in expanded


# --- country of origin (TC-19) --------------------------------------------------------

@pytest.mark.tc("TC-19")
def test_import_without_origin_on_label_is_missing() -> None:
    r = compare.compare_country_of_origin(E(None), "France", is_import=True)
    assert r.verdict is Verdict.MISSING


@pytest.mark.tc("TC-19")
def test_domestic_without_origin_is_not_applicable() -> None:
    r = compare.compare_country_of_origin(E(None), None, is_import=False)
    assert r.verdict is Verdict.NOT_APPLICABLE
    assert "imported" in r.rationale


def test_import_with_matching_origin() -> None:
    r = compare.compare_country_of_origin(
        E("Product of France"), "Product of France", is_import=True
    )
    assert r.verdict is Verdict.MATCH


# --- alcohol content ------------------------------------------------------------------

CTX = LabelContext(class_type="Kentucky Straight Bourbon Whiskey", application_abv=45.0)


def test_matching_abv_across_formats() -> None:
    r = compare.compare_alcohol_content(E("45% Alc./Vol. (90 Proof)"), 45.0, Commodity.SPIRITS, CTX)
    assert r.verdict is Verdict.MATCH


@pytest.mark.tc("TC-08")
def test_abv_mismatch_shows_the_delta() -> None:
    r = compare.compare_alcohol_content(E("40% Alc./Vol."), 45.0, Commodity.SPIRITS, CTX)
    assert r.verdict is Verdict.MISMATCH
    assert "5 percentage points" in r.rationale


@pytest.mark.tc("TC-08")
def test_abv_mismatch_shows_tolerance_as_context_not_an_excuse() -> None:
    r = compare.compare_alcohol_content(E("40% Alc./Vol."), 45.0, Commodity.SPIRITS, CTX)
    assert "does not excuse" in r.rationale


@pytest.mark.tc("TC-09")
def test_proof_inconsistency_rides_along_as_a_finding() -> None:
    r = compare.compare_alcohol_content(E("40% Alc./Vol. (90 Proof)"), 40.0, Commodity.SPIRITS, CTX)
    assert r.verdict is Verdict.MATCH  # matches the application
    assert any(f.code == "proof_abv_inconsistent" for f in r.findings)


@pytest.mark.tc("TC-22")
def test_spirits_abv_abbreviation_rides_along_as_a_finding() -> None:
    r = compare.compare_alcohol_content(E("45% ABV"), 45.0, Commodity.SPIRITS, CTX)
    assert r.verdict is Verdict.MATCH
    assert any(f.code == "spirits_abv_abbreviation" for f in r.findings)


@pytest.mark.tc("TC-18")
def test_malt_without_abv_is_not_applicable() -> None:
    ctx = LabelContext(class_type="India Pale Ale")
    r = compare.compare_alcohol_content(E(None), None, Commodity.MALT, ctx)
    assert r.verdict is Verdict.NOT_APPLICABLE


@pytest.mark.tc("TC-17")
def test_table_wine_without_abv_is_not_applicable() -> None:
    ctx = LabelContext(class_type="Table Wine", application_abv=12.0)
    r = compare.compare_alcohol_content(E(None), None, Commodity.WINE, ctx)
    assert r.verdict is Verdict.NOT_APPLICABLE


def test_spirits_without_abv_is_missing() -> None:
    r = compare.compare_alcohol_content(E(None), 45.0, Commodity.SPIRITS, CTX)
    assert r.verdict is Verdict.MISSING


# --- net contents ---------------------------------------------------------------------

def test_net_contents_match_across_units() -> None:
    r = compare.compare_net_contents(E("75 cl"), "750 mL", Commodity.SPIRITS)
    assert r.verdict is Verdict.MATCH


def test_net_contents_format_difference_still_matches() -> None:
    r = compare.compare_net_contents(E("750ML"), "750 mL", Commodity.SPIRITS)
    assert r.verdict is Verdict.MATCH


@pytest.mark.tc("TC-10")
def test_non_standard_fill_matches_the_application_and_still_raises_a_finding() -> None:
    r = compare.compare_net_contents(E("733 mL"), "733 mL", Commodity.SPIRITS)
    assert r.verdict is Verdict.MATCH
    assert any(f.code == "non_standard_fill" for f in r.findings)


def test_net_contents_mismatch() -> None:
    r = compare.compare_net_contents(E("700 mL"), "750 mL", Commodity.SPIRITS)
    assert r.verdict is Verdict.MISMATCH


def test_illegible_net_contents_is_unreadable() -> None:
    r = compare.compare_net_contents(E(None, legible=False), "750 mL", Commodity.SPIRITS)
    assert r.verdict is Verdict.UNREADABLE


# --- invariants -----------------------------------------------------------------------

def test_no_comparator_ever_fabricates_an_extracted_value() -> None:
    """LP-067: an unreadable field reports None, never a guess."""
    for r in [
        compare.compare_brand_name(E(None, legible=False), "Old Tom"),
        compare.compare_net_contents(E(None, legible=False), "750 mL", Commodity.SPIRITS),
        compare.compare_alcohol_content(E(None, legible=False), 45.0, Commodity.SPIRITS, CTX),
    ]:
        assert r.extracted is None
