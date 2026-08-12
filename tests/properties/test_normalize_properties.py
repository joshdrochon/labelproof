"""Properties of Tier-1 normalization and Tier-2 variation classification.

A hand-picked example set for a normalizer tests the inputs the author thought of.
The bugs live in the inputs nobody thought of — and one already did: an earlier
`fold_diacritics` ran NFD before NFKC, which split Hangul syllables into jamo that are
not combining marks, survived the filter, and left text that was no longer NFKC-stable.
No example in the suite used Hangul. A property test found it in seconds.

These properties are the standard that bug set. Each one is a statement about *every*
string, and each is here because violating it would change a verdict.
"""

# ruff: noqa: RUF001
# This file is *about* visually ambiguous characters — an acute accent used as an
# apostrophe, fullwidth forms, ligatures. Ruff's confusable check would flag every
# one of them, which is the point of the test rather than a defect in it.


from __future__ import annotations

import unicodedata

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from api.rules import normalize as norm
from api.rules.normalize import Variation

pytestmark = pytest.mark.property

#: Deliberately unrestricted. Label text arrives from a vision model reading imported
#: artwork; restricting the alphabet to Latin would test the case we already handle.
TEXT = st.text(max_size=80)

SETTINGS = settings(
    max_examples=400,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# --------------------------------------------------------------------------------------
# Equal-by-construction pairs
# --------------------------------------------------------------------------------------
#
# Filtering random string pairs for "these two normalize equal" throws away essentially
# every example — two random strings never match. So the pairs are *built* to match:
# take a base string and apply transformations Tier 1 is specified to fold away. Every
# pair is then a claim the product makes ("these are the same brand"), and the property
# is that the engine agrees.

def _is_a_clean_base(value: str) -> bool:
    """Is this string a fair starting point for a manufactured case variant?

    Two exclusions, both about the *generator* rather than the engine.

    **Case round-trip.** Turkish dotless ı uppercases to `I`, which casefolds to `i` —
    a different letter. Nothing in the pipeline ever calls `.upper()` on label text;
    only this test does, to manufacture a variant. Asserting that our own uppercasing
    is lossless would be asserting something about Python.

    **NFKC stability.** Compatibility characters — the `ﬀ` ligature, fullwidth `ＡＢＣ`,
    the `㎖` symbol — are folded by the NFKC at the top of `normalize`, and no
    `Variation` class describes that fold. The strings differ and Tier 2 reports
    nothing, so the pair passes as Match rather than Acceptable variation. That gap is
    real and is pinned in tests/regression/test_unicode_normalization.py; excluding
    these characters here keeps this file asserting the properties that hold.
    """
    # Pure punctuation is not a brand name and normalizes to the empty string, which
    # carries no verdict. Requiring a letter or digit keeps the generated pairs to
    # values a label could actually print.
    if not any(character.isalnum() for character in value):
        return False
    folded = value.casefold()
    if value.upper().casefold() != folded or value.lower().casefold() != folded:
        return False
    return all(
        unicodedata.normalize("NFKC", form) == form
        for form in (value, value.upper(), value.lower())
    )


#: The alphabet a US alcohol label actually carries: ASCII plus the accented Latin an
#: imported wine or a French producer name brings with it.
#:
#: Scoped on purpose. The idempotence, NFKC-stability and symmetry properties above run
#: against unrestricted `st.text()` — they must hold for anything. *This* alphabet
#: exists for the equal-by-construction pairs, where the test manufactures a case
#: variant with `.upper()`. Across the whole of Unicode that operation is not
#: information-preserving (Greek iota subscript expands, Turkish ı changes letter), and
#: a test that tripped over it would be reporting on Python's case mappings rather than
#: on the matching policy.
_LABEL_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " '-&."
    "áéíóúàèìòùâêîôûäëïöüãñõåøçÁÉÍÓÚÀÈÌÒÙÂÊÎÔÛÄËÏÖÜÃÑÕÅØÇ"
)

BASE = st.text(alphabet=_LABEL_ALPHABET, min_size=1, max_size=24).filter(_is_a_clean_base)

