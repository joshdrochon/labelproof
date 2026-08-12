"""Unicode defects in Tier-1 normalization — one fixed, two open.

**FIXED — NFD split Hangul into jamo that survived the combining-mark filter.**
`fold_diacritics` decomposed with NFD, dropped combining marks, and returned the result.
NFD splits a Hangul syllable into jamo, which are *not* combining marks, so they
survived the filter — leaving text that was no longer NFKC-stable. Imported labels carry
non-Latin scripts, so this was load-bearing rather than theoretical. No example in the
suite used Hangul; a property test found it in seconds. The fix recomposes with NFKC
afterwards.

**OPEN — five entries in the punctuation map are dead, and one of them matters.**
`normalize` applies NFKC *before* `unify_punctuation`, and NFKC has already rewritten
five of the map's keys by then. Four are harmless. `U+00B4 ACUTE ACCENT` is not: NFKC
turns it into space + combining acute, `fold_diacritics` strips the mark, and a brand
name typed with an acute accent as an apostrophe becomes a **space**. `STONE´S THROW`
normalizes to `stone s throw` and reaches an agent as a Mismatch against `Stone's
Throw`. Using `´` for an apostrophe is common in older artwork and in fonts with no
proper apostrophe glyph.

**OPEN — compatibility folds have no `Variation` class, so they pass as Match.**
That same leading NFKC folds ligatures (`ﬀ`), fullwidth forms (`ＯＬＤ ＴＯＭ`), and unit
symbols (`㎖` -> `ml`). `classify_variation` has no probe for it, so it returns `[]` and
the comparator returns **Match** rather than **Acceptable variation** — a judgment call
the agent never sees, which is what MATCH-9 exists to prevent.

Both open defects are `xfail(strict=True)`: they fail today and will turn this file red
the moment they are fixed, which is the signal to widen the property generators in
tests/properties/test_normalize_properties.py.
"""

# ruff: noqa: RUF001
# This file is *about* visually ambiguous characters — an acute accent used as an
# apostrophe, fullwidth forms, ligatures. Ruff's confusable check would flag every
# one of them, which is the point of the test rather than a defect in it.


from __future__ import annotations

import unicodedata

import pytest

from api.models import ExtractedField, FieldName, Verdict
from api.rules import compare
from api.rules import normalize as norm

pytestmark = pytest.mark.regression


# --------------------------------------------------------------------------------------
# FIXED: Hangul survived NFD's combining-mark filter
# --------------------------------------------------------------------------------------

#: Syllables NFD decomposes into jamo. Latin-only test data never reaches this path.
HANGUL = ["한", "국", "소", "주", "한국소주", "진로"]


@pytest.mark.parametrize("text", HANGUL)
def test_normalizing_hangul_produces_nfkc_stable_text(text: str) -> None:
    """The regression: the output must be a fixed point of NFKC.

    Before the fix `normalize` returned decomposed jamo. The string *looked* normalized
    and was not, so any later NFKC anywhere downstream moved it again and two readings
    of the same label could compare unequal depending on which path they took.
    """
    once = norm.normalize(text)
    assert unicodedata.normalize("NFKC", once) == once


@pytest.mark.parametrize("text", HANGUL)
def test_normalizing_hangul_is_idempotent(text: str) -> None:
    once = norm.normalize(text)
    assert norm.normalize(once) == once


@pytest.mark.parametrize("text", HANGUL)
def test_hangul_survives_normalization_rather_than_being_stripped(text: str) -> None:
    """Folding diacritics must not delete a script.

    An over-eager fix — stripping anything NFD produces — would empty the brand name
    and turn every Korean import into a Missing brand.
    """
    assert norm.normalize(text) != ""


def test_a_korean_brand_name_matches_itself() -> None:
    """The consequence at the layer an agent sees."""
    result = compare.compare_brand_name(
        ExtractedField(value="한국소주", confidence=0.95), "한국소주"
    )
    assert result.verdict is Verdict.MATCH


