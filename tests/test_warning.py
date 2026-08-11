"""Government warning statement. The highest-stakes module in the codebase.

A false Match here is the worst possible failure of this product, so the tests weight
toward proving the tool does NOT pass things it should catch.
"""

import pytest

from api import canon
from api.models import (
    Application,
    BoundingBox,
    Commodity,
    FieldName,
    FieldResult,
    Recommendation,
    Verdict,
    WarningTypography,
)
from api.rules import aggregate, typography, warning

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
    signals = WarningTypography(header_is_bold=True, body_is_bold=False, contrast_ok=True)
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


def test_a_cut_that_landed_mid_word_is_still_a_truncation() -> None:
    """A cut lands wherever the artwork ran out, not politely between words. This used
    to classify as a rewording, sending the applicant to look for words they never
    changed."""
    cut = canon.CANONICAL_WARNING[: canon.CANONICAL_WARNING.index("machinery") + 4]
    assert cut.endswith("mach")
    assert warning.classify(cut).kind == warning.TRUNCATED


def test_a_prefix_of_the_last_word_is_not_confused_with_a_different_word() -> None:
    """Loose matching applies to the final token only, and only as a prefix."""
    tokens = warning.tokenize(canon.CANONICAL_WARNING)
    swapped = " ".join([*tokens[:10], "zzz"])
    assert warning.classify(swapped).kind != warning.TRUNCATED


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


# --- LP-217 / TC-16: one application, several images ----------------------------------

def _sighting(
    index: int,
    text: str | None,
    signals: WarningTypography | None = None,
    **kw: object,
) -> warning.WarningSighting:
    return warning.WarningSighting(
        image_index=index,
        text=text,
        typography=signals if signals is not None else GOOD,
        **kw,  # type: ignore[arg-type]
    )


# --- the typography of one label, seen from several photographs -----------------------
#
# These are the regression tests for a confirmed false pass. Selecting one sighting used
# to discard the other images' typography signals, so a label whose bold-body violation
# was DETECTED on image 1 came back Match when image 0 happened to win the tie-break —
# and Mismatch when the two photographs were uploaded in the other order.

_BOLD_BODY = WarningTypography(
    header_is_all_caps=True, header_is_bold=True, body_is_bold=True,
    contrast_ok=True, relative_size=1.0,
)


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_a_violation_detected_on_any_image_is_a_violation(order: tuple[int, int]) -> None:
    """The extractor answered correctly and the pipeline threw the answer away."""
    good_at, bad_at = order
    result = warning.evaluate_across_images(
        [
            _sighting(good_at, canon.CANONICAL_WARNING, GOOD),
            _sighting(bad_at, canon.CANONICAL_WARNING, _BOLD_BODY),
        ]
    )
    assert result.verdict is Verdict.MISMATCH
    assert "warning_body_is_bold" in _asserted(result)


def test_the_verdict_does_not_depend_on_the_order_images_were_uploaded() -> None:
    """The property the false pass violated. Two photographs, one label, one answer."""
    for signals in (_BOLD_BODY, WarningTypography(header_is_bold=False),
                    WarningTypography(contrast_ok=False)):
        forward = warning.evaluate_across_images(
            [_sighting(0, canon.CANONICAL_WARNING, GOOD),
             _sighting(1, canon.CANONICAL_WARNING, signals)]
        ).verdict
        backward = warning.evaluate_across_images(
            [_sighting(0, canon.CANONICAL_WARNING, signals),
             _sighting(1, canon.CANONICAL_WARNING, GOOD)]
        ).verdict
        assert forward is backward


def test_an_abstention_on_one_image_does_not_erase_an_answer_on_another() -> None:
    merged = warning.merge_sighting_typography(
        [
            _sighting(0, canon.CANONICAL_WARNING, WarningTypography()),
            _sighting(1, canon.CANONICAL_WARNING, GOOD),
        ]
    )
    assert merged.header_is_bold is True
    assert merged.body_is_bold is False


def test_the_most_concerning_size_ratio_wins() -> None:
    merged = warning.merge_sighting_typography(
        [
            _sighting(0, canon.CANONICAL_WARNING,
                      WarningTypography(relative_size=1.0)),
            _sighting(1, canon.CANONICAL_WARNING,
                      WarningTypography(relative_size=0.4)),
        ]
    )
    assert merged.relative_size == 0.4


def test_signals_from_an_image_with_no_warning_on_it_are_ignored() -> None:
    """A front label carrying no warning has nothing to say about the warning's type."""
    merged = warning.merge_sighting_typography(
        [_sighting(0, None, _BOLD_BODY), _sighting(1, canon.CANONICAL_WARNING, GOOD)]
    )
    assert merged.body_is_bold is False


def test_a_reworded_warning_on_another_panel_is_never_a_clean_match() -> None:
    """The second confirmed path: front reworded, back correct, reported as approved."""
    reworded = canon.CANONICAL_WARNING.replace("birth defects", "health risks")
    result = warning.evaluate_across_images(
        [_sighting(0, reworded, GOOD), _sighting(1, canon.CANONICAL_WARNING, GOOD)]
    )
    assert result.verdict is not Verdict.MATCH
    note = next(f for f in result.findings if f.code == "warning_differs_between_images")
    assert note.severity == typography.SEVERITY_UNVERIFIED