#: Apostrophes that survive NFKC and reach `unify_punctuation`. U+00B4 ACUTE ACCENT is
#: deliberately excluded: NFKC decomposes it before the map runs, and the result is a
#: space rather than an apostrophe. That is a defect, pinned in
#: tests/regression/test_unicode_normalization.py — not a property to assert here.
_FOLDABLE_APOSTROPHES = "'’‘‛ʼ′`"


def _upper(value: str) -> str:
    return value.upper()


def _lower(value: str) -> str:
    return value.lower()


def _pad(value: str) -> str:
    return f"  \t{value}\n "


def _double_spaces(value: str) -> str:
    return value.replace(" ", "   ")


def _terminal_punctuation(value: str) -> str:
    """Append a full stop *at the end of the text*, not after trailing whitespace.

    `rstrip` first because the transforms compose in any order. Appending to an
    already-padded string produces `"OLD TOM \\n."`, whose full stop is a separate
    token once whitespace collapses — and a floating token is not the terminal
    punctuation this transform is meant to model. No label prints one.
    """
    return f"{value.rstrip()}."


def _curly_apostrophes(value: str) -> str:
    return value.replace("'", "’")


def _prime_apostrophes(value: str) -> str:
    return value.replace("'", "ʼ")


def _hyphenate_across_a_line(value: str) -> str:
    """Split one word across two lines the way justified label copy does."""
    for i in range(1, len(value)):
        if value[i - 1].isalnum() and value[i].isalnum():
            return f"{value[:i]}-\n{value[i:]}"
    return value


#: `unique_by` matters: these compose, but they are not all idempotent. Hyphenating an
#: already-hyphenated string splits the same word twice, and `re.sub` does not match
#: overlapping spans — so the double break survives normalization. That is a property
#: of the generator, not a defect in the engine; a label is hyphenated once.
MEANING_PRESERVING = st.lists(
    st.sampled_from(
        [
            _upper,
            _lower,
            _pad,
            _double_spaces,
            _terminal_punctuation,
            _curly_apostrophes,
            _prime_apostrophes,
            _hyphenate_across_a_line,
        ]
    ),
    max_size=4,
    unique_by=lambda f: f.__name__,
)


def _apply(value: str, transforms: list[object]) -> str:
    for transform in transforms:
        value = transform(value)  # type: ignore[operator]
    return value


# --------------------------------------------------------------------------------------
# Idempotence — normalizing twice must equal normalizing once
# --------------------------------------------------------------------------------------


@SETTINGS
@given(TEXT)
def test_normalize_is_idempotent(value: str) -> None:
    """`normalize(normalize(x)) == normalize(x)` for every string.

    The whole comparison layer assumes this. If normalizing twice moved the string,
    then whether two labels matched would depend on how many times the pipeline had
    happened to normalize each side — a verdict that changes with call order.
    """
    once = norm.normalize(value)
    assert norm.normalize(once) == once


@SETTINGS
@given(TEXT)
def test_normalize_output_is_nfkc_stable(value: str) -> None:
    """Normalized text is a fixed point of NFKC.

    This is the property the Hangul defect broke: the output looked normalized and was
    not, so a later NFKC anywhere downstream would move it again.
    """
    once = norm.normalize(value)
    assert unicodedata.normalize("NFKC", once) == once


@SETTINGS
@given(TEXT)
def test_normalize_output_is_casefold_stable(value: str) -> None:
    """Casefolding normalized text is a no-op. Tier 1 has already folded case."""
    once = norm.normalize(value)
    assert once.casefold() == once


@SETTINGS
@given(TEXT)
def test_normalize_output_has_no_leading_or_trailing_whitespace(value: str) -> None:
    once = norm.normalize(value)
    assert once == once.strip()


@SETTINGS
@given(TEXT)
def test_normalize_output_has_no_double_spaces(value: str) -> None:
    assert "  " not in norm.normalize(value)


@pytest.mark.parametrize(
    "transform",
    [
        norm.collapse_whitespace,
        norm.unify_punctuation,
        norm.rejoin_hyphenation,
        norm.strip_terminal_punctuation,
    ],
    ids=lambda f: f.__name__,
)
@settings(max_examples=250, deadline=None)
@given(TEXT)
def test_each_transform_is_idempotent(transform: object, value: str) -> None:
    """Every stage of the pipeline is idempotent on its own.

    `fold_diacritics` is deliberately absent — it is only idempotent on NFKC-normalized
    input, which is what `normalize` gives it. The gap is pinned as a defect in
    `tests/regression/test_unicode_normalization.py`.
    """
    once = transform(value)  # type: ignore[operator]
    assert transform(once) == once  # type: ignore[operator]


