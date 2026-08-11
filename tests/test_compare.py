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


# --- state expansion: the false-pass class it used to be -------------------------------
#
# These are two tests, not one, on purpose. They were one, and the negative assertion came
# first, so the day someone "fixed" the false pass by deleting `"or": "oregon"` from the
# table the positive assertion never ran and the suite stayed green. A test whose second
# half is unreachable when its first half is satisfied is not covering the second half.


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("Portland, OR", "portland, oregon"),
        ("Bardstown, KY", "bardstown, kentucky"),
        ("Bardstown, KY 40004", "bardstown, kentucky 40004"),
        ("Bardstown, KY 40004-1234", "bardstown, kentucky 40004-1234"),
        ("Windsor, CA, USA", "windsor, california, usa"),
        ("Ponce, PR", "ponce, puerto rico"),
    ],
    ids=["end", "no-zip", "zip", "zip-plus-four", "trailing-country", "territory"],
)
def test_a_state_code_in_a_state_position_still_expands(address: str, expected: str) -> None:
    """The feature has to keep working. `Bardstown, KY` is `Bardstown, Kentucky`."""
    assert compare.expand_state_abbreviations(address) == expected


@pytest.mark.parametrize(
    ("text", "must_not_contain"),
    [
        ("Gin or Vodka", "oregon"),
        ("Made in Kentucky", "indiana"),
        ("Old Tom Distilling Co", "colorado"),
        ("Old Tom Distilling Co.", "colorado"),
        ("La Crema Winery", "louisiana"),
        ("Casa de Campo", "delaware"),
        ("In-N-Out Spirits", "indiana"),
        ("Mo's Distillery", "missouri"),
        ("Hi-Time Wine Cellars", "hawaii"),
    ],
    ids=[
        "english-or",
        "english-in",
        "company-suffix",
        "company-suffix-dotted",
        "french-article",
        "spanish-article",
        "hyphenated-name",
        "possessive",
        "hyphenated-prefix",
    ],
)
def test_a_state_code_outside_a_state_position_is_left_alone(
    text: str, must_not_contain: str
) -> None:
    """`or` is Oregon's code and also an English word; `Co` ends half the producers alive."""
    assert must_not_contain not in compare.expand_state_abbreviations(text)


@pytest.mark.parametrize(
    ("label", "app_name", "app_address"),
    [
        ("La Crema Winery, Windsor, CA", "Louisiana Crema Winery", "Windsor, CA"),
        ("Casa de Campo, Ponce, PR", "Casa Delaware Campo", "Ponce, PR"),
        ("Mo's Distillery, Bend, OR", "Missouri's Distillery", "Bend, OR"),
        ("In-N-Out Spirits, Baltimore, MD", "Indiana-N-Out Spirits", "Baltimore, MD"),
        (
            "Old Tom Distilling Co, Bardstown, KY",
            "Old Tom Distilling Colorado",
            "Bardstown, KY",
        ),
    ],
    ids=["la", "de", "mo", "in", "co"],
)
def test_two_different_producers_never_collide_into_a_match(
    label: str, app_name: str, app_address: str
) -> None:
    """The regression that mattered: distinct producers reported as an exact Tier-1 Match.

    Symmetric normalization prevents false MISMATCHes and does nothing for false MATCHes,
    because a many-to-one rewrite maps two different inputs onto one string. Every row
    here returned `match`, tier 1, "The label matches the application." The last one is
    the one that would really have shipped: every producer ending in "Co" was becoming
    "colorado" (FIELD-5).
    """
    r = compare.compare_producer(E(label), app_name, app_address)

    assert r.verdict is Verdict.MISMATCH, f"{label!r} passed as {app_name!r}"


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
