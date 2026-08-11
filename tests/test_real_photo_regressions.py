"""Regressions found by real photographs, not by fixtures (Tier B).

Every case here was a live defect discovered by running an actual phone photo of an
actual bottle through the pipeline. The synthetic fixtures could not have found any of
them, which is the argument for Tier B existing at all.
"""

from __future__ import annotations

from api import canon
from api.rules import warning


def test_a_statement_set_entirely_in_capitals_is_not_a_defect() -> None:
    """Found on a shipping Fireball bottle (Sazerac, 750mL).

    The label sets the whole warning in caps. Comparing case-sensitively called that
    `warning_text_casing` and drove the application to Return for correction — a
    correction notice for a compliant label, which is the failure that stops an agent
    trusting the tool. 27 CFR 16.22(a)(2) governs the case of the HEADING and says
    nothing about the statement that follows.
    """
    comparison = warning.classify(canon.CANONICAL_WARNING.upper())

    assert comparison.is_verbatim
    assert warning.text_findings(comparison) == []


def test_a_selectively_recased_word_is_still_a_defect() -> None:
    """The forgiveness above is narrow on purpose.

    An all-caps setting is a printing convention. One word altered is not, and must not
    ride along on the same exemption.
    """
    altered = canon.CANONICAL_WARNING.replace("Surgeon General", "surgeon general")
    comparison = warning.classify(altered)

    assert not comparison.is_verbatim
    assert [f.code for f in warning.text_findings(comparison)] == ["warning_text_casing"]


def test_the_heading_check_is_untouched_by_the_capitals_exemption() -> None:
    """Jenny's catch (TC-03) never depended on the text comparison.

    `check_header_caps` compares the heading directly. This pins that the exemption for
    an all-caps BODY cannot be widened into forgiving a title-case heading.
    """
    title_cased = canon.CANONICAL_WARNING.replace(
        canon.WARNING_HEADER, "Government Warning:"
    )
    codes = [f.code for f in warning.check_header_caps(title_cased, None)]

    assert "warning_header_not_all_caps" in codes


def test_capitals_detection_ignores_a_fragment() -> None:
    """A scrap of text must not qualify as "set in capitals" by accident."""
    assert warning.is_set_in_capitals(canon.CANONICAL_WARNING.upper())
    assert not warning.is_set_in_capitals("GW:")
    assert not warning.is_set_in_capitals(canon.CANONICAL_WARNING)