@pytest.mark.tc("TC-16")
def test_a_warning_on_the_back_label_is_found() -> None:
    """The commonest layout there is. The front has no warning and that is not a defect."""
    result = warning.evaluate_across_images(
        [_sighting(0, None), _sighting(1, canon.CANONICAL_WARNING)]
    )
    assert result.verdict is Verdict.MATCH


@pytest.mark.tc("TC-16")
def test_missing_is_only_declared_when_every_image_came_back_empty() -> None:
    for sightings in (
        [_sighting(0, None)],
        [_sighting(0, None), _sighting(1, None), _sighting(2, "  ")],
    ):
        assert warning.evaluate_across_images(sightings).verdict is Verdict.MISSING


def test_the_most_complete_reading_wins_over_a_fragment() -> None:
    """A decorative fragment on the front must not be what the application is judged on."""
    fragment = "GOVERNMENT WARNING: (1) According to the Surgeon General"
    chosen = warning.select_sighting(
        [_sighting(0, fragment, confidence=0.99), _sighting(1, canon.CANONICAL_WARNING,
                                                            confidence=0.70)]
    )
    assert chosen is not None
    assert chosen.image_index == 1


def test_a_legible_reading_beats_an_illegible_one() -> None:
    """TC-12's glare on one image does not make the warning unreadable on the other."""
    chosen = warning.select_sighting(
        [
            _sighting(0, canon.CANONICAL_WARNING, legible=False, confidence=0.9),
            _sighting(1, canon.CANONICAL_WARNING, legible=True, confidence=0.5),
        ]
    )
    assert chosen is not None and chosen.image_index == 1


def test_an_illegible_image_is_not_an_image_with_no_warning() -> None:
    """Glare over the warning is Unreadable, never Missing. Confusing the two is the
    false pass this product exists to avoid."""
    result = warning.evaluate_across_images(
        [_sighting(0, None, legible=False), _sighting(1, None)]
    )
    assert result.verdict is Verdict.UNREADABLE


def test_completeness_counts_words_of_the_required_statement() -> None:
    assert warning.completeness(canon.CANONICAL_WARNING) == len(
        warning.tokenize(canon.CANONICAL_WARNING)
    )
    assert warning.completeness(None) == 0
    assert warning.completeness("Bottled in Bardstown, Kentucky") == 0


def test_completeness_scores_a_fragment_below_the_whole_statement() -> None:
    fragment = "GOVERNMENT WARNING: (1) According to the Surgeon General"
    assert 0 < warning.completeness(fragment) < warning.completeness(
        canon.CANONICAL_WARNING
    )


def test_two_images_with_different_warnings_are_flagged_not_silently_resolved() -> None:
    """Picking the most complete reading is right, and on its own it hides a defective
    warning printed on another panel. This test previously asserted Match, which was
    the bug written down as an expectation."""
    result = warning.evaluate_across_images(
        [
            _sighting(0, _retitled("Government Warning:")),
            _sighting(1, canon.CANONICAL_WARNING),
        ]
    )
    assert result.verdict is Verdict.UNREADABLE
    note = next(f for f in result.findings if f.code == "warning_differs_between_images")
    assert note.severity == typography.SEVERITY_UNVERIFIED


def test_the_same_warning_on_both_images_raises_no_note() -> None:
    result = warning.evaluate_across_images(
        [_sighting(0, canon.CANONICAL_WARNING), _sighting(1, canon.CANONICAL_WARNING)]
    )
    assert not any(f.code == "warning_differs_between_images" for f in result.findings)


def test_line_wrapping_alone_does_not_count_as_a_different_warning() -> None:
    """Two photographs of the same back label, read with different line breaks."""
    wrapped = canon.CANONICAL_WARNING.replace(" ", "\n", 4)
    result = warning.evaluate_across_images(
        [_sighting(0, canon.CANONICAL_WARNING), _sighting(1, wrapped)]
    )
    assert not any(f.code == "warning_differs_between_images" for f in result.findings)


def test_no_images_at_all_is_missing_not_a_crash() -> None:
    assert warning.evaluate_across_images([]).verdict is Verdict.MISSING


def test_selection_is_deterministic_when_two_readings_tie() -> None:
    """Two identical readings must not make the answer depend on dict ordering."""
    sightings = [
        _sighting(1, canon.CANONICAL_WARNING, confidence=0.9),
        _sighting(0, canon.CANONICAL_WARNING, confidence=0.9),
    ]
    first = warning.select_sighting(sightings)
    second = warning.select_sighting(list(reversed(sightings)))
    assert first is not None and second is not None
    assert first.image_index == second.image_index == 0


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


def test_the_prominence_message_claims_no_check_the_module_disclaims() -> None:
    """`LIMITS` declares 16.21's separate-and-apart rule unchecked, and the message used
    to assert it. A checker must not claim in prose what it disclaims in its manifest."""
    signals = WarningTypography(header_is_bold=True, body_is_bold=False, relative_size=0.4,
                                contrast_ok=True)
    finding = next(
        f for f in warning.evaluate(canon.CANONICAL_WARNING, signals).findings
        if f.code == "warning_less_prominent"
    )
    assert "set apart" not in finding.message
    assert "separate" not in finding.message


