"""Properties of alcohol-content parsing, proof cross-check, and format rules.

Three jobs live in `api/rules/abv.py` and the module's docstring is emphatic that they
must not be conflated. Properties are the cheapest way to keep them apart: each one
below fixes two of the three and varies the remaining one, so a change that leaks
parsing into compliance, or compliance into comparison, breaks a named test.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from api import canon
from api.models import Commodity
from api.rules import abv

pytestmark = pytest.mark.property

SETTINGS = settings(max_examples=300, deadline=None)

#: The range a label can actually state. Two integer digits and up to two decimals is
#: what the parser's own pattern allows, so generating outside it would test the regex
#: rather than the rule.
ABV_VALUES = st.floats(
    min_value=0.0, max_value=99.99, allow_nan=False, allow_infinity=False
).map(lambda v: round(v, 2))

COMMODITIES = st.sampled_from(list(Commodity))


def _render(value: float) -> str:
    return f"{value:g}"


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


@SETTINGS
@given(ABV_VALUES)
def test_percent_statements_round_trip(value: float) -> None:
    """`45% Alc./Vol.` parses back to 45.0, for every value a label can state."""
    assert abv.parse(f"{_render(value)}% Alc./Vol.").abv == pytest.approx(value)


@SETTINGS
@given(
    ABV_VALUES,
    st.sampled_from(
        [
            "{v}% Alc./Vol.",
            "{v}% ALC/VOL",
            "Alcohol {v}% by volume",
            "alc. {v}% by vol.",
            "{v} percent alcohol by volume",
            "{v}%ALC/VOL",
            "ALC {v}% VOL",
        ]
    ),
)
def test_every_accepted_phrasing_yields_the_same_number(value: float, template: str) -> None:
    """Phrasing is presentation; the alcohol content is the fact.

    A label that says `alc. 45% by vol.` and one that says `45% ALC/VOL` state the same
    thing. If they parsed differently, one of them would reach the agent as a Mismatch
    against an application that says 45.
    """
    parsed = abv.parse(template.format(v=_render(value)))
    assert parsed.abv == pytest.approx(value)


@SETTINGS
@given(ABV_VALUES)
def test_parsing_is_idempotent_through_its_own_rendering(value: float) -> None:
    """Reparsing what we would display recovers the same number.

    The mismatch rationale prints `{abv:g}%` back to the agent. If that rendering did
    not round-trip, the number in the explanation would not be the number in the
    verdict.
    """
    parsed = abv.parse(f"{_render(value)}% Alc./Vol.")
    assert parsed.abv is not None
    assert abv.parse(f"{parsed.abv:g}%").abv == pytest.approx(value)


@SETTINGS
@given(st.text(max_size=40).filter(lambda s: not any(c.isdigit() for c in s)))
def test_text_with_no_digits_is_never_readable(text: str) -> None:
    """No number, no alcohol content — and never a guessed one.

    `is_readable` false routes to Unreadable or Missing. The one outcome that must be
    impossible is a fabricated value, because there is no channel downstream that
    distinguishes a guess from a reading (LP-067).
    """
    assert not abv.parse(text).is_readable


@pytest.mark.parametrize("text", [None, "", "   ", "\n\t "])
def test_absent_statements_are_unreadable_rather_than_zero(text: str | None) -> None:
    """Absence is not 0%. A label stating 0% and a label stating nothing are different."""
    parsed = abv.parse(text)
    assert parsed.abv is None
    assert not parsed.is_readable


@SETTINGS
@given(
    st.integers(min_value=0, max_value=99),
    st.sampled_from(
        [
            "Alcohol {v} by volume",
            "ALCOHOL {v} BY VOL",
            "alc. {v} vol",
            "Alc {v} by volume",
        ]
    ),
)
def test_an_alcohol_statement_with_no_percent_sign_still_parses(
    value: int, template: str
) -> None:
    """`Alcohol 45 by volume` — the number is unambiguous even with the sign dropped.

    Printers drop the `%` more often than you would expect, especially where the
    statement is set in small caps. Reporting the field as Missing would be a false
    finding on a label that states its alcohol content in words.
    """
    assert abv.parse(template.format(v=value)).abv == pytest.approx(value)


@SETTINGS
@given(st.integers(min_value=0, max_value=99))
def test_a_percent_sign_wins_over_a_bare_number(value: int) -> None:
    """When both readings are available the explicit one is used.

    `Alcohol 45% by volume` must parse as 45, not as whichever number the looser
    pattern happened to find first.
    """
    assert abv.parse(f"Alcohol {value}% by volume").abv == pytest.approx(value)


@SETTINGS
@given(st.integers(min_value=0, max_value=199))
def test_a_proof_only_label_still_states_its_alcohol_content(proof: int) -> None:
    """Proof implies ABV exactly, so a proof-only label is readable, not Missing.

    Reporting `90 Proof` as a missing alcohol statement would be a false finding on a
    label that states its alcohol content perfectly clearly.
    """
    parsed = abv.parse(f"{proof} Proof")
    assert parsed.abv == pytest.approx(proof / canon.PROOF_PER_ABV_POINT)
    assert parsed.proof == pytest.approx(proof)


# --------------------------------------------------------------------------------------
# Internal consistency (TC-09) — a property of the label alone
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-09")
@SETTINGS
@given(st.integers(min_value=0, max_value=49))
def test_a_consistent_label_raises_no_finding(value: int) -> None:
    """Proof exactly twice ABV is silent, at every value.

    A consistency check that fires on correct labels is worse than none: the agent
    learns to click past it, and the one real inconsistency goes with it.
    """
    parsed = abv.parse(f"{value}% Alc./Vol. ({value * 2} Proof)")
    assert abv.check_internal_consistency(parsed) == []


@pytest.mark.tc("TC-09")
@SETTINGS
@given(st.integers(min_value=0, max_value=49), st.integers(min_value=1, max_value=40))
def test_any_disagreement_between_proof_and_abv_is_reported(
    value: int, offset: int
) -> None:
    """Proof that is not twice ABV always produces a finding, whatever the gap.

    Independent of the application: this is the label contradicting itself. `40%
    Alc./Vol. (90 Proof)` is wrong whether the application says 40, 45, or nothing.
    """
    parsed = abv.parse(f"{value}% Alc./Vol. ({value * 2 + offset} Proof)")
    findings = abv.check_internal_consistency(parsed)
    assert [f.code for f in findings] == ["proof_abv_inconsistent"]
    assert findings[0].citation == canon.CITATIONS["spirits_abv"]


@SETTINGS
@given(ABV_VALUES)
def test_consistency_is_never_checked_against_a_proof_that_was_not_stated(
    value: float,
) -> None:
    """No proof on the label means nothing to cross-check — not a finding.

    Most labels state only a percentage. Reporting every one of them as inconsistent
    would bury the real TC-09 case in noise.
    """
    parsed = abv.parse(f"{_render(value)}% Alc./Vol.")
    assert abv.check_internal_consistency(parsed) == []


# --------------------------------------------------------------------------------------
# Format rules (TC-22) — commodity-specific, and only commodity-specific
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-22")
@SETTINGS
@given(ABV_VALUES)
def test_abv_abbreviation_is_a_finding_on_spirits_only(value: float) -> None:
    """27 CFR 5.65 restricts spirits to `alc.`/`vol.`; wine and malt may use ABV.

    Raising the finding on a beer label would be a false finding against a rule that
    does not apply to it, and false findings are how an agent learns to ignore the
    tool.
    """
    text = f"{_render(value)}% ABV"
    spirits = abv.check_format(text, Commodity.SPIRITS)
    assert [f.code for f in spirits] == ["spirits_abv_abbreviation"]
    for other in (Commodity.WINE, Commodity.MALT):
        assert abv.check_format(text, other) == []


@SETTINGS
@given(ABV_VALUES, COMMODITIES)
def test_the_permitted_abbreviations_never_raise_a_format_finding(
    value: float, commodity: Commodity
) -> None:
    assert abv.check_format(f"{_render(value)}% Alc./Vol.", commodity) == []


@SETTINGS
@given(COMMODITIES)
def test_absent_text_raises_no_format_finding(commodity: Commodity) -> None:
    """A missing statement is a Missing verdict, not a formatting complaint."""
    assert abv.check_format(None, commodity) == []
    assert abv.check_format("", commodity) == []


# --------------------------------------------------------------------------------------
# MATCH-8 — the tolerance is context, never an excuse
# --------------------------------------------------------------------------------------


@SETTINGS
@given(COMMODITIES, ABV_VALUES)
def test_tolerance_context_always_says_it_does_not_excuse_the_difference(
    commodity: Commodity, value: float
) -> None:
    """The sentence exists to stop a tolerance being read as permission.

    Tolerances govern the liquid against the label. This tool compares the label
    against the application and cannot measure liquid, so a tolerance can never excuse
    a mismatch. Every rendering of the sentence has to say so, in the agent's words —
    the number alone would read as a threshold that had been applied.
    """
    text = abv.tolerance_context(commodity, value)
    assert "does not excuse" in text
    assert "cannot measure" in text
    assert commodity.value in text


@SETTINGS
@given(ABV_VALUES)
def test_wine_tolerance_widens_at_or_below_fourteen_percent(value: float) -> None:
    """27 CFR 4.36's band boundary, checked from both sides at every value."""
    expected = (
        canon.WINE_ABV_TOLERANCE_PP_AT_OR_BELOW_14
        if value <= canon.WINE_TABLE_WINE_MAX_ABV
        else canon.WINE_ABV_TOLERANCE_PP_ABOVE_14
    )
    assert canon.abv_tolerance_pp("wine", value) == expected


