"""Alcohol content parsing, proof cross-check, and format rules."""

import pytest

from api.models import Commodity
from api.rules import abv


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("45% Alc./Vol. (90 Proof)", 45.0),
        ("Alcohol 45% by volume", 45.0),
        ("alc. 45% by vol.", 45.0),
        ("45% ABV", 45.0),
        ("45%", 45.0),
        ("40.5% Alc./Vol.", 40.5),
        ("13.5% alcohol by volume", 13.5),
        ("5 percent alcohol by volume", 5.0),
    ],
)
def test_parses_abv_from_common_forms(text: str, expected: float) -> None:
    assert abv.parse(text).abv == expected


def test_parses_proof_alongside_abv() -> None:
    parsed = abv.parse("45% Alc./Vol. (90 Proof)")
    assert parsed.abv == 45.0
    assert parsed.proof == 90.0


def test_proof_only_label_implies_abv() -> None:
    parsed = abv.parse("90 Proof")
    assert parsed.abv == 45.0
    assert parsed.proof == 90.0


@pytest.mark.parametrize("text", [None, "", "   ", "no numbers here"])
def test_unreadable_returns_none_never_a_guess(text: str | None) -> None:
    """IMG-5 / LP-067: there is no fabricated-value path."""
    parsed = abv.parse(text)
    assert parsed.abv is None
    assert not parsed.is_readable


# --- TC-09: internal consistency ------------------------------------------------------

@pytest.mark.tc("TC-09")
def test_proof_inconsistency_is_flagged() -> None:
    parsed = abv.parse("40% Alc./Vol. (90 Proof)")
    findings = abv.check_internal_consistency(parsed)
    assert len(findings) == 1
    assert findings[0].code == "proof_abv_inconsistent"
    assert "45" in findings[0].message


@pytest.mark.tc("TC-09")
def test_consistent_proof_raises_nothing() -> None:
    assert abv.check_internal_consistency(abv.parse("45% Alc./Vol. (90 Proof)")) == []


def test_consistency_check_needs_both_values() -> None:
    assert abv.check_internal_consistency(abv.parse("45% Alc./Vol.")) == []


# --- TC-22: spirits format ------------------------------------------------------------

@pytest.mark.tc("TC-22")
def test_bare_abv_on_spirits_is_a_format_finding() -> None:
    findings = abv.check_format("45% ABV", Commodity.SPIRITS)
    assert len(findings) == 1
    assert findings[0].code == "spirits_abv_abbreviation"


@pytest.mark.tc("TC-22")
def test_alc_vol_on_spirits_is_fine() -> None:
    assert abv.check_format("45% Alc./Vol.", Commodity.SPIRITS) == []


@pytest.mark.parametrize("commodity", [Commodity.WINE, Commodity.MALT])
def test_abv_abbreviation_is_permitted_off_spirits(commodity: Commodity) -> None:
    assert abv.check_format("5% ABV", commodity) == []


# --- tolerance context ----------------------------------------------------------------

def test_tolerance_context_never_reads_as_an_excuse() -> None:
    """MATCH-8: tolerance governs liquid vs label, which this tool cannot measure."""
    text = abv.tolerance_context(Commodity.SPIRITS, 45.0)
    assert "0.3" in text
    assert "does not excuse" in text