@pytest.mark.parametrize("ratio", [-1.0, 0.0, 1000.0])
def test_an_impossible_size_ratio_raises_no_finding(ratio: float) -> None:
    """A warning cannot be a negative size. Reporting "printed about 200% smaller" from
    a -1.0 would be inventing a finding out of a broken reading."""
    signals = WarningTypography(header_is_bold=True, body_is_bold=False, contrast_ok=True,
                                relative_size=ratio)
    codes = {f.code for f in warning.evaluate(canon.CANONICAL_WARNING, signals).findings}
    assert "warning_less_prominent" not in codes


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


# --- LP-215: the zero-false-pass gate --------------------------------------------------
#
# The release gate (OPS-3) is enforced in `eval/run.py` against the golden set, which is
# eight warning labels. Eight labels is a sample, and the claim being made is universal:
# *no* defective warning is ever reported as a Match. So the gate is also enforced here,
# where the corpus can be exhaustive rather than illustrative.
#
# Everything below asserts one property, from several directions:
#
#     If the warning is not exactly right, the verdict is not Match.
#
# There is no tolerance, no confidence floor and no threshold anywhere in the path. A
# test in this section going red means the product's central claim is false.

_TOKENS = warning.tokenize(canon.CANONICAL_WARNING)

#: Named defects, each one a thing an applicant has actually tried.
VIOLATION_CORPUS: list[tuple[str, str | None]] = [
    ("title-case heading", _retitled("Government Warning:")),
    ("sentence-case heading", _retitled("Government warning:")),
    ("lower-case heading", _retitled("government warning:")),
    ("alternating-case heading", _retitled("GoVeRnMeNt WaRnInG:")),
    ("heading with a comma", canon.CANONICAL_WARNING.replace("WARNING:", "WARNING,", 1)),
    ("heading with no punctuation", canon.CANONICAL_WARNING.replace("WARNING:", "WARNING", 1)),
    ("no heading at all", canon.WARNING_BODY),
    ("clause (1) only", canon.CANONICAL_WARNING[: canon.CANONICAL_WARNING.index("(2)")].strip()),
    ("clause (2) dropped mid-sentence",
     canon.CANONICAL_WARNING.replace(" and may cause health problems", "")),
    ("numbering removed", canon.CANONICAL_WARNING.replace("(1) ", "").replace("(2) ", "")),
    ("paraphrased clause (1)", canon.CANONICAL_WARNING.replace(
        "women should not drink alcoholic beverages during pregnancy",
        "pregnant women should not drink alcoholic beverages")),
    ("softened wording", canon.CANONICAL_WARNING.replace("should not", "may wish not to")),
    ("birth defects softened", canon.CANONICAL_WARNING.replace("birth defects", "health risks")),
    ("machinery clause dropped", canon.CANONICAL_WARNING.replace(
        "or operate machinery, ", "")),
    ("marketing line appended", canon.CANONICAL_WARNING + " Please drink responsibly."),
    ("brand name inserted", canon.CANONICAL_WARNING.replace(
        "GOVERNMENT WARNING:", "OLD TOM GOVERNMENT WARNING:", 1)),
    ("semicolon for colon", canon.CANONICAL_WARNING.replace(":", ";", 1)),
    ("Surgeon General lower-cased", canon.CANONICAL_WARNING.replace(
        "Surgeon General", "surgeon general")),
    ("words reordered", " ".join(reversed(_TOKENS))),
    ("absent", None),
    ("blank", "   "),
    ("unrelated text", "Bottled and distilled in Bardstown, Kentucky."),
]


@pytest.mark.parametrize(
    ("name", "text"), VIOLATION_CORPUS, ids=[n for n, _ in VIOLATION_CORPUS]
)
def test_no_defective_warning_is_ever_a_match(name: str, text: str | None) -> None:
    """The release gate, one label at a time, with typography that is beyond reproach."""
    assert warning.evaluate(text, GOOD).verdict is not Verdict.MATCH


@pytest.mark.parametrize(
    ("name", "text"), VIOLATION_CORPUS, ids=[n for n, _ in VIOLATION_CORPUS]
)
def test_no_defective_warning_survives_an_uncertain_reading(
    name: str, text: str | None
) -> None:
    """The same corpus with every typography signal abstaining — the realistic case."""
    assert warning.evaluate(text, WarningTypography()).verdict is not Verdict.MATCH


@pytest.mark.parametrize("cut", range(1, len(_TOKENS)))
def test_no_truncation_at_any_word_boundary_is_a_match(cut: int) -> None:
    """Exhaustive over every place the statement could stop early."""
    partial = " ".join(_TOKENS[:cut])
    assert warning.evaluate(partial, GOOD).verdict is not Verdict.MATCH


