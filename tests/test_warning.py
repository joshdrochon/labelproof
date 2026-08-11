"""Government warning statement. The highest-stakes module in the codebase.

A false Match here is the worst possible failure of this product, so the tests weight
toward proving the tool does NOT pass things it should catch.
"""

import pytest

from api import canon
from api.models import Verdict, WarningTypography
from api.rules import typography, warning

#: A label the extractor read confidently and completely: every 16.22 signal answered,
#: and answered the compliant way. This is the only shape of input that can reach Match.
GOOD = WarningTypography(
    header_is_all_caps=True,
    header_is_bold=True,
    body_is_bold=False,
    contrast_ok=True,
    relative_size=1.0,
)


def _retitled(header: str) -> str:
    return canon.CANONICAL_WARNING.replace("GOVERNMENT WARNING:", header, 1)


def _asserted(result: warning.WarningResult) -> list[str]:
    """Findings that claim something, as opposed to admitting something."""
    return [f.code for f in result.findings if f.severity in typography.ASSERTED_SEVERITIES]


# --- the clean case -------------------------------------------------------------------

@pytest.mark.tc("TC-01")
def test_exact_warning_with_good_typography_matches() -> None:
    result = warning.evaluate(canon.CANONICAL_WARNING, GOOD)
    assert result.verdict is Verdict.MATCH
    assert _asserted(result) == []


@pytest.mark.tc("TC-01")
def test_a_match_still_admits_what_it_could_not_measure() -> None:
    """WARN-9. Nothing is broken, and the tool still says what it did not check."""
    signals = WarningTypography(header_is_bold=True, body_is_bold=False)
    result = warning.evaluate(canon.CANONICAL_WARNING, signals)
    assert result.verdict is Verdict.MATCH
    assert _asserted(result) == []
    assert any(f.severity == typography.SEVERITY_CONTEXT for f in result.findings)


def test_line_breaks_do_not_break_the_match() -> None:
    """Line wrapping is label layout, not a wording difference."""
    wrapped = canon.CANONICAL_WARNING.replace(" ", "\n", 6)
    assert warning.evaluate(wrapped, GOOD).verdict is Verdict.MATCH


# --- TC-03: Jenny's catch -------------------------------------------------------------
#
# LP-208, the named regression. A compliance agent rejected a real label because its
# warning heading read `Government Warning:` in title case instead of capitals. She
# caught it by eye, off a printed checklist, and it is the headline claim of this
# product that the machine catches it too. Everything below is one test case seen from a
# different angle; if any of them ever goes green-to-red, the product has lost the thing
# it was built to do.


@pytest.mark.tc("TC-03")
def test_title_case_header_is_a_violation() -> None:
    """The named regression. She rejected a real label for exactly this."""
    result = warning.evaluate(_retitled("Government Warning:"), GOOD)
    assert result.verdict is not Verdict.MATCH
    assert any(f.code == "warning_header_not_all_caps" for f in result.findings)


@pytest.mark.tc("TC-03")
def test_jennys_catch_end_to_end() -> None:
    """The whole chain, from the string on the label to the sentence on the screen."""
    result = warning.evaluate(_retitled("Government Warning:"), GOOD)

    assert result.verdict is Verdict.MISMATCH           # not a pass, not a maybe
    assert "warning_header_not_all_caps" in _asserted(result)
    assert result.kind == warning.CASING                # named, so the agent knows why
    assert any(seg.is_difference for seg in result.diff)  # provable to the applicant
    assert "Government Warning" in result.rationale     # quotes what the label said


@pytest.mark.tc("TC-03")
def test_jennys_catch_survives_perfect_typography() -> None:
    """Bold, contrasting, correctly sized — and still rejected, because of the capitals.

    This is the test that would fail if anyone ever decided the heading check was
    "covered by" the typography signals.
    """
    perfect = WarningTypography(
        header_is_all_caps=True,   # the extractor is wrong, and the text proves it
        header_is_bold=True,
        body_is_bold=False,
        contrast_ok=True,
        relative_size=1.2,
    )
    assert warning.evaluate(_retitled("Government Warning:"), perfect).verdict is (
        Verdict.MISMATCH
    )


@pytest.mark.tc("TC-03")
def test_jennys_catch_is_not_a_casefolding_accident() -> None:
    """If this module ever starts calling normalize(), this is the test that dies."""
    from api.rules import normalize

    title_case = _retitled("Government Warning:")
    assert normalize.normalize(title_case) == normalize.normalize(canon.CANONICAL_WARNING)
    assert not warning.is_verbatim(title_case)


@pytest.mark.tc("TC-03")
@pytest.mark.parametrize(
    "header", ["Government Warning:", "government warning:", "GoVeRnMeNt WaRnInG:"]
)
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


