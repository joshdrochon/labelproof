"""Tier 1 / Tier 2 matching. TC-02 is the named regression."""

import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from api.rules.normalize import (
    Variation,
    classify_variation,
    contains_after_normalization,
    equal_after_normalization,
    normalize,
    surrounding_words,
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


# --- the value carried inside a longer printed statement ------------------------------


def test_the_words_around_the_value_are_quoted_from_the_label() -> None:
    """"DISTILLED IN CANADA" agrees about the country and shows the agent the lead-in.

    Quoted from the label's own text rather than from the normalized form: the whole
    point of the row is that a reviewer can see it said "Distilled in" and not something
    that changes the meaning.
    """
    assert surrounding_words("Distilled in Canada", "Canada") == "Distilled in"
    assert surrounding_words("Bottled by Old Tom Distillery", "Old Tom") == (
        "Bottled by … Distillery"
    )


def test_a_value_that_is_not_there_has_no_surrounding_words() -> None:
    """The empty answer, which the caller renders as "within a longer phrase" and no quote.

    Reachable on its own terms: `surrounding_words` is public and its contract is "the
    words around the value, if the value is there". `compare_text` happens to call it
    only after `contains_after_normalization` says yes, so nothing in the request path
    reaches this branch — which is exactly why it needs asserting here rather than being
    assumed impossible. A future caller that checks containment against a differently
    normalized pair reaches it immediately, and the honest answer is no quote, not a
    crash and not a wrong one.
    """
    assert not contains_after_normalization("Distilled in Canada", "Mexico")
    assert surrounding_words("Distilled in Canada", "Mexico") == ""


def test_the_quote_comes_from_the_label_even_when_the_match_was_made_on_a_rewrite() -> None:
    """`matched_on` locates; the label's own text is what gets shown.

    `expand_state_abbreviations` rewrites "BOTTLED BY OLD TOM, BARDSTOWN, KY" into
    "...bardstown, kentucky" to make the two spellings compare equal. That rewrite is
    fine to match on and absurd to print at an agent.
    """
    quoted = surrounding_words(
        "BOTTLED BY OLD TOM, BARDSTOWN, KY",
        "Old Tom, Bardstown, Kentucky",
        matched_on=(
            "bottled by old tom, bardstown, kentucky",
            "old tom, bardstown, kentucky",
        ),
    )
    assert quoted == "BOTTLED BY"


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