@pytest.mark.parametrize("index", range(len(_TOKENS)))
def test_dropping_any_single_word_is_never_a_match(index: int) -> None:
    """Exhaustive over every word in the statement."""
    mutilated = " ".join(_TOKENS[:index] + _TOKENS[index + 1 :])
    assert warning.evaluate(mutilated, GOOD).verdict is not Verdict.MATCH


@pytest.mark.parametrize("index", range(len(_TOKENS)))
def test_altering_any_single_word_is_never_a_match(index: int) -> None:
    swapped = [*_TOKENS]
    swapped[index] = "SOMETHINGELSE"
    assert warning.evaluate(" ".join(swapped), GOOD).verdict is not Verdict.MATCH


@pytest.mark.parametrize(
    "signals",
    [
        WarningTypography(header_is_bold=h, body_is_bold=b, contrast_ok=c, relative_size=r)
        for h in (True, False, None)
        for b in (True, False, None)
        for c in (True, False, None)
        for r in (None, 0.3, 0.8, 1.0)
    ],
)
def test_only_one_typography_combination_can_reach_match(
    signals: WarningTypography,
) -> None:
    """Exhaustive over the whole signal space, against a verbatim statement.

    Match requires the heading bold, the body not bold, and no prominence problem that
    was actually detected. Everything else — including every abstention — lands
    somewhere an agent has to look.
    """
    verdict = warning.evaluate(canon.CANONICAL_WARNING, signals).verdict
    size_ok = (
        signals.relative_size is None
        or signals.relative_size > typography.PROMINENCE_CONCERN_RATIO
    )
    can_match = (
        signals.header_is_bold is True
        and signals.body_is_bold is False
        and signals.contrast_ok is True
        and size_ok
    )
    assert (verdict is Verdict.MATCH) is can_match


def test_an_unconfirmed_contrast_never_reaches_a_pass() -> None:
    """Confirmed false pass, now a named regression.

    A verbatim statement whose contrast the reading could not judge used to come back
    Match with two context notes beside it, and the aggregate said "Every required field
    on the label matches the application." That scenario IS the evasion the PRD
    describes — the warning screened back into busy artwork.
    """
    signals = WarningTypography(
        header_is_bold=True, body_is_bold=False, contrast_ok=None, relative_size=None
    )
    result = warning.evaluate(canon.CANONICAL_WARNING, signals)
    assert result.verdict is Verdict.UNREADABLE
    assert "warning_contrast_unverified" in {f.code for f in result.findings}
    assert _asserted(result) == []


def test_an_unconfirmed_contrast_does_not_reach_ready_to_approve() -> None:
    """The same case, traced to the sentence an agent actually reads."""
    signals = WarningTypography(
        header_is_bold=True, body_is_bold=False, contrast_ok=None, relative_size=None
    )
    rows = [
        _row(FieldName.BRAND_NAME, Verdict.MATCH),
        _row(FieldName.CLASS_TYPE, Verdict.MATCH),
        _row(FieldName.GOVERNMENT_WARNING,
             warning.evaluate(canon.CANONICAL_WARNING, signals).verdict),
    ]
    advice = aggregate.recommend(rows)
    assert advice.recommendation is Recommendation.NEEDS_REVIEW
    assert advice.driving_field is FieldName.GOVERNMENT_WARNING


def test_only_size_may_go_unassessed_without_cost() -> None:
    """The one signal whose abstention is free, and the reason it is free: 16.22(b)'s
    rule is in millimetres and WARN-9 concedes we cannot measure those."""
    answered = WarningTypography(
        header_is_bold=True, body_is_bold=False, contrast_ok=True, relative_size=1.0
    )
    assert warning.evaluate(canon.CANONICAL_WARNING, answered).verdict is Verdict.MATCH
    for signal in ("header_is_bold", "body_is_bold", "contrast_ok"):
        blanked = answered.model_copy(update={signal: None})
        assert warning.evaluate(canon.CANONICAL_WARNING, blanked).verdict is not (
            Verdict.MATCH
        ), signal
    unsized = answered.model_copy(update={"relative_size": None})
    assert warning.evaluate(canon.CANONICAL_WARNING, unsized).verdict is Verdict.MATCH


def test_match_is_the_only_verdict_that_reads_as_a_pass() -> None:
    """The eval's gate treats Match and Not applicable as passes. This module can never
    return Not applicable — the warning is required on every alcohol label — so Match is
    the entire pass surface, and every test above is aimed at it."""
    reachable = {
        warning.evaluate(text, signals).verdict
        for text in (None, "", canon.CANONICAL_WARNING, _retitled("Government Warning:"))
        for signals in (GOOD, WarningTypography(), WarningTypography(body_is_bold=True))
    }
    assert Verdict.NOT_APPLICABLE not in reachable
    assert Verdict.ACCEPTABLE_VARIATION not in reachable


def test_an_illegible_reading_is_never_a_match_whatever_the_text_says() -> None:
    for text in (None, canon.CANONICAL_WARNING, _retitled("Government Warning:")):
        assert warning.evaluate(text, GOOD, legible=False).verdict is Verdict.UNREADABLE