# --- LP-203 / LP-209 / LP-210: what kind of difference is it? -------------------------

@pytest.mark.tc("TC-05")
def test_a_paraphrase_is_classified_as_rewording() -> None:
    reworded = canon.CANONICAL_WARNING.replace(
        "women should not drink alcoholic beverages during pregnancy",
        "pregnant women should not drink alcoholic beverages",
    )
    result = warning.evaluate(reworded, GOOD)
    assert result.kind == warning.REWORDING
    assert "warning_text_rewording" in _asserted(result)


@pytest.mark.tc("TC-05")
def test_the_rewording_finding_forbids_paraphrase_explicitly() -> None:
    """"But it means the same thing" is the argument this sentence has to answer."""
    reworded = canon.CANONICAL_WARNING.replace("birth defects", "harm to the baby")
    finding = next(
        f for f in warning.evaluate(reworded, GOOD).findings
        if f.code == "warning_text_rewording"
    )
    assert "paraphrasing" in finding.message
    assert finding.citation == "27 CFR 16.21"


def test_a_statement_that_stops_halfway_is_a_truncation() -> None:
    """LP-210. The commonest partial warning: clause (1) printed, clause (2) dropped."""
    clause_two = canon.CANONICAL_WARNING.index("(2)")
    result = warning.evaluate(canon.CANONICAL_WARNING[:clause_two].strip(), GOOD)
    assert result.kind == warning.TRUNCATED
    assert "warning_text_truncated" in _asserted(result)


def test_a_truncation_that_took_the_last_punctuation_with_it_is_still_a_truncation() -> None:
    """Cutting a tail usually leaves a full stop the printer added. Still truncation."""
    cut = canon.CANONICAL_WARNING.replace(" and may cause health problems", "")
    assert warning.classify(cut).kind == warning.TRUNCATED


def test_the_truncation_message_names_the_missing_clause() -> None:
    clause_two = canon.CANONICAL_WARNING.index("(2)")
    finding = next(
        f for f in warning.evaluate(canon.CANONICAL_WARNING[:clause_two].strip()).findings
        if f.code == "warning_text_truncated"
    )
    assert "second numbered part" in finding.message


def test_words_dropped_from_the_middle_are_an_omission_not_a_truncation() -> None:
    """The difference matters: one label stops early, the other quietly edits."""
    edited = canon.CANONICAL_WARNING.replace("or operate machinery, ", "")
    assert warning.classify(edited).kind == warning.OMISSION


def test_extra_words_are_an_addition() -> None:
    padded = canon.CANONICAL_WARNING + " Please drink responsibly."
    result = warning.evaluate(padded, GOOD)
    assert result.kind == warning.ADDITION
    assert "warning_text_addition" in _asserted(result)


def test_the_same_words_in_a_different_order_are_caught_as_reordering() -> None:
    """The PRD names reordering as its own evasion. A bag-of-words check would pass it."""
    scrambled = " ".join(reversed(warning.tokenize(canon.CANONICAL_WARNING)))
    assert warning.classify(scrambled).kind == warning.REORDERING


def test_a_case_only_difference_is_named_as_such() -> None:
    lowered = canon.CANONICAL_WARNING.replace("Surgeon General", "surgeon general")
    assert warning.classify(lowered).kind == warning.CASING


def test_a_punctuation_only_difference_is_named_as_such() -> None:
    repunctuated = canon.CANONICAL_WARNING.replace("birth defects.", "birth defects;")
    assert warning.classify(repunctuated).kind == warning.PUNCTUATION


def test_a_case_or_punctuation_difference_is_still_a_mismatch() -> None:
    """Naming the difference gently is not the same as forgiving it."""
    for altered in (
        canon.CANONICAL_WARNING.replace("Surgeon General", "surgeon general"),
        canon.CANONICAL_WARNING.replace("birth defects.", "birth defects;"),
    ):
        assert warning.evaluate(altered, GOOD).verdict is Verdict.MISMATCH


def test_the_canonical_statement_classifies_as_verbatim() -> None:
    comparison = warning.classify(canon.CANONICAL_WARNING)
    assert comparison.kind == warning.VERBATIM
    assert comparison.is_verbatim
    assert warning.text_findings(comparison) == []


def test_every_kind_has_a_message_and_a_finding_code() -> None:
    """A classification with no sentence behind it is a verdict with no explanation."""
    kinds = [
        warning.TRUNCATED, warning.OMISSION, warning.ADDITION, warning.REORDERING,
        warning.CASING, warning.PUNCTUATION, warning.REWORDING,
    ]
    for kind in kinds:
        findings = warning.text_findings(warning.TextComparison(kind=kind))
        assert findings[0].code == f"warning_text_{kind}"
        assert findings[0].message.strip()