# --------------------------------------------------------------------------------------
# OPEN: NFKC runs before the punctuation map, killing five of its entries
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "character",
    ["´", "″", "‑", " ", " ", " "],
    ids=[
        "acute-accent", "double-prime", "non-breaking-hyphen",
        "nbsp", "figure-space", "narrow-nbsp",
    ],
)
def test_the_punctuation_map_contains_keys_nfkc_has_already_removed(
    character: str,
) -> None:
    """Diagnostic, not a failure: these map entries are unreachable from `normalize`.

    Passing means the entry is dead code. Four of the six are harmless because NFKC
    happens to produce something the rest of the pipeline handles anyway. The acute
    accent is not, and the next test is why.
    """
    # `str.maketrans` keys the table by codepoint, not by character.
    assert ord(character) in norm._QUOTE_MAP
    assert unicodedata.normalize("NFKC", character) != character


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open): normalize() applies NFKC before unify_punctuation, so the map's "
        "U+00B4 ACUTE ACCENT entry never fires. NFKC turns it into space + combining "
        "acute, fold_diacritics strips the mark, and the apostrophe becomes a space — so "
        "a brand typed with an acute accent reaches an agent as a false Mismatch. Fix: "
        "run unify_punctuation before the NFKC in normalize(), or map the decomposed "
        "form. Owner: api/rules/normalize.py."
    ),
)
def test_an_acute_accent_used_as_an_apostrophe_folds_to_an_apostrophe() -> None:
    """`STONE´S THROW` is Dave's case typed on a keyboard without a real apostrophe."""
    assert norm.normalize("STONE´S THROW") == norm.normalize("Stone's Throw")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open): the same U+00B4 defect, at the layer an agent sees. The brand "
        "row reads Mismatch on a compliant label. Owner: api/rules/normalize.py."
    ),
)
def test_an_acute_accent_brand_name_is_not_reported_as_a_mismatch() -> None:
    result = compare.compare_brand_name(
        ExtractedField(value="STONE´S THROW", confidence=0.95), "Stone's Throw"
    )
    assert result.verdict is not Verdict.MISMATCH


# --------------------------------------------------------------------------------------
# OPEN: compatibility folds are silent (MATCH-9)
# --------------------------------------------------------------------------------------

#: Pairs that NFKC folds together. Each is genuinely the same text — the defect is not
#: that they match, it is that they match *silently*.
COMPATIBILITY_PAIRS = [
    ("ﬀ", "ff"),
    ("ＯＬＤ ＴＯＭ", "OLD TOM"),
    ("㎖", "ml"),
    ("½", "1⁄2"),
]


@pytest.mark.parametrize(("compatibility", "plain"), COMPATIBILITY_PAIRS, ids=lambda p: p)
def test_compatibility_forms_do_compare_equal(compatibility: str, plain: str) -> None:
    """Established first: the equality itself is correct and should stay."""
    assert norm.equal_after_normalization(compatibility, plain)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open): normalize() applies NFKC unconditionally and classify_variation "
        "has no probe for it, so a compatibility fold reports no variation. The "
        "comparator therefore returns Match rather than Acceptable variation, and the "
        "agent never sees that the tool folded a ligature, a fullwidth form or a unit "
        "symbol — the silent pass MATCH-9 forbids. Fix: add a COMPATIBILITY variation "
        "class with a differs_without(keep_compatibility=True) probe. "
        "Owner: api/rules/normalize.py."
    ),
)
@pytest.mark.parametrize(("compatibility", "plain"), COMPATIBILITY_PAIRS, ids=lambda p: p)
def test_a_compatibility_fold_is_reported_as_a_variation(
    compatibility: str, plain: str
) -> None:
    """MATCH-9: nothing that differed is allowed to pass without a note."""
    assert norm.classify_variation(compatibility, plain) != []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open): the compatibility-fold defect at the layer an agent sees. "
        "`㎖` against `ml` shows a bare Match chip, so the agent is never told the tool "
        "read a unit symbol as two letters. Owner: api/rules/normalize.py."
    ),
)
def test_a_compatibility_fold_reaches_the_agent_as_an_acceptable_variation() -> None:
    result = compare.compare_text(
        FieldName.CLASS_TYPE,
        ExtractedField(value="ＯＬＤ ＴＯＭ", confidence=0.95),
        "OLD TOM",
        required=True,
    )
    assert result.verdict is Verdict.ACCEPTABLE_VARIATION
