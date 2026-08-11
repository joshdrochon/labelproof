"""Guards on the regulatory constants.

These are not unit tests of behaviour — they are tripwires. If a constant here drifts,
the rules engine encodes a wrong regulation and every downstream test passes anyway.
"""

import re

import pytest

from api import canon


def test_canonical_warning_is_character_exact() -> None:
    """27 CFR 16.21, verified against Cornell LII 2026-08-10."""
    expected = (
        "GOVERNMENT WARNING: (1) According to the Surgeon General, women should not "
        "drink alcoholic beverages during pregnancy because of the risk of birth "
        "defects. (2) Consumption of alcoholic beverages impairs your ability to "
        "drive a car or operate machinery, and may cause health problems."
    )
    assert canon.CANONICAL_WARNING == expected


def test_warning_header_uses_a_colon_not_a_comma() -> None:
    """16.22 cites the phrase with a comma; the statement itself uses a colon."""
    assert canon.WARNING_HEADER == "GOVERNMENT WARNING:"
    assert canon.CANONICAL_WARNING.startswith(canon.WARNING_HEADER)


def test_warning_header_and_body_reconstruct_the_statement() -> None:
    assert f"{canon.WARNING_HEADER} {canon.WARNING_BODY}" == canon.CANONICAL_WARNING


def test_warning_body_carries_both_numbered_clauses() -> None:
    assert canon.WARNING_BODY.startswith("(1)")
    assert "(2)" in canon.WARNING_BODY


def test_spirits_has_exactly_25_authorized_sizes() -> None:
    """27 CFR 5.203 — the count is stated in the regulation itself."""
    assert len(canon.SPIRITS_STANDARDS_OF_FILL_ML) == 25


@pytest.mark.parametrize("size", [750.0, 1750.0, 50.0, 3750.0])
def test_common_spirits_sizes_are_authorized(size: float) -> None:
    assert size in canon.SPIRITS_STANDARDS_OF_FILL_ML


def test_733ml_is_not_an_authorized_spirits_size() -> None:
    """TC-10 depends on this being absent."""
    assert 733.0 not in canon.SPIRITS_STANDARDS_OF_FILL_ML


@pytest.mark.parametrize(
    ("volume_ml", "min_mm", "max_cpi"),
    [
        (200.0, 1.0, 40),
        (237.0, 1.0, 40),      # boundary: "237 mL or less"
        (237.5, 2.0, 25),      # boundary: "more than 237 mL"
        (750.0, 2.0, 25),
        (3000.0, 2.0, 25),     # boundary: "up to 3 liters"
        (3000.5, 3.0, 12),     # boundary: "more than 3 liters"
    ],
)
def test_warning_type_size_bands(volume_ml: float, min_mm: float, max_cpi: int) -> None:
    assert canon.warning_type_size_for(volume_ml) == (min_mm, max_cpi)


@pytest.mark.parametrize(
    ("commodity", "abv", "expected"),
    [
        ("spirits", 45.0, 0.3),
        ("malt", 5.0, 0.3),
        ("wine", 12.0, 1.5),   # table-wine band
        ("wine", 14.0, 1.5),   # boundary is inclusive
        ("wine", 14.5, 1.0),
    ],
)
def test_abv_tolerances(commodity: str, abv: float, expected: float) -> None:
    assert canon.abv_tolerance_pp(commodity, abv) == expected


def test_abv_tolerance_rejects_unknown_commodity() -> None:
    with pytest.raises(ValueError, match="unknown commodity"):
        canon.abv_tolerance_pp("cider", 5.0)


def test_proof_is_exactly_twice_abv() -> None:
    assert canon.PROOF_PER_ABV_POINT * 45.0 == 90.0


def test_wine_allows_even_liters_at_or_above_4l_only_via_rule_not_table() -> None:
    """4 L and up is a rule (any even liter), deliberately not enumerated."""
    assert 4000.0 not in canon.WINE_STANDARDS_OF_FILL_ML
    assert max(canon.WINE_STANDARDS_OF_FILL_ML) == 3000.0


def test_no_citation_is_left_blank() -> None:
    assert all(re.match(r"^27 CFR ", c) for c in canon.CITATIONS.values())
