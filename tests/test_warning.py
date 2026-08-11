"""Government warning statement. The highest-stakes module in the codebase.

A false Match here is the worst possible failure of this product, so the tests weight
toward proving the tool does NOT pass things it should catch.
"""

import pytest

from api import canon
from api.models import Verdict, WarningTypography
from api.rules import warning

GOOD = WarningTypography(
    header_is_all_caps=True, header_is_bold=True, body_is_bold=False, contrast_ok=True
)


def _retitled(header: str) -> str:
    return canon.CANONICAL_WARNING.replace("GOVERNMENT WARNING:", header, 1)


# --- the clean case -------------------------------------------------------------------

@pytest.mark.tc("TC-01")
def test_exact_warning_with_good_typography_matches() -> None:
    result = warning.evaluate(canon.CANONICAL_WARNING, GOOD)
    assert result.verdict is Verdict.MATCH
    assert not result.findings


def test_line_breaks_do_not_break_the_match() -> None:
    """Line wrapping is label layout, not a wording difference."""
    wrapped = canon.CANONICAL_WARNING.replace(" ", "\n", 6)
    assert warning.evaluate(wrapped, GOOD).verdict is Verdict.MATCH


# --- TC-03: Jenny's catch -------------------------------------------------------------

@pytest.mark.tc("TC-03")
def test_title_case_header_is_a_violation() -> None:
    """The named regression. She rejected a real label for exactly this."""
    result = warning.evaluate(_retitled("Government Warning:"), GOOD)
    assert result.verdict is not Verdict.MATCH
    assert any(f.code == "warning_header_not_all_caps" for f in result.findings)


@pytest.mark.tc("TC-03")
@pytest.mark.parametrize("header", ["Government Warning:", "government warning:", "GoVeRnMeNt WaRnInG:"])
def test_any_non_uppercase_header_is_caught(header: str) -> None:
    result = warning.evaluate(_retitled(header), GOOD)
    assert result.verdict is not Verdict.MATCH


@pytest.mark.tc("TC-03")
def test_title_case_header_names_what_the_label_actually_said() -> None:
    result = warning.evaluate(_retitled("Government Warning:"), GOOD)
    finding = next(f for f in result.findings if f.code == "warning_header_not_all_caps")
    assert "Government Warning" in finding.message


# --- TC-04: the inverse rule ----------------------------------------------------------

@pytest.mark.tc("TC-04")
def test_bold_body_is_a_violation() -> None:
    """WARN-7 — 16.22 requires the remainder NOT be bold."""
    signals = WarningTypography(header_is_all_caps=True, header_is_bold=True, body_is_bold=True)
    result = warning.evaluate(canon.CANONICAL_WARNING, signals)
    assert result.verdict is Verdict.MISMATCH
    assert any(f.code == "warning_body_is_bold" for f in result.findings)


def test_non_bold_header_is_a_violation() -> None:
    signals = WarningTypography(header_is_all_caps=True, header_is_bold=False, body_is_bold=False)
    result = warning.evaluate(canon.CANONICAL_WARNING, signals)
    assert result.verdict is Verdict.MISMATCH
    assert any(f.code == "warning_header_not_bold" for f in result.findings)


# --- TC-05: rewording -----------------------------------------------------------------

@pytest.mark.tc("TC-05")
def test_reworded_warning_is_a_mismatch_with_a_diff() -> None:
    reworded = canon.CANONICAL_WARNING.replace(
        "women should not drink alcoholic beverages during pregnancy",
        "pregnant women should not drink alcoholic beverages",
    )
    result = warning.evaluate(reworded, GOOD)
    assert result.verdict is Verdict.MISMATCH
    assert any(seg.is_difference for seg in result.diff)


@pytest.mark.tc("TC-05")
def test_diff_identifies_the_substituted_words() -> None:
    reworded = canon.CANONICAL_WARNING.replace("birth defects", "health issues")
    segments = warning.tokenized_diff(reworded)
    replaced = [s for s in segments if s.op == "replace"]
    assert replaced
    assert any("defects" in " ".join(s.expected) for s in replaced)


def test_omitted_words_are_reported_as_missing_text() -> None:
    """A label carrying only clause (1) — a real truncation, whole words dropped."""
    clause_two = canon.CANONICAL_WARNING.index("(2)")
    truncated = canon.CANONICAL_WARNING[:clause_two].strip()
    result = warning.evaluate(truncated, GOOD)
    assert result.verdict is Verdict.MISMATCH
    assert "missing the words" in result.rationale


def test_partial_word_truncation_is_reported_as_a_substitution() -> None:
    """Cutting mid-phrase mangles a token, so it diffs as a replacement, not a deletion."""
    mangled = canon.CANONICAL_WARNING.replace(" and may cause health problems", "")
    result = warning.evaluate(mangled, GOOD)
    assert result.verdict is Verdict.MISMATCH
    assert "where the required text reads" in result.rationale


def test_added_words_are_reported() -> None:
    padded = canon.CANONICAL_WARNING + " Please drink responsibly."
    result = warning.evaluate(padded, GOOD)
    assert result.verdict is Verdict.MISMATCH
    assert "adds the words" in result.rationale


# --- TC-07: missing -------------------------------------------------------------------

@pytest.mark.tc("TC-07")
@pytest.mark.parametrize("text", [None, "", "   "])
def test_absent_warning_is_missing_not_mismatch(text: str | None) -> None:
    result = warning.evaluate(text, GOOD)
    assert result.verdict is Verdict.MISSING
    assert any(f.severity == "critical" for f in result.findings)


# --- fail closed ----------------------------------------------------------------------

def test_illegible_is_unreadable_and_is_never_a_match() -> None:
    """A glare patch over the warning must never read as a pass."""
    result = warning.evaluate(canon.CANONICAL_WARNING, GOOD, legible=False)
    assert result.verdict is Verdict.UNREADABLE


def test_illegible_outranks_absent() -> None:
    """Could-not-read and is-not-there are different findings."""
    assert warning.evaluate(None, GOOD, legible=False).verdict is Verdict.UNREADABLE


def test_unknown_typography_never_yields_match() -> None:
    """PRD §Constraints: uncertainty about the warning is never Match."""
    result = warning.evaluate(canon.CANONICAL_WARNING, WarningTypography())
    assert result.verdict is not Verdict.MATCH
    assert all(f.severity == "unverified" for f in result.findings)


def test_unknown_typography_says_check_by_eye() -> None:
    result = warning.evaluate(canon.CANONICAL_WARNING, WarningTypography())
    assert "eye" in result.rationale.lower()


def test_no_signals_at_all_does_not_crash_or_pass() -> None:
    assert warning.evaluate(canon.CANONICAL_WARNING).verdict is not Verdict.MATCH


# --- casefolding trap -----------------------------------------------------------------

def test_warning_comparison_is_case_sensitive() -> None:
    """Reusing the brand-name normalizer here would erase Jenny's catch entirely."""
    assert not warning.is_verbatim(canon.CANONICAL_WARNING.lower())


def test_punctuation_differences_are_not_folded_away() -> None:
    assert not warning.is_verbatim(canon.CANONICAL_WARNING.replace(":", ";"))


# --- WARN-9 honesty -------------------------------------------------------------------

def test_type_size_context_admits_it_cannot_be_verified() -> None:
    text = warning.type_size_context(750.0)
    assert "2 mm" in text
    assert "not verifiable" in text


def test_type_size_context_without_container_size() -> None:
    assert "unknown" in warning.type_size_context(None)
