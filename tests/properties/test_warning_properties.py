"""Properties of the government warning check — the one field that may never false-pass.

Jenny's specification: *"It has to be exact. Like, word-for-word, and the 'GOVERNMENT
WARNING:' part has to be in all caps and bold."* She rejected a label for title-case
`Government Warning`.

Examples can show that the six defects somebody thought of are caught. What the product
actually promises is stronger and negative: **no input reaches Match unless it is the
statement, verbatim, in compliant type.** That is an implication over every string, and
the properties below state it that way — mutate the canonical text in any way at all and
the verdict must move off Match.

The tests deliberately mutate rather than construct. A constructed "wrong warning" tests
the wrongness the author imagined; a mutated one tests the wrongness the printer will
actually produce.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from api import canon
from api.models import Verdict, WarningTypography
from api.rules import warning as warn

pytestmark = pytest.mark.property

SETTINGS = settings(max_examples=300, deadline=None)

CANONICAL = canon.CANONICAL_WARNING
WORDS = CANONICAL.split()

#: Typography signals that are unambiguously compliant: heading bold, body not bold,
#: everything determined. Any Match verdict in this file has to survive these being
#: perfect, which isolates the text check from the typography check.
COMPLIANT = WarningTypography(
    header_is_all_caps=True,
    header_is_bold=True,
    body_is_bold=False,
    relative_size=1.0,
    contrast_ok=True,
)

TRISTATE = st.sampled_from([True, False, None])


# --------------------------------------------------------------------------------------
# The negative property, stated over every string
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-05")
@settings(max_examples=600, deadline=None)
@given(st.text(max_size=200))
def test_match_implies_the_text_is_the_statement_verbatim(text: str) -> None:
    """For every string in the universe: Match implies verbatim.

    Layout whitespace is the only thing collapsed, because a line break is where the
    printer wrapped the paragraph and is not part of the regulation. Case and
    punctuation are the regulation.
    """
    result = warn.evaluate(text, COMPLIANT)
    if result.verdict is Verdict.MATCH:
        assert warn.collapse_layout_whitespace(text) == CANONICAL


@settings(max_examples=600, deadline=None)
@given(st.text(max_size=200), TRISTATE, TRISTATE, TRISTATE)
def test_match_implies_the_typography_was_confirmed_compliant(
    text: str, header_bold: bool | None, body_bold: bool | None, contrast: bool | None
) -> None:
    """Match also implies 16.22 was checked and passed — not merely not-failed.

    `None` means the extractor could not tell. An unknown must never become a pass:
    "we could not determine whether the heading is bold" and "the heading is bold" are
    different findings, and only one of them is a determination we made (WARN-6).
    """
    signals = WarningTypography(
        header_is_all_caps=True,
        header_is_bold=header_bold,
        body_is_bold=body_bold,
        relative_size=1.0,
        contrast_ok=contrast,
    )
    if warn.evaluate(text, signals).verdict is Verdict.MATCH:
        assert header_bold is True
        assert body_bold is False


# --------------------------------------------------------------------------------------
# Mutation properties — every way to get it wrong
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-05")
@SETTINGS
@given(st.integers(min_value=0, max_value=len(WORDS) - 1))
def test_omitting_any_word_breaks_the_match(index: int) -> None:
    """Drop any one of the statement's words and it is no longer the statement.

    27 CFR 16.21 prescribes the text. There is no word in it the tool may treat as
    optional, including the ones that look like filler.
    """
    mutated = " ".join(WORDS[:index] + WORDS[index + 1 :])
    assert warn.evaluate(mutated, COMPLIANT).verdict is not Verdict.MATCH


@pytest.mark.tc("TC-05")
@SETTINGS
@given(
    st.integers(min_value=0, max_value=len(WORDS) - 1),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=10),
)
def test_substituting_any_word_breaks_the_match(index: int, replacement: str) -> None:
    """TC-05 generalised: `pregnant women` for `women ... during pregnancy` is one of
    infinitely many rewordings, and none of them is the statement."""
    mutated = " ".join([*WORDS[:index], replacement, *WORDS[index + 1 :]])
    assume(warn.collapse_layout_whitespace(mutated) != CANONICAL)
    assert warn.evaluate(mutated, COMPLIANT).verdict is not Verdict.MATCH


@SETTINGS
@given(
    st.integers(min_value=0, max_value=len(WORDS)),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=10),
)
def test_inserting_any_word_breaks_the_match(index: int, addition: str) -> None:
    """Adding words is a violation too, and the less obvious half of the rule.

    A producer who appends their own safety advice to the required paragraph has not
    printed the required statement.
    """
    mutated = " ".join([*WORDS[:index], addition, *WORDS[index:]])
    assert warn.evaluate(mutated, COMPLIANT).verdict is not Verdict.MATCH


@pytest.mark.tc("TC-03")
@SETTINGS
@given(st.integers(min_value=0, max_value=len(CANONICAL) - 1))
def test_flipping_the_case_of_any_letter_breaks_the_match(index: int) -> None:
    """Jenny's catch, at every position in the statement.

    This is why the warning check does *not* use `normalize.normalize()`. That function
    casefolds, which is right for brand names and would erase exactly the violation she
    found.
    """
    character = CANONICAL[index]
    assume(character.lower() != character.upper())
    flipped = character.lower() if character.isupper() else character.upper()
    mutated = f"{CANONICAL[:index]}{flipped}{CANONICAL[index + 1 :]}"
    assert warn.evaluate(mutated, COMPLIANT).verdict is not Verdict.MATCH


@SETTINGS
@given(st.sampled_from(".,;:!?()"), st.integers(min_value=0, max_value=len(CANONICAL)))
def test_altering_punctuation_anywhere_breaks_the_match(mark: str, index: int) -> None:
    """Punctuation is the regulation too. `16.21` prints a colon after the heading."""
    mutated = f"{CANONICAL[:index]}{mark}{CANONICAL[index:]}"
    assume(warn.collapse_layout_whitespace(mutated) != CANONICAL)
    assert warn.evaluate(mutated, COMPLIANT).verdict is not Verdict.MATCH


# --------------------------------------------------------------------------------------
# What must NOT break the match
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-01")
@SETTINGS
@given(st.lists(st.sampled_from([" ", "\n", "\t", "  ", "\r\n", "\n  "]), min_size=1, max_size=8))
def test_reflowing_the_paragraph_never_breaks_the_match(separators: list[str]) -> None:
    """However the printer wrapped it, it is still the statement.

    Line breaks are label layout. A tool that reported every two-column back label as a
    warning mismatch would be reporting on typesetting, and the agent would learn to
    ignore the row.
    """
    parts = CANONICAL.split(" ")
    reflowed = parts[0]
    for i, part in enumerate(parts[1:]):
        reflowed += separators[i % len(separators)] + part
    assert warn.evaluate(reflowed, COMPLIANT).verdict is Verdict.MATCH


@SETTINGS
@given(st.text(alphabet=" \t\n", max_size=6), st.text(alphabet=" \t\n", max_size=6))
def test_surrounding_whitespace_never_breaks_the_match(before: str, after: str) -> None:
    assert warn.evaluate(f"{before}{CANONICAL}{after}", COMPLIANT).verdict is Verdict.MATCH


# --------------------------------------------------------------------------------------
# Failing closed on absence and illegibility
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-07")
@pytest.mark.parametrize("text", [None, "", "   ", "\n\t"])
def test_an_absent_warning_is_missing_with_a_critical_finding(text: str | None) -> None:
    """No statement on any image is the disqualifying case (TC-07)."""
    result = warn.evaluate(text, COMPLIANT)
    assert result.verdict is Verdict.MISSING
    assert [f.code for f in result.findings] == ["warning_missing"]
    assert result.findings[0].severity == "critical"


@pytest.mark.tc("TC-12")
@settings(max_examples=200, deadline=None)
@given(st.text(max_size=120))
def test_an_illegible_image_is_unreadable_whatever_the_text_says(text: str) -> None:
    """Illegibility is reported before absence, and before any reading of the text.

    "We could not read it" and "it is not there" are different findings, and reporting
    the second when the first is true is a false finding on a compliant label. Glare
    across the back of a bottle produces exactly this.
    """
    assert warn.evaluate(text, COMPLIANT, legible=False).verdict is Verdict.UNREADABLE


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=120))
def test_an_illegible_reading_never_reaches_a_pass(text: str) -> None:
    """The strongest form: no text at all can pass while the image is illegible."""
    assert warn.evaluate(text, COMPLIANT, legible=False).verdict is not Verdict.MATCH


# --------------------------------------------------------------------------------------
# Typography (27 CFR 16.22)
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-04")
@SETTINGS
@given(TRISTATE, TRISTATE, TRISTATE, TRISTATE)
def test_an_unknown_typography_signal_never_produces_silence(
    header_caps: bool | None,
    header_bold: bool | None,
    body_bold: bool | None,
    contrast: bool | None,
) -> None:
    """Every `None` produces a cannot-confirm finding rather than nothing.

    Silence reads downstream as a pass. The tri-state exists precisely so that "the
    extractor could not tell" survives all the way to the agent's screen as "check this
    by eye" (WARN-6, LP-053).
    """
    signals = WarningTypography(
        header_is_all_caps=header_caps,
        header_is_bold=header_bold,
        body_is_bold=body_bold,
        relative_size=None,
        contrast_ok=contrast,
    )
    codes = {f.code for f in warn.check_typography(signals)}
    if header_bold is None:
        assert "warning_header_bold_unverified" in codes
    if body_bold is None:
        assert "warning_body_bold_unverified" in codes


@pytest.mark.tc("TC-04")
def test_a_bold_body_is_a_hard_finding_not_an_unverified_one() -> None:
    """16.22's inverse rule: only the heading may be bold, and this label breaks it."""
    signals = WarningTypography(
        header_is_all_caps=True, header_is_bold=True, body_is_bold=True,
        relative_size=1.0, contrast_ok=True,
    )
    result = warn.evaluate(CANONICAL, signals)
    assert result.verdict is Verdict.MISMATCH
    assert [f.code for f in result.findings] == ["warning_body_is_bold"]


