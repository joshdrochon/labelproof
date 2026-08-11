"""Regressions found by real photographs, not by fixtures (Tier B).

Every case here was a live defect discovered by running an actual phone photo of an
actual bottle through the pipeline. The synthetic fixtures could not have found any of
them, which is the argument for Tier B existing at all.
"""

from __future__ import annotations

from api import canon
from api.models import Verdict
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


# --- the label carries the value inside a longer statement -------------------------------

def test_a_producer_printed_with_its_lead_in_phrase_is_not_a_mismatch() -> None:
    """Found on a Found North bottle and a Fireball bottle, independently.

    Labels do not print a bare producer. They print "PRODUCED BY SAZERAC CO., INC.,
    FRANKFORT, KY" and "DISTILLED IN CANADA. BOTTLED BY FOUND NORTH WHISKY, CAMBRIDGE,
    WI". The application record holds the bare value, so demanding equality reported a
    mismatch on every compliant label of this shape.
    """
    from api.models import ExtractedField
    from api.rules.compare import compare_producer

    result = compare_producer(
        ExtractedField(
            value="DISTILLED IN CANADA. BOTTLED BY FOUND NORTH WHISKY, CAMBRIDGE, WI",
            confidence=0.95,
            legible=True,
        ),
        "Found North Whisky",
        "Cambridge, WI",
    )

    assert result.verdict is not Verdict.MISMATCH
    assert result.verdict is Verdict.ACCEPTABLE_VARIATION
    # The extra words must be shown, not swallowed — an acceptable variation still has
    # to let the agent see what else the label says.
    assert "distilled in canada" in result.rationale.casefold()


def test_a_country_stated_as_distilled_in_is_not_a_mismatch() -> None:
    from api.models import ExtractedField
    from api.rules.compare import compare_country_of_origin

    result = compare_country_of_origin(
        ExtractedField(value="DISTILLED IN CANADA.", confidence=0.95, legible=True),
        "Canada",
        is_import=True,
    )

    assert result.verdict is Verdict.ACCEPTABLE_VARIATION


def test_containment_is_matched_on_word_boundaries() -> None:
    """A raw substring test would find "Canada" inside "Canadaville"."""
    from api.rules.normalize import contains_after_normalization

    assert contains_after_normalization("DISTILLED IN CANADA", "Canada")
    assert not contains_after_normalization("Product of Canadaville", "Canada")
    assert not contains_after_normalization("Canada", "Distilled in Canada")


def test_a_brand_name_does_not_get_the_surrounding_text_allowance() -> None:
    """A brand buried in a longer string is not the same claim as the brand.

    The allowance is scoped to fields whose regulated phrasing wraps the value. If it
    ever leaks onto brand_name, a label reading "NOT OLD TOM DISTILLERY" would pass.
    """
    from api.models import ExtractedField
    from api.rules.compare import compare_brand_name

    result = compare_brand_name(
        ExtractedField(value="DEFINITELY NOT OLD TOM DISTILLERY", confidence=0.9, legible=True),
        "Old Tom Distillery",
    )

    assert result.verdict is Verdict.MISMATCH