def test_no_container_size_and_no_signal_combination_rescues_a_bad_warning() -> None:
    """The last door a false pass could come through: some other input relaxing the
    text check. Nothing in the signature is allowed to do that."""
    bad = _retitled("Government Warning:")
    for ml in (None, 50.0, 750.0, 5000.0):
        for signals in (GOOD, WarningTypography(), WarningTypography(relative_size=2.0)):
            assert warning.evaluate(bad, signals, net_contents_ml=ml).verdict is not (
                Verdict.MATCH
            )


def _import_closure(*modules: str) -> set[str]:
    """Every `api.*` module reachable from these, following imports transitively.

    Transitively is the point. A direct-import check passes the moment somebody adds
    `from api.rules import compare` to this path and `compare` reaches a threshold two
    hops away — which is exactly how a knob gets into the warning path without anybody
    deciding to put one there.
    """
    import ast
    from pathlib import Path

    package = Path(warning.__file__).parents[1]
    seen: set[str] = set()
    queue = list(modules)

    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        path = package.parent / (name.replace(".", "/") + ".py")
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                queue += [a.name for a in node.names if a.name.startswith("api")]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("api"):
                base = node.module or ""
                queue.append(base)
                queue += [f"{base}.{a.name}" for a in node.names]

    return seen


def test_the_import_closure_actually_follows_imports() -> None:
    """The guard below is worthless if the walker returns nothing. Prove it walks."""
    reached = _import_closure("api.rules.warning")
    assert "api.canon" in reached
    assert "api.models" in reached
    assert "api.rules.typography" in reached


def test_the_warning_path_consults_no_threshold() -> None:
    """WARN-6. If a knob ever appears in this path, at any depth, this test names it.

    Structural rather than behavioural on purpose: a behavioural test can only cover the
    thresholds that exist today. Note that nothing in the repo imports `thresholds` yet,
    so this currently guards a property that holds for the whole codebase — it is here to
    keep holding for this path specifically, whatever the rest of the engine does later.
    """
    reached = _import_closure("api.rules.warning", "api.rules.typography")
    offenders = {name for name in reached if "thresholds" in name}
    assert offenders == set(), f"the warning path reaches {sorted(offenders)}"


# --- casefolding trap -----------------------------------------------------------------

def test_warning_comparison_is_case_sensitive() -> None:
    """Reusing the brand-name normalizer here would erase Jenny's catch entirely."""
    assert not warning.is_verbatim(canon.CANONICAL_WARNING.lower())


def test_punctuation_differences_are_not_folded_away() -> None:
    assert not warning.is_verbatim(canon.CANONICAL_WARNING.replace(":", ";"))


# --- LP-218: the manifest cannot fall behind the code ---------------------------------
#
# Documentation about checks goes stale the first time someone adds a check. These tests
# make that impossible: the manifest and the finding codes the source actually emits are
# asserted to be the same set, by reading the source.


def _emitted_codes() -> set[str]:
    """Every `code="..."` literal in the two modules that produce warning findings."""
    import re as _re
    from pathlib import Path

    root = Path(warning.__file__).parent
    source = (root / "warning.py").read_text() + (root / "typography.py").read_text()
    return set(_re.findall(r'code="([a-z_]+)"', source))


def test_the_manifest_lists_every_finding_the_code_can_raise() -> None:
    missing = _emitted_codes() - warning.FINDING_CODES
    assert missing == set(), f"findings raised but not documented: {sorted(missing)}"


def test_the_manifest_documents_nothing_the_code_cannot_raise() -> None:
    stale = warning.FINDING_CODES - _emitted_codes()
    assert stale == set(), f"documented but never raised: {sorted(stale)}"


def test_every_check_says_what_it_checks_and_what_follows() -> None:
    for check in warning.CHECK_MANIFEST:
        assert check.checks.strip()
        assert check.evidence.strip()
        assert check.outcome.strip()
        assert check.citation.startswith("27 CFR 16.")


def test_manifest_codes_are_unique() -> None:
    codes = [c.code for c in warning.CHECK_MANIFEST]
    assert len(codes) == len(set(codes))


def test_context_only_findings_are_marked_as_such_in_the_manifest() -> None:
    """A row that says "context only" must not be one that changes a verdict."""
    context_codes = {
        c.code for c in warning.CHECK_MANIFEST if c.outcome.startswith("context only")
    }
    assert context_codes == {
        "warning_prominence_unassessed",
        "warning_type_size_not_verified",
        "warning_differs_between_images",
    }


def test_the_limits_are_stated_rather_than_left_out() -> None:
    """WARN-9 as a list. A requirement quietly missing from a checker reads as a
    requirement that was met."""
    requirements = " ".join(limit.requirement for limit in warning.LIMITS).lower()
    assert "millimetres" in requirements
    assert "separate and apart" in requirements
    assert "compressed" in requirements
    for limit in warning.LIMITS:
        assert len(limit.why_not) > 60, f"{limit.requirement}: no reason given"


def test_the_type_size_limit_is_cited_to_both_its_subsections() -> None:
    """16.22(b) maps container volume to millimetres in prose; 16.22(a)(4) carries the
    millimetres-to-characters-per-inch table. An earlier pass cited (b) for both."""
    limit = next(x for x in warning.LIMITS if "millimetres" in x.requirement)
    assert limit.citation == "27 CFR 16.22(a)(4), (b)"


