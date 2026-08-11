"""Net contents parsing and standards-of-fill validation. TC-10 is the named case."""

import pytest

from api.models import Commodity
from api.rules import fills


@pytest.mark.parametrize(
    ("text", "expected_ml"),
    [
        ("750 mL", 750.0),
        ("750ML", 750.0),
        ("750ml", 750.0),
        ("75 cl", 750.0),
        ("75cl", 750.0),
        ("1 L", 1000.0),
        ("1.75L", 1750.0),
        ("1,75 L", 1750.0),          # European decimal comma
        ("1.75 liters", 1750.0),
        ("50 mL", 50.0),
        ("7.5 dl", 750.0),
    ],
)
def test_parses_to_millilitres(text: str, expected_ml: float) -> None:
    assert fills.parse(text).ml == pytest.approx(expected_ml)


def test_parses_fluid_ounces() -> None:
    assert fills.parse("25.4 fl oz").ml == pytest.approx(751.2, abs=0.5)


@pytest.mark.parametrize("text", [None, "", "   ", "net contents", "abc mL"])
def test_unreadable_returns_none_never_a_guess(text: str | None) -> None:
    parsed = fills.parse(text)
    assert parsed.ml is None
    assert not parsed.is_readable


def test_same_volume_in_different_units_is_equal() -> None:
    assert fills.equal(fills.parse("750 mL"), fills.parse("75 cl"))
    assert fills.equal(fills.parse("1 L"), fills.parse("1000 mL"))


def test_different_volumes_are_not_equal() -> None:
    assert not fills.equal(fills.parse("750 mL"), fills.parse("700 mL"))


def test_unreadable_never_compares_equal() -> None:
    assert not fills.equal(fills.parse("750 mL"), fills.parse(""))


# --- TC-10: matches the application AND is non-compliant ------------------------------

@pytest.mark.tc("TC-10")
def test_733ml_raises_a_standards_of_fill_finding() -> None:
    findings = fills.check_standards_of_fill(fills.parse("733 mL"), Commodity.SPIRITS)
    assert len(findings) == 1
    assert findings[0].code == "non_standard_fill"
    assert "733" in findings[0].message


@pytest.mark.tc("TC-10")
def test_733ml_matching_the_application_does_not_suppress_the_finding() -> None:
    """The comparison and the compliance check are independent."""
    label, application = fills.parse("733 mL"), fills.parse("733 mL")
    assert fills.equal(label, application)
    assert fills.check_standards_of_fill(label, Commodity.SPIRITS)


def test_finding_names_the_nearest_authorized_size() -> None:
    findings = fills.check_standards_of_fill(fills.parse("733 mL"), Commodity.SPIRITS)
    assert "720 mL" in findings[0].message


@pytest.mark.parametrize("size", ["750 mL", "1.75 L", "50 mL", "375 mL"])
def test_authorized_spirits_sizes_raise_nothing(size: str) -> None:
    assert fills.check_standards_of_fill(fills.parse(size), Commodity.SPIRITS) == []


# --- wine ------------------------------------------------------------------------------

@pytest.mark.parametrize("size", ["750 mL", "1.5 L", "187 mL", "3 L"])
def test_authorized_wine_sizes_raise_nothing(size: str) -> None:
    assert fills.check_standards_of_fill(fills.parse(size), Commodity.WINE) == []


@pytest.mark.parametrize("size", ["4 L", "5 L", "6 L", "10 L"])
def test_wine_allows_whole_liters_at_or_above_4l(size: str) -> None:
    assert fills.check_standards_of_fill(fills.parse(size), Commodity.WINE) == []


def test_wine_rejects_fractional_liters_above_4l() -> None:
    assert fills.check_standards_of_fill(fills.parse("4.5 L"), Commodity.WINE)


def test_wine_3750ml_is_not_authorized_even_though_spirits_allows_it() -> None:
    """3.75 L is a spirits size; wine's list stops at 3 L and its rule starts at 4 L."""
    assert fills.check_standards_of_fill(fills.parse("3.75 L"), Commodity.WINE)
    assert fills.check_standards_of_fill(fills.parse("3.75 L"), Commodity.SPIRITS) == []


# --- malt ------------------------------------------------------------------------------

@pytest.mark.parametrize("size", ["355 mL", "733 mL", "12 fl oz", "1 L"])
def test_malt_has_no_standards_of_fill(size: str) -> None:
    """Any accurately stated volume is acceptable for malt beverages."""
    assert fills.check_standards_of_fill(fills.parse(size), Commodity.MALT) == []


def test_unreadable_net_contents_raises_no_compliance_finding() -> None:
    """Cannot judge compliance of a value we could not read."""
    assert fills.check_standards_of_fill(fills.parse(""), Commodity.SPIRITS) == []