@SETTINGS
@given(TEXT)
def test_fold_diacritics_is_idempotent_on_nfkc_input(value: str) -> None:
    """The contract `normalize` actually relies on, and the one that holds."""
    prepared = unicodedata.normalize("NFKC", value)
    once = norm.fold_diacritics(prepared)
    assert norm.fold_diacritics(once) == once


# --------------------------------------------------------------------------------------
# Equality is an equivalence relation
# --------------------------------------------------------------------------------------


@SETTINGS
@given(TEXT)
def test_equality_is_reflexive(value: str) -> None:
    assert norm.equal_after_normalization(value, value)


@SETTINGS
@given(TEXT, TEXT)
def test_equality_is_symmetric(left: str, right: str) -> None:
    """Which side is the label and which is the application cannot change the answer.

    `compare.py` passes the label first and the application second. If Tier 1 were
    asymmetric, swapping the arguments would swap the verdict — and the caller has no
    reason to think the order matters.
    """
    assert norm.equal_after_normalization(left, right) == norm.equal_after_normalization(
        right, left
    )


@settings(max_examples=300, deadline=None)
@given(BASE, MEANING_PRESERVING, MEANING_PRESERVING, MEANING_PRESERVING)
def test_equality_is_transitive_across_meaning_preserving_edits(
    base: str,
    first: list[object],
    second: list[object],
    third: list[object],
) -> None:
    """Three renderings of one brand are all mutually equal.

    Transitivity is what lets the pipeline compare a label against an application
    without caring how either side happened to be typed. Break it and two labels that
    each match the application can fail to match each other, which is a verdict that
    depends on nothing the agent can see.
    """
    a, b, c = (_apply(base, t) for t in (first, second, third))
    assert norm.equal_after_normalization(a, b)
    assert norm.equal_after_normalization(b, c)
    assert norm.equal_after_normalization(a, c)


@settings(max_examples=400, deadline=None)
@given(BASE, MEANING_PRESERVING)
def test_meaning_preserving_edits_never_change_the_normalized_form(
    base: str, transforms: list[object]
) -> None:
    """The specification of Tier 1, stated as a property.

    Capitalisation, apostrophe style, spacing, terminal punctuation and line-break
    hyphenation carry no meaning on a label. Any composition of them must leave the
    normalized form untouched — otherwise `STONE'S THROW` versus `Stone's Throw.`
    reaches an agent as a Mismatch on a compliant label.
    """
    assert norm.normalize(_apply(base, transforms)) == norm.normalize(base)


# --------------------------------------------------------------------------------------
# Tier 2 — variation classification
# --------------------------------------------------------------------------------------


@SETTINGS
@given(TEXT, TEXT)
def test_variation_classification_is_symmetric(left: str, right: str) -> None:
    """The *explanation* is symmetric too, not just the equality.

    Tier 2 is what the agent reads. "Punctuation differs" must mean the same thing
    whichever way round the two strings were handed in, or the note the agent sees
    depends on an implementation detail of the caller.
    """
    assert norm.classify_variation(left, right) == norm.classify_variation(right, left)


@settings(max_examples=400, deadline=None)
@given(BASE, MEANING_PRESERVING)
def test_no_variations_reported_exactly_when_strings_are_identical(
    base: str, transforms: list[object]
) -> None:
    """Tier 2 reports an empty list if and only if the strings are byte-identical.

    Both halves matter. Reporting no variation for strings that *do* differ is the
    silent pass MATCH-9 exists to prevent — the agent would see "Match" on text that
    is not the same text, with no note explaining why. Reporting a variation for
    identical strings is a false finding, which is how an agent learns to ignore the
    tool.
    """
    mutated = _apply(base, transforms)
    assume(norm.equal_after_normalization(base, mutated))
    assert (norm.classify_variation(base, mutated) == []) == (base == mutated)