def test_separate_and_apart_is_cited_to_16_21_not_16_22() -> None:
    """It sits with the statement, not with the type-style rules — see LP-328."""
    limit = next(x for x in warning.LIMITS if "separate and apart" in x.requirement)
    assert limit.citation == "27 CFR 16.21"


# --- LP-204: what the diff view is handed ---------------------------------------------
#
# WARN-8 says the word-level diff is the evidence — the thing an agent shows a supervisor
# and an applicant. `web/src/components/DiffView.tsx` renders it from two fields on the
# wire, `expected` and `extracted`, and it can only be as good as those. These tests
# assert the contract from the server side, because a diff rendered against a placeholder
# is worse than no diff: it looks like evidence and is noise.


def _warning_row(fixture: str) -> FieldResult:
    from api.provider.base import ImageInput
    from api.provider.fake import SpecBackedProvider
    from api.verify import verify as run_verification
    from fixtures.generator.catalog import by_name

    spec = by_name(fixture)
    producer_name, _, producer_address = spec.producer.partition(", ")
    application = Application(
        commodity=Commodity(spec.commodity),
        brand_name=spec.brand_name,
        class_type=spec.class_type,
        alcohol_content=45.0,
        net_contents=spec.net_contents,
        producer_name=producer_name,
        producer_address=producer_address,
        country_of_origin=spec.country_of_origin,
        is_import=False,
    )
    result = run_verification(
        application, [ImageInput(index=0, data=b"", role="single")], SpecBackedProvider(spec)
    )
    return next(f for f in result.fields if f.field is FieldName.GOVERNMENT_WARNING)


@pytest.mark.tc("TC-05")
def test_the_diff_view_is_handed_the_regulation_not_a_description_of_it() -> None:
    row = _warning_row("tc05_reworded_warning")
    assert row.expected == canon.CANONICAL_WARNING
    assert row.extracted is not None and row.extracted != canon.CANONICAL_WARNING


@pytest.mark.tc("TC-05")
def test_the_two_sides_of_the_diff_differ_only_where_the_label_does() -> None:
    """Whatever diff algorithm renders it, this is the pair it renders — and the words
    that differ have to be the reworded ones, not an artefact of the comparison."""
    row = _warning_row("tc05_reworded_warning")
    assert row.extracted is not None
    changed = {
        word
        for seg in warning.tokenized_diff(row.extracted)
        if seg.is_difference
        for word in (*seg.expected, *seg.found)
    }
    assert "pregnant" in changed
    assert "Consumption" not in changed  # the untouched clause stays untouched


def test_both_sides_are_long_enough_to_open_the_block_diff() -> None:
    """FieldRow switches to the full side-by-side past 90 characters. A warning row
    that fell under that would show fifty words of legalese inside a table cell."""
    row = _warning_row("tc03_title_case_warning")
    assert row.expected is not None and len(row.expected) > 90
    assert row.extracted is not None and len(row.extracted) > 90


def test_a_missing_warning_still_shows_the_agent_what_was_required() -> None:
    """Nothing to diff against, and the required wording is still the useful thing to
    put on screen — it is what the applicant has to add."""
    row = _warning_row("tc07_missing_warning")
    assert row.verdict is Verdict.MISSING
    assert row.expected == canon.CANONICAL_WARNING
    assert row.extracted is None


def test_the_outlined_region_belongs_to_the_photograph_it_names() -> None:
    """The box used to come from the highest-confidence reading and the image index from
    the most complete one, so a two-image application drew image 0's rectangle over
    image 1's photograph — on the row the PRD most wants outlined."""
    from api.models import ExtractedField, Extraction
    from api.verify import warning_sightings

    front_box = BoundingBox(x0=0.0, y0=0.0, x1=0.2, y1=0.2)
    back_box = BoundingBox(x0=0.1, y0=0.7, x1=0.9, y1=0.95)
    fragment = "GOVERNMENT WARNING: (1) According to the Surgeon General"
    extractions = [
        Extraction(
            image_index=0,
            fields={FieldName.GOVERNMENT_WARNING: ExtractedField(
                value=fragment, confidence=0.99, bbox=front_box)},
            warning_text=fragment,
        ),
        Extraction(
            image_index=1,
            fields={FieldName.GOVERNMENT_WARNING: ExtractedField(
                value=canon.CANONICAL_WARNING, confidence=0.70, bbox=back_box)},
            warning_text=canon.CANONICAL_WARNING,
        ),
    ]
    chosen = warning.select_sighting(warning_sightings(extractions))
    assert chosen is not None
    assert chosen.image_index == 1          # the complete reading wins
    assert chosen.bbox == back_box          # and it brings its own rectangle


def test_the_warning_row_points_at_a_region_on_the_picture() -> None:
    """LP-212 asks for the region, and the row an agent most needs outlined was the one
    field arriving with no evidence at all."""
    row = _warning_row("tc06_buried_warning")
    assert row.evidence is not None
    assert row.evidence.bbox is not None