@SETTINGS
@given(
    ABV_VALUES,
    st.sampled_from(
        [
            ("spirits", canon.SPIRITS_ABV_TOLERANCE_PP),
            ("malt", canon.MALT_ABV_TOLERANCE_PP),
        ]
    ),
)
def test_spirits_and_malt_tolerances_do_not_vary_with_strength(
    value: float, case: tuple[str, float]
) -> None:
    commodity, expected = case
    assert canon.abv_tolerance_pp(commodity, value) == expected


# `abv_tolerance_pp` refusing an unknown commodity is asserted in
# tests/test_canon.py::test_abv_tolerance_rejects_unknown_commodity. It lived here too,
# byte for byte, and this is the wrong file for it: the tolerance table is canon's, and a
# second copy in the ABV property tests is one more place to update and one more place to
# forget.


# --------------------------------------------------------------------------------------
# The three jobs stay separate
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-09")
@SETTINGS
@given(st.integers(min_value=1, max_value=49))
def test_an_inconsistent_label_still_parses_its_stated_percentage(value: int) -> None:
    """Parsing does not defer to the consistency check, and must not.

    TC-09's label reads `40% Alc./Vol. (90 Proof)`. The comparison against the
    application uses 40 — what the label says — while the finding reports that 90
    proof implies 45. Letting the check rewrite the parsed value would silently change
    the comparison verdict on the strength of a defect.
    """
    parsed = abv.parse(f"{value}% Alc./Vol. ({value * 2 + 10} Proof)")
    assert parsed.abv == pytest.approx(value)
    assert abv.check_internal_consistency(parsed)


@SETTINGS
@given(ABV_VALUES)
def test_the_format_finding_does_not_depend_on_the_parsed_value(value: float) -> None:
    """TC-22 is about the words, not the number. Both `45% ABV` and `5% ABV` offend."""
    assume(abv.parse(f"{_render(value)}% ABV").abv is not None)
    assert abv.check_format(f"{_render(value)}% ABV", Commodity.SPIRITS)