def test_the_comparison_lists_the_words_that_moved() -> None:
    """LP-203: the diff is evidence, so it has to be inspectable, not just renderable."""
    comparison = warning.classify(canon.CANONICAL_WARNING.replace("birth defects", "harm"))
    assert "defects" in " ".join(comparison.missing_words)
    assert "harm" in " ".join(comparison.added_words)


# --- LP-205: the heading, character for character -------------------------------------

def test_the_heading_must_end_in_a_colon() -> None:
    """27 CFR 16.21 punctuates the heading with a colon and nothing else.

    16.22 quotes the phrase as `"GOVERNMENT WARNING,"` when stating the bold rule, but
    that comma is American typography inside the closing quote — it belongs to the
    regulation's own sentence, not to the required phrase. Verified 2026-08-11 (LP-328).
    """
    with_comma = canon.CANONICAL_WARNING.replace("WARNING:", "WARNING,", 1)
    findings = warning.check_header_caps(with_comma, GOOD)
    assert [f.code for f in findings] == ["warning_header_punctuation"]


def test_a_heading_with_no_punctuation_at_all_is_caught() -> None:
    bare = canon.CANONICAL_WARNING.replace("WARNING:", "WARNING", 1)
    assert [f.code for f in warning.check_header_caps(bare, GOOD)] == [
        "warning_header_punctuation"
    ]


def test_the_heading_is_found_even_when_it_is_not_the_first_thing_read() -> None:
    """An extractor that returns a neighbouring line must not skip the caps check."""
    with_neighbour = f"Bottled in Kentucky. {_retitled('Government Warning:')}"
    assert "warning_header_not_all_caps" in {
        f.code for f in warning.check_header_caps(with_neighbour, GOOD)
    }


def test_a_warning_with_no_heading_at_all_is_reported() -> None:
    body_only = canon.WARNING_BODY
    assert [f.code for f in warning.check_header_caps(body_only, GOOD)] == [
        "warning_header_missing"
    ]


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
    assert result.verdict is Verdict.UNREADABLE
    assert _asserted(result) == []


def test_unknown_typography_says_check_by_eye() -> None:
    result = warning.evaluate(canon.CANONICAL_WARNING, WarningTypography())
    assert "eye" in result.rationale.lower()


def test_no_signals_at_all_does_not_crash_or_pass() -> None:
    assert warning.evaluate(canon.CANONICAL_WARNING).verdict is not Verdict.MATCH


def test_unconfirmed_typography_is_not_called_an_acceptable_variation() -> None:
    """There is no acceptable variation of the statement required by 16.21.

    The verdict taxonomy has six values and five of them can apply here. A warning row
    reading "Acceptable variation" would be this tool telling an agent that a variation
    of the government warning was fine, which is the opposite of what it means.
    """
    for signals in (
        WarningTypography(),
        WarningTypography(header_is_bold=None, body_is_bold=False),
        WarningTypography(header_is_bold=True, body_is_bold=None),
        WarningTypography(header_is_bold=True, body_is_bold=False, contrast_ok=False),
        WarningTypography(header_is_bold=True, body_is_bold=False, relative_size=0.3),
    ):
        result = warning.evaluate(canon.CANONICAL_WARNING, signals)
        assert result.verdict is not Verdict.ACCEPTABLE_VARIATION


def test_unconfirmed_bold_reads_as_not_verified_not_as_a_defect() -> None:
    """"We could not tell" and "the label is wrong" are different sentences."""
    result = warning.evaluate(
        canon.CANONICAL_WARNING, WarningTypography(header_is_bold=None, body_is_bold=False)
    )
    assert result.verdict is Verdict.UNREADABLE
    assert "could not be confirmed" in result.rationale


# --- WARN-5 prominence routing (TC-06) ------------------------------------------------

@pytest.mark.tc("TC-06")
def test_buried_warning_goes_to_a_human_not_back_to_the_applicant() -> None:
    """TC-06 expects Needs review. Prominence is a judgement about a photograph."""
    signals = WarningTypography(
        header_is_bold=True, body_is_bold=False, relative_size=0.45, contrast_ok=False
    )
    result = warning.evaluate(canon.CANONICAL_WARNING, signals)
    assert result.verdict is Verdict.UNREADABLE
    assert "warning_less_prominent" in _asserted(result)
    assert "warning_low_contrast" in _asserted(result)


@pytest.mark.tc("TC-06")
def test_prominence_rationale_describes_the_problem_not_the_verdict() -> None:
    signals = WarningTypography(header_is_bold=True, body_is_bold=False, relative_size=0.45)
    result = warning.evaluate(canon.CANONICAL_WARNING, signals)
    assert "smaller" in result.rationale