# --- escalation, wired ----------------------------------------------------------------
#
# The hook used to exist only in this file's sibling. It is now a parameter on
# `evaluate_across_images`, defaulting to None, so a provider adapter can engage it
# without the pipeline changing shape — and so it can be proved to engage at all.


class _Rereader:
    """A stub stronger model. Records what it was asked, returns what it was told to."""

    name = "stub"

    def __init__(self, reply: typography.WarningReread) -> None:
        self.reply = reply
        self.requests: list[typography.WarningRereadRequest] = []

    def reread_warning(
        self, request: typography.WarningRereadRequest
    ) -> typography.WarningReread:
        self.requests.append(request)
        return self.reply


def test_escalation_engages_on_a_warning_about_to_pass() -> None:
    """The whole point of finding #3: the net has to be under the pass."""
    stub = _Rereader(typography.WarningReread(typography=GOOD))
    warning.evaluate_across_images(
        [_sighting(0, canon.CANONICAL_WARNING, GOOD)], rereader=stub
    )
    assert len(stub.requests) == 1
    assert "compliant" in stub.requests[0].reason


def test_a_stronger_model_can_overturn_a_pass() -> None:
    """A confident wrong answer from the fast model is the measured failure. This is
    the path that catches it."""
    stub = _Rereader(
        typography.WarningReread(typography=WarningTypography(body_is_bold=True))
    )
    result = warning.evaluate_across_images(
        [_sighting(0, canon.CANONICAL_WARNING, GOOD)], rereader=stub
    )
    assert result.verdict is not Verdict.MATCH
    assert "warning_typography_disputed" in {f.code for f in result.findings}


def test_escalation_carries_the_region_so_the_crop_is_not_guessed() -> None:
    stub = _Rereader(typography.WarningReread(typography=GOOD))
    box = BoundingBox(x0=0.1, y0=0.7, x1=0.9, y1=0.95)
    warning.evaluate_across_images(
        [warning.WarningSighting(
            image_index=2, text=canon.CANONICAL_WARNING, typography=GOOD, bbox=box
        )],
        rereader=stub,
    )
    assert stub.requests[0].image_index == 2
    assert stub.requests[0].bbox == box


def test_escalation_recovers_a_warning_the_first_pass_could_not_read() -> None:
    """Unreadable is the only verdict a second look can improve, and it can only improve
    it by supplying words the first pass never had."""
    stub = _Rereader(
        typography.WarningReread(warning_text=canon.CANONICAL_WARNING, typography=GOOD)
    )
    result = warning.evaluate_across_images(
        [_sighting(0, None, GOOD, legible=False)], rereader=stub
    )
    assert result.verdict is Verdict.MATCH


def test_no_rereader_leaves_the_first_pass_exactly_as_it_was() -> None:
    with_hook = warning.evaluate_across_images(
        [_sighting(0, canon.CANONICAL_WARNING, GOOD)], rereader=None
    )
    assert with_hook.verdict is Verdict.MATCH


def test_a_rereader_that_blows_up_does_not_take_the_verification_with_it() -> None:
    """NET-3. The first pass already fails closed, so losing the second look costs
    certainty, never safety."""

    class Broken:
        name = "broken"

        def reread_warning(
            self, request: typography.WarningRereadRequest
        ) -> typography.WarningReread:
            raise RuntimeError("provider unreachable")

    result = warning.evaluate_across_images(
        [_sighting(0, canon.CANONICAL_WARNING, GOOD)], rereader=Broken()
    )
    assert result.verdict is Verdict.MATCH


def test_escalation_does_not_fire_when_the_first_pass_already_found_a_violation() -> None:
    stub = _Rereader(typography.WarningReread(typography=GOOD))
    warning.evaluate_across_images(
        [_sighting(0, canon.CANONICAL_WARNING, _BOLD_BODY)], rereader=stub
    )
    assert stub.requests == []


# --- LP-216: the warning fixture set, exercised end to end -----------------------------
#
# Fixtures that exist but are never run against the rules engine prove nothing. Each of
# these takes a rendered label through the whole pipeline and asserts the verdict and the
# finding the golden set claims for it.


@pytest.mark.parametrize(
    ("fixture", "verdict", "code"),
    [
        ("tc01_old_tom_clean", Verdict.MATCH, None),
        ("tc03_title_case_warning", Verdict.MISMATCH, "warning_header_not_all_caps"),
        ("tc03b_non_bold_warning_header", Verdict.MISMATCH, "warning_header_not_bold"),
        ("tc04_bold_warning_body", Verdict.MISMATCH, "warning_body_is_bold"),
        ("tc05_reworded_warning", Verdict.MISMATCH, "warning_text_rewording"),
        ("tc05b_truncated_warning", Verdict.MISMATCH, "warning_text_truncated"),
        ("tc06_buried_warning", Verdict.UNREADABLE, "warning_less_prominent"),
        ("tc07_missing_warning", Verdict.MISSING, "warning_missing"),
    ],
)
def test_each_warning_fixture_produces_what_the_golden_set_claims(
    fixture: str, verdict: Verdict, code: str | None
) -> None:
    row = _warning_row(fixture)
    assert row.verdict is verdict
    if code is not None:
        assert code in {f.code for f in row.findings}


