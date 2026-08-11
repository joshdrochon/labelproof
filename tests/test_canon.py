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
    assert expected == canon.CANONICAL_WARNING


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
    assert all(c.strip() for c in canon.CITATIONS.values())


def test_every_citation_names_a_real_authority() -> None:
    """A CFR section, or a named TTB guidance page. Nothing vaguer than that."""
    for key, citation in canon.CITATIONS.items():
        assert re.match(r"^(27 CFR \d|TTB guidance: \S)", citation), f"{key}: {citation!r}"


# --- the type-size table is two steps, not one ------------------------------------------

def test_characters_per_inch_is_keyed_to_type_size_not_volume() -> None:
    """16.22(b)'s table is headed by type size. Volume selects the size; the size
    selects the density. Collapsing the two gives the right answer today and would
    silently give a wrong one if either half were amended."""
    assert canon.WARNING_MAX_CHARACTERS_PER_INCH == {1.0: 40, 2.0: 25, 3.0: 12}


def test_the_legacy_band_table_is_derived_not_transcribed_twice() -> None:
    for upper, mm, cpi in canon.WARNING_TYPE_SIZE_BANDS:
        assert canon.WARNING_MAX_CHARACTERS_PER_INCH[mm] == cpi
        assert (upper, mm) in canon.WARNING_MIN_TYPE_SIZE_BANDS


@pytest.mark.parametrize(
    ("volume_ml", "min_mm"), [(200.0, 1.0), (237.0, 1.0), (750.0, 2.0), (5000.0, 3.0)]
)
def test_minimum_type_size_alone(volume_ml: float, min_mm: float) -> None:
    assert canon.warning_min_type_size_mm(volume_ml) == min_mm


def test_the_warning_applies_from_half_a_percent(  ) -> None:
    """16.10. ABV alone is not the trigger — see the constant's comment — but this is
    the number, and it must not drift."""
    assert canon.WARNING_REQUIRED_MIN_ABV == 0.5


def test_even_liters_means_whole_liters() -> None:
    """4.72(b)'s own example lists 5 liters. A `% 2 == 0` reading would reject it."""
    assert canon.WINE_EVEN_LITER_FLOOR_ML == 4000.0
    record = canon.verification_for("WINE_STANDARDS_OF_FILL_ML / WINE_EVEN_LITER_FLOOR_ML")
    assert record is not None
    assert "even means whole" in record.note


# --- LP-328: the appendix itself is guarded ---------------------------------------------
#
# LP-022 asserts these constants match PRD Appendix B, which catches a transcription
# typo and cannot catch an appendix that was wrong to begin with. These assert that each
# figure was re-read against a primary source, that the source was named, and that the
# date was written down.


def test_every_regulatory_constant_has_a_verification_record() -> None:
    subjects = " | ".join(v.subject for v in canon.VERIFICATIONS)
    for name in (
        "CANONICAL_WARNING",
        "WARNING_HEADER",
        "WARNING_REQUIRED_MIN_ABV",
        "SPIRITS_STANDARDS_OF_FILL_ML",
        "WINE_STANDARDS_OF_FILL_ML",
        "MALT_HAS_STANDARDS_OF_FILL",
        "SPIRITS_ABV_TOLERANCE_PP",
        "MALT_ABV_TOLERANCE_PP",
        "WINE_ABV_TOLERANCE_PP_AT_OR_BELOW_14",
        "WINE_TABLE_WINE_MAX_ABV",
        "PROOF_PER_ABV_POINT",
        "SPIRITS_FORBIDDEN_ABV_ABBREVIATIONS",
        "WARNING_MIN_TYPE_SIZE_BANDS",
    ):
        assert name in subjects, f"{name} has no LP-328 verification record"


def test_no_verification_claims_a_check_it_did_not_do() -> None:
    """A record with no source or no date is a claim, not a check."""
    for record in canon.VERIFICATIONS:
        assert record.source.strip(), f"{record.subject}: no source"
        assert record.citation.strip(), f"{record.subject}: no citation"
        assert record.checked.year >= 2026, f"{record.subject}: no retrieval date"
        assert record.note.strip(), f"{record.subject}: no finding recorded"


def test_verification_subjects_are_unique() -> None:
    subjects = [v.subject for v in canon.VERIFICATIONS]
    assert len(subjects) == len(set(subjects))


def test_nothing_is_recorded_as_unconfirmed_without_saying_why() -> None:
    """An unconfirmed figure is allowed to exist. Silently shipping one is not."""
    for record in canon.VERIFICATIONS:
        if not record.confirmed:
            assert len(record.note) > 40, f"{record.subject}: unconfirmed with no detail"


def test_the_cfr_edition_the_check_read_is_recorded() -> None:
    """The known limit of the check: anything amended after this date is invisible to it."""
    assert canon.CFR_EDITION == "2025-04-01"


def test_the_warning_placement_rule_is_cited_to_the_right_section() -> None:
    """"Separate and apart from all other information" is in 16.21 with the statement,
    not in 16.22 with the type-style rules, where Appendix B files it."""
    assert canon.CITATIONS["warning_placement"] == "27 CFR 16.21"


def test_the_abv_abbreviation_rule_cites_guidance_not_the_regulation() -> None:
    """5.65 authorizes four abbreviations and never mentions "ABV", so a finding that
    cites the CFR for the prohibition overstates its source.

    Cited by page title rather than G-number: TTB's index lists this page as G 2021-1
    while the page body is stamped G 2021-4, which the index assigns to a different
    document. Hardcoding either side of that would assert something genuinely in doubt.
    """
    citation = canon.CITATIONS["spirits_abv_abbreviation"]
    assert citation.startswith("TTB guidance:")
    assert "2021" not in citation


def test_the_abv_abbreviation_finding_cites_the_guidance() -> None:
    """The user-visible half. A dict entry nothing reads is not a correction."""
    from api.models import Commodity
    from api.rules import abv

    finding = abv.check_format("45% ABV", Commodity.SPIRITS)[0]
    assert finding.citation == canon.CITATIONS["spirits_abv_abbreviation"]
    assert "27 CFR" not in (finding.citation or "")


def test_the_characters_per_inch_table_is_cited_to_its_own_subsection() -> None:
    """16.22(b) is prose mapping volume to millimetres. The characters-per-inch table
    is 16.22(a)(4) — an earlier pass cited (b) for both."""
    record = canon.verification_for(
        "WARNING_MIN_TYPE_SIZE_BANDS / WARNING_MAX_CHARACTERS_PER_INCH"
    )
    assert record is not None
    assert record.citation == "27 CFR 16.22(a)(4), (b)"