def test_a_bold_violation_outranks_a_prominence_concern() -> None:
    """One is a correction the applicant must make; the other is a second opinion."""
    signals = WarningTypography(header_is_bold=True, body_is_bold=True, relative_size=0.4)
    assert warning.evaluate(canon.CANONICAL_WARNING, signals).verdict is Verdict.MISMATCH


# --- the extractor that tidies up ------------------------------------------------------

def test_a_caps_signal_contradicting_the_text_is_not_resolved_in_our_favour() -> None:
    """The one failure mode text-only checking cannot see.

    An extractor that normalised `Government Warning:` into canonical form before
    returning it would hand us a perfect string. The signal is the only witness.
    """
    signals = WarningTypography(
        header_is_all_caps=False, header_is_bold=True, body_is_bold=False
    )
    result = warning.evaluate(canon.CANONICAL_WARNING, signals)
    assert result.verdict is Verdict.UNREADABLE
    assert any(f.code == "warning_header_caps_disputed" for f in result.findings)


def test_a_caps_signal_agreeing_with_the_text_adds_nothing() -> None:
    assert warning.check_header_caps(canon.CANONICAL_WARNING, GOOD) == []


def test_the_text_wins_when_it_shows_a_violation() -> None:
    """A signal claiming capitals cannot rescue a heading that visibly is not."""
    signals = WarningTypography(
        header_is_all_caps=True, header_is_bold=True, body_is_bold=False
    )
    result = warning.evaluate(_retitled("Government Warning:"), signals)
    assert "warning_header_not_all_caps" in _asserted(result)


# --- layout is not wording -------------------------------------------------------------

def test_word_wrap_hyphenation_is_layout_not_a_difference() -> None:
    """A narrow column hyphenates. The canonical statement contains no hyphen, so
    rejoining one that sits on a line break cannot hide a real difference."""
    wrapped = canon.CANONICAL_WARNING.replace("machinery", "machin-\nery")
    assert warning.is_verbatim(wrapped)


def test_soft_hyphens_and_zero_width_characters_are_layout() -> None:
    littered = canon.CANONICAL_WARNING.replace("pregnancy", "preg­nan​cy")
    assert warning.is_verbatim(littered)


def test_a_real_hyphen_inside_a_line_is_still_a_difference() -> None:
    """Only a hyphen at a line break is wrapping. One mid-line is a changed word."""
    altered = canon.CANONICAL_WARNING.replace("machinery", "machin-ery")
    assert not warning.is_verbatim(altered)


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


@pytest.mark.parametrize(
    ("ml", "expected"),
    [(200.0, "1 mm"), (237.0, "1 mm"), (750.0, "2 mm"), (3000.0, "2 mm"), (5000.0, "3 mm")],
)
def test_the_applicable_minimum_follows_the_container(ml: float, expected: str) -> None:
    """The number is only useful if it is the right number for this bottle."""
    assert expected in warning.type_size_context(ml)


def test_the_honesty_caveat_rides_on_a_match_too() -> None:
    """The row where it matters most. "Match" covered the wording and the type style,
    and it did not cover the millimetres — a clean row must still say so."""
    result = warning.evaluate(canon.CANONICAL_WARNING, GOOD, net_contents_ml=750.0)
    assert result.verdict is Verdict.MATCH
    caveat = next(f for f in result.findings if f.code == "warning_type_size_not_verified")
    assert caveat.severity == typography.SEVERITY_CONTEXT
    assert "2 mm" in caveat.message


@pytest.mark.parametrize("legible", [True, False])
def test_the_honesty_caveat_rides_on_every_outcome(legible: bool) -> None:
    for text in (None, canon.CANONICAL_WARNING, "GOVERNMENT WARNING: something else"):
        result = warning.evaluate(text, GOOD, legible=legible, net_contents_ml=750.0)
        assert any(f.code == "warning_type_size_not_verified" for f in result.findings)


def test_container_size_never_changes_a_verdict() -> None:
    """WARN-9 context is context. No bottle size makes a wrong warning right."""
    title_case = _retitled("Government Warning:")
    verdicts = {
        warning.evaluate(title_case, GOOD, net_contents_ml=ml).verdict
        for ml in (None, 50.0, 750.0, 5000.0)
    }
    assert verdicts == {Verdict.MISMATCH}


def test_the_caveat_is_never_mistaken_for_a_check_that_ran() -> None:
    caveat = warning.type_size_finding(750.0)
    assert caveat.severity not in typography.ASSERTED_SEVERITIES
    assert "not verifiable" in caveat.message