def test_the_bold_half_of_warn_2_has_a_fixture_of_its_own() -> None:
    """TC-03 covers capitals. Without this one, a checker that ignored the bold signal
    entirely would have passed the whole fixture set."""
    row = _warning_row("tc03b_non_bold_warning_header")
    assert "warning_header_not_all_caps" not in {f.code for f in row.findings}
    assert row.extracted == canon.CANONICAL_WARNING  # the wording is correct


def test_the_truncated_fixture_is_a_truncation_and_not_a_rewording() -> None:
    """LP-210's two cases are different corrections for the applicant to make."""
    row = _warning_row("tc05b_truncated_warning")
    assert row.extracted is not None
    assert warning.classify(row.extracted).kind == warning.TRUNCATED


@pytest.mark.tc("TC-06")
def test_the_buried_fixture_is_verbatim_and_still_needs_a_human() -> None:
    """The point of TC-06: nothing is wrong with the words, so this is not a correction
    to send back — it is a judgement about a photograph."""
    row = _warning_row("tc06_buried_warning")
    assert row.extracted == canon.CANONICAL_WARNING
    assert row.verdict is Verdict.UNREADABLE
    assert row.evidence is not None and row.evidence.bbox is not None


# --- LP-214: the warning is the row an agent sees first --------------------------------
#
# MATCH-10 and WARN-6. The ranking rule is implemented in aggregate.py and mirrored in
# web/src/triage.ts; these assert the property from the warning's side, because the
# verdicts this module now produces are what feed it — and one of them, Unreadable on a
# verbatim warning with unconfirmed type styling, did not exist before this wave.


def _row(field: FieldName, verdict: Verdict) -> FieldResult:
    return FieldResult(
        field=field, verdict=verdict, extracted=None, expected=None,
        confidence=1.0, rationale="",
    )


@pytest.mark.parametrize(
    "warning_verdict",
    [Verdict.MISSING, Verdict.MISMATCH, Verdict.UNREADABLE, Verdict.MATCH],
)
def test_the_warning_row_is_always_first(warning_verdict: Verdict) -> None:
    rows = [
        _row(FieldName.BRAND_NAME, Verdict.MISSING),
        _row(FieldName.NET_CONTENTS, Verdict.MISMATCH),
        _row(FieldName.GOVERNMENT_WARNING, warning_verdict),
    ]
    assert aggregate.triage_order(rows)[0].field is FieldName.GOVERNMENT_WARNING


@pytest.mark.tc("TC-07")
def test_a_missing_warning_drives_the_recommendation_on_its_own() -> None:
    """WARN-6. Every other row matching does not dilute it."""
    rows = [
        _row(FieldName.BRAND_NAME, Verdict.MATCH),
        _row(FieldName.NET_CONTENTS, Verdict.MATCH),
        _row(FieldName.GOVERNMENT_WARNING, Verdict.MISSING),
    ]
    advice = aggregate.recommend(rows)
    assert advice.recommendation is Recommendation.RETURN_FOR_CORRECTION
    assert advice.driving_field is FieldName.GOVERNMENT_WARNING


@pytest.mark.tc("TC-03")
def test_jennys_catch_reaches_the_top_of_the_screen() -> None:
    """The whole chain again, this time ending where an agent actually looks."""
    verdict = warning.evaluate(_retitled("Government Warning:"), GOOD).verdict
    rows = [
        _row(FieldName.BRAND_NAME, Verdict.ACCEPTABLE_VARIATION),
        _row(FieldName.GOVERNMENT_WARNING, verdict),
    ]
    advice = aggregate.recommend(rows)
    assert advice.driving_field is FieldName.GOVERNMENT_WARNING
    assert aggregate.attention_fields(rows)[0].field is FieldName.GOVERNMENT_WARNING


def test_an_unconfirmed_warning_outranks_an_acceptable_variation() -> None:
    """The new verdict this wave introduced has to sort above the soft ones, or the
    row an agent must look at sits under the row they need not."""
    unconfirmed = warning.evaluate(canon.CANONICAL_WARNING, WarningTypography()).verdict
    rows = [
        _row(FieldName.BRAND_NAME, Verdict.ACCEPTABLE_VARIATION),
        _row(FieldName.GOVERNMENT_WARNING, unconfirmed),
    ]
    attention = aggregate.attention_fields(rows)
    assert attention[0].field is FieldName.GOVERNMENT_WARNING
    assert aggregate.recommend(rows).recommendation is Recommendation.NEEDS_REVIEW


def test_a_clean_warning_does_not_hold_up_a_clean_label() -> None:
    """The other half of the ranking rule: pinning it first must not pin it open."""
    rows = [
        _row(FieldName.BRAND_NAME, Verdict.MATCH),
        _row(FieldName.GOVERNMENT_WARNING, warning.evaluate(canon.CANONICAL_WARNING, GOOD).verdict),
    ]
    assert aggregate.recommend(rows).recommendation is Recommendation.READY_TO_APPROVE
    assert aggregate.attention_fields(rows) == []


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