def test_verbatim_text_with_unconfirmed_typography_fails_closed() -> None:
    """Right words, unverifiable formatting: Acceptable variation, never Match.

    This is the fail-closed direction stated as a case. The wording being perfect is
    not enough — an agent still has to look at the type.
    """
    result = warn.evaluate(CANONICAL, WarningTypography())
    assert result.verdict is Verdict.ACCEPTABLE_VARIATION
    assert "could not be confirmed" in result.rationale


@pytest.mark.tc("TC-03")
@SETTINGS
@given(st.sampled_from(["Government Warning:", "government warning:", "GoVeRnMeNt WaRnInG:"]))
def test_a_non_capitalised_heading_is_found_and_then_judged(heading: str) -> None:
    """The heading is matched case-insensitively so it can be *judged*, not missed.

    Missing it entirely would report the label as a text mismatch — technically a
    return, but for the wrong reason, and the agent's note to the applicant would be
    wrong too.
    """
    text = heading + CANONICAL[len(canon.WARNING_HEADER) :]
    findings = warn.check_header_caps(text)
    assert [f.code for f in findings] == ["warning_header_not_all_caps"]
    assert heading.rstrip(": ") in findings[0].message


def test_a_statement_with_no_heading_at_all_is_reported_as_such() -> None:
    findings = warn.check_header_caps("According to the Surgeon General, women should not")
    assert [f.code for f in findings] == ["warning_header_missing"]