@settings(max_examples=400, deadline=None)
@given(BASE, MEANING_PRESERVING)
def test_a_folded_difference_is_always_explained(
    base: str, transforms: list[object]
) -> None:
    """MATCH-9: nothing is folded away silently.

    Tier 1 passing on text that is not byte-identical means the engine made a judgment
    call. The agent has to see that call — an Acceptable variation with a note, never
    a Match. This is the property that keeps `classify_variation` honest as the
    normalizer grows: add a fold without a variation class and this fails.
    """
    mutated = _apply(base, transforms)
    assume(norm.equal_after_normalization(base, mutated))
    assume(base != mutated)
    variations = norm.classify_variation(base, mutated)
    assert variations, f"{base!r} vs {mutated!r} folded with no explanation"


@SETTINGS
@given(TEXT, TEXT)
def test_reported_variations_are_a_stable_ordered_subset(left: str, right: str) -> None:
    """Order is canonical and no class is reported twice.

    The note is rendered by joining these in order. A set would render differently run
    to run, and the same label would produce two different reports.
    """
    found = norm.classify_variation(left, right)
    canonical = list(Variation)
    assert found == [v for v in canonical if v in found]
    assert len(found) == len(set(found))


@SETTINGS
@given(TEXT, TEXT)
def test_every_reported_variation_has_an_agent_facing_note(
    left: str, right: str
) -> None:
    """UX-6: every class the engine can report has words written for an agent.

    A `KeyError` here would surface as a 500 on a label that merely used an unusual
    apostrophe.
    """
    found = norm.classify_variation(left, right)
    note = norm.variation_note(found)
    assert note.strip()
    assert all(v in norm.VARIATION_NOTES for v in found)


# --------------------------------------------------------------------------------------
# Targeted properties — each variation class is actually detected
# --------------------------------------------------------------------------------------

_LETTERS = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=2, max_size=12
)


@settings(max_examples=200, deadline=None)
@given(_LETTERS)
def test_case_difference_is_classified_as_case(word: str) -> None:
    assert norm.classify_variation(word.upper(), word.lower()) == [Variation.CASE]


@settings(max_examples=200, deadline=None)
@given(_LETTERS, _LETTERS)
def test_extra_spacing_is_classified_as_whitespace(left: str, right: str) -> None:
    """Covers the whitespace probe, which a case-only example set never reaches."""
    assert Variation.WHITESPACE in norm.classify_variation(
        f"{left} {right}", f"{left}   {right}"
    )


@settings(max_examples=200, deadline=None)
@given(_LETTERS)
def test_accent_difference_is_classified_as_diacritics(word: str) -> None:
    accented = f"{word}́"  # combining acute
    assert Variation.DIACRITICS in norm.classify_variation(accented, word)


@settings(max_examples=200, deadline=None)
@given(_LETTERS, _LETTERS)
def test_line_break_hyphenation_is_classified_as_hyphenation(
    left: str, right: str
) -> None:
    assert Variation.HYPHENATION in norm.classify_variation(
        f"{left}-\n{right}", f"{left}{right}"
    )


def test_multiple_variations_produce_a_combined_note() -> None:
    """The multi-class rendering path, which single-difference examples never reach."""
    note = norm.variation_note([Variation.CASE, Variation.PUNCTUATION])
    assert "case" in note and "punctuation" in note
    assert note.endswith("same text.")


def test_identical_values_are_described_as_identical() -> None:
    assert norm.variation_note([]) == "Values are identical."


# --------------------------------------------------------------------------------------
# The case the product is named after
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-02")
@settings(max_examples=100, deadline=None)
@given(st.sampled_from(_FOLDABLE_APOSTROPHES))
def test_every_apostrophe_in_the_zoo_folds_to_the_same_brand(mark: str) -> None:
    """Dave's case, across every apostrophe a designer might have typed.

    `STONE'S THROW` on the label against `Stone's Throw` in the application is one
    judgment call, not one per glyph the artwork happened to use.

    U+00B4 ACUTE ACCENT is excluded and is a defect, not an omission — see
    tests/regression/test_unicode_normalization.py.
    """
    label = f"STONE{mark}S THROW"
    assert norm.equal_after_normalization(label, "Stone's Throw")
    assert norm.classify_variation(label, "Stone's Throw") != []
