"""Tier 1 / Tier 2 matching. TC-02 is the named regression."""

import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from api.rules.normalize import (
    Variation,
    classify_variation,
    equal_after_normalization,
    normalize,
    variation_note,
)

# --- TC-02: the case the whole matching policy exists for ----------------------------

@pytest.mark.tc("TC-02")
def test_stones_throw_curly_apostrophe_matches_after_normalization() -> None:
    label = "STONE’S THROW"        # curly apostrophe, all caps
    application = "Stone's Throw"       # straight apostrophe, title case
    assert equal_after_normalization(label, application)


@pytest.mark.tc("TC-02")
def test_stones_throw_is_classified_as_case_and_punctuation() -> None:
    variations = classify_variation("STONE’S THROW", "Stone's Throw")
    assert Variation.CASE in variations
    assert Variation.PUNCTUATION in variations


@pytest.mark.tc("TC-02")
def test_stones_throw_produces_a_visible_note() -> None:
    """MATCH-9: never silently merged into Match. The agent must see the call."""
    note = variation_note(classify_variation("STONE’S THROW", "Stone's Throw"))
    assert note
    assert note != "Values are identical."


# --- individual normalization classes -------------------------------------------------

@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("OLD TOM DISTILLERY", "Old Tom Distillery"),          # case
        ("Stone’s Throw", "Stone's Throw"),                # curly apostrophe
        ("Old  Tom   Distillery", "Old Tom Distillery"),        # whitespace
        ("  Old Tom Distillery  ", "Old Tom Distillery"),       # trim
        ("Rhône Valley", "Rhone Valley"),                  # diacritics
        ("Old Tom Distillery.", "Old Tom Distillery"),          # terminal punctuation
        ("DISTIL-\nLERY", "DISTILLERY"),                        # hyphen across lines
        ("Café", "Café"),                            # precomposed vs combining
    ],
)
def test_equal_after_normalization(left: str, right: str) -> None:
    assert equal_after_normalization(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Old Tom Distillery", "Young Tom Distillery"),
        ("Stone's Throw", "Stones Throw Away"),
        ("45", "40"),
        ("Kentucky Straight Bourbon", "Tennessee Straight Bourbon"),
    ],
)
def test_genuinely_different_values_do_not_match(left: str, right: str) -> None:
    assert not equal_after_normalization(left, right)


def test_identical_strings_have_no_variation() -> None:
    assert classify_variation("Old Tom", "Old Tom") == []
    assert variation_note([]) == "Values are identical."


def test_case_only_difference_is_classified_as_case_alone() -> None:
    assert classify_variation("OLD TOM", "Old Tom") == [Variation.CASE]


def test_hyphenation_is_detected() -> None:
    assert Variation.HYPHENATION in classify_variation("DISTIL-\nLERY", "DISTILLERY")


# --- properties -----------------------------------------------------------------------

@given(st.text(max_size=200))
def test_normalize_is_idempotent(value: str) -> None:
    once = normalize(value)
    assert normalize(once) == once


@given(st.text(max_size=200))
def test_normalize_never_raises(value: str) -> None:
    normalize(value)


@given(st.text(max_size=100))
def test_normalization_is_reflexive(value: str) -> None:
    assert equal_after_normalization(value, value)


@given(st.text(max_size=100), st.text(max_size=100))
def test_normalization_is_symmetric(left: str, right: str) -> None:
    assert equal_after_normalization(left, right) == equal_after_normalization(right, left)


@given(st.text(max_size=100))
def test_normalized_output_is_nfkc_stable(value: str) -> None:
    out = normalize(value)
    assert unicodedata.normalize("NFKC", out) == out


@given(st.text(max_size=100))
def test_normalized_output_has_no_leading_or_trailing_space(value: str) -> None:
    out = normalize(value)
    assert out == out.strip()