# --------------------------------------------------------------------------------------
# The diff an agent reads (WARN-8)
# --------------------------------------------------------------------------------------


@SETTINGS
@given(st.text(max_size=200))
def test_the_diff_always_reconstructs_the_required_statement(text: str) -> None:
    """Concatenating the expected side of every segment gives back the regulation.

    The diff is the evidence panel. If it dropped or reordered words, the agent would
    be checking the tool's paraphrase of 16.21 rather than 16.21.
    """
    segments = warn.tokenized_diff(text)
    rebuilt = [word for segment in segments for word in segment.expected]
    assert rebuilt == warn.tokenize(CANONICAL)


@SETTINGS
@given(st.text(max_size=200))
def test_the_diff_always_reconstructs_what_the_label_said(text: str) -> None:
    segments = warn.tokenized_diff(text)
    rebuilt = [word for segment in segments for word in segment.found]
    assert rebuilt == warn.tokenize(text)


@SETTINGS
@given(st.text(max_size=200))
def test_a_non_matching_statement_always_gets_a_plain_language_summary(text: str) -> None:
    """UX-6: the agent is told what differs, in words, not shown a raw diff structure."""
    result = warn.evaluate(text, COMPLIANT)
    assume(result.verdict is Verdict.MISMATCH)
    assert result.rationale.strip()
    assert not result.rationale.startswith("[")


def test_a_verbatim_statement_summarises_as_a_match() -> None:
    """The no-difference branch of the summary, which mutation tests never reach."""
    assert warn.diff_summary(warn.tokenized_diff(CANONICAL)) == (
        "The warning statement matches the required text word for word."
    )


def test_added_words_are_described_as_added() -> None:
    summary = warn.diff_summary(warn.tokenized_diff(f"{CANONICAL} Drink responsibly."))
    assert "adds the words" in summary


def test_omitted_words_are_described_as_missing() -> None:
    summary = warn.diff_summary(warn.tokenized_diff(" ".join(WORDS[:-3])))
    assert "missing the words" in summary


# --------------------------------------------------------------------------------------
# Type size (WARN-9) — context, never a claim
# --------------------------------------------------------------------------------------


@SETTINGS
@given(st.floats(min_value=1.0, max_value=20000.0, allow_nan=False))
def test_type_size_context_never_claims_the_tool_verified_it(volume: float) -> None:
    """Absolute type size is not measurable from an unscaled photograph.

    The sentence gives the agent the applicable minimum and then says plainly that the
    tool did not check it. Claiming otherwise would be the tool asserting a
    determination it cannot make.
    """
    text = warn.type_size_context(volume)
    assert "not verifiable" in text
    assert "not a check the tool performed" in text


def test_type_size_context_admits_when_the_container_size_is_unknown() -> None:
    text = warn.type_size_context(None)
    assert "could not be determined" in text


@SETTINGS
@given(st.floats(min_value=1.0, max_value=20000.0, allow_nan=False))
def test_every_container_size_falls_in_a_band(volume: float) -> None:
    """27 CFR 16.22's bands are exhaustive; no bottle has no applicable minimum."""
    minimum, cpi = canon.warning_type_size_for(volume)
    assert minimum > 0
    assert cpi > 0


@pytest.mark.parametrize(
    ("volume", "expected_mm"), [(237.0, 1.0), (238.0, 2.0), (3000.0, 2.0), (3001.0, 3.0)]
)
def test_the_type_size_bands_are_upper_exclusive_at_the_boundary(
    volume: float, expected_mm: float
) -> None:
    """"237 mL or less", then "more than 237 mL" — checked from both sides."""
    assert canon.warning_type_size_for(volume)[0] == expected_mm
