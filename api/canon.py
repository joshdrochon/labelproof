"""Regulatory constants — the data the rules engine encodes.

Every value here is transcribed from the Code of Federal Regulations and carries its
citation. Verified 2026-08-10 against Cornell LII's eCFR mirror (ecfr.gov served a
bot-block). Re-verification is tracked as LP-328.

Nothing in this module may be "fixed" to make a test pass. If a value here disagrees
with an implementation, the implementation is wrong.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------------------
# Government health warning statement — 27 CFR 16.21
# --------------------------------------------------------------------------------------

#: The statement, character for character. Note the COLON after GOVERNMENT WARNING —
#: 16.22 cites the phrase as `GOVERNMENT WARNING,` with a comma when stating the bold
#: requirement, but the statement itself in 16.21 uses a colon. Encode the colon.
CANONICAL_WARNING: Final[str] = (
    "GOVERNMENT WARNING: (1) According to the Surgeon General, women should not "
    "drink alcoholic beverages during pregnancy because of the risk of birth defects. "
    "(2) Consumption of alcoholic beverages impairs your ability to drive a car or "
    "operate machinery, and may cause health problems."
)

#: The portion required to be capitalized AND bold (27 CFR 16.22).
WARNING_HEADER: Final[str] = "GOVERNMENT WARNING:"

#: The remainder — which must NOT be bold. This is the inverse rule almost everyone
#: misses, and it is a real requirement, not an inference (16.22).
WARNING_BODY: Final[str] = CANONICAL_WARNING[len(WARNING_HEADER) :].strip()


# --------------------------------------------------------------------------------------
# Warning type size — 27 CFR 16.22
# --------------------------------------------------------------------------------------

#: (max container volume in mL or None for unbounded, min type size mm, max chars/inch).
#: Bands are upper-exclusive at the boundary: 237 mL falls in the first band, 238 in the
#: second, because the regulation reads "237 mL or less" then "more than 237 mL".
WARNING_TYPE_SIZE_BANDS: Final[tuple[tuple[float | None, float, int], ...]] = (
    (237.0, 1.0, 40),
    (3000.0, 2.0, 25),
    (None, 3.0, 12),
)


def warning_type_size_for(net_contents_ml: float) -> tuple[float, int]:
    """Return (minimum type size in mm, maximum characters per inch) for a container.

    Displayed to the agent as *context only*. Absolute type size is not measurable from
    an unscaled photograph — see WARN-9. The app must never claim to have verified it.
    """
    for upper, min_mm, max_cpi in WARNING_TYPE_SIZE_BANDS:
        if upper is None or net_contents_ml <= upper:
            return min_mm, max_cpi
    raise AssertionError("unreachable: final band is unbounded")


# --------------------------------------------------------------------------------------
# Standards of fill
# --------------------------------------------------------------------------------------

#: Distilled spirits — 27 CFR 5.203. Twenty-five authorized sizes. The can/non-can
#: distinction was eliminated by the TTB final rule effective 2025-01-10.
SPIRITS_STANDARDS_OF_FILL_ML: Final[frozenset[float]] = frozenset(
    {
        50.0, 100.0, 187.0, 200.0, 250.0, 331.0, 350.0, 355.0, 375.0,
        475.0, 500.0, 570.0, 700.0, 710.0, 720.0, 750.0, 900.0, 945.0,
        1000.0, 1500.0, 1750.0, 1800.0, 2000.0, 3000.0, 3750.0,
    }
)

#: Wine — 27 CFR 4.72. Discrete sizes below 4 L; at or above 4 L, any even liter.
WINE_STANDARDS_OF_FILL_ML: Final[frozenset[float]] = frozenset(
    {
        50.0, 100.0, 180.0, 187.0, 200.0, 250.0, 300.0, 330.0, 355.0, 360.0,
        375.0, 473.0, 500.0, 550.0, 568.0, 600.0, 620.0, 700.0, 720.0, 750.0,
        1000.0, 1500.0, 1800.0, 2250.0, 3000.0,
    }
)

#: Malt beverages have no federal standards of fill. Net contents must simply be
#: stated accurately, so any value is acceptable and only the statement is checked.
MALT_HAS_STANDARDS_OF_FILL: Final[bool] = False


# --------------------------------------------------------------------------------------
# Alcohol content tolerances (liquid vs. label)
# --------------------------------------------------------------------------------------

#: Percentage-point tolerances permitted between the actual alcohol content of the
#: liquid and what the label states.
#:
#: CRITICAL: these govern liquid-vs-label. This tool compares LABEL-vs-APPLICATION and
#: cannot measure liquid. Tolerances are surfaced as context on a finding and must never
#: be used to excuse a label/application mismatch (MATCH-8). A 45%-vs-40% difference is
#: a Mismatch regardless of any tolerance below.
SPIRITS_ABV_TOLERANCE_PP: Final[float] = 0.3
MALT_ABV_TOLERANCE_PP: Final[float] = 0.3
WINE_ABV_TOLERANCE_PP_AT_OR_BELOW_14: Final[float] = 1.5
WINE_ABV_TOLERANCE_PP_ABOVE_14: Final[float] = 1.0

#: Wine at or below this ABV may omit alcohol content entirely if labelled
#: "table wine" or "light wine" (27 CFR 4.36).
WINE_TABLE_WINE_MAX_ABV: Final[float] = 14.0

#: Proof is exactly twice alcohol-by-volume. `90 Proof` alongside `40% Alc./Vol.` is an
#: internal label inconsistency, reported independently of the application comparison.
PROOF_PER_ABV_POINT: Final[float] = 2.0


def abv_tolerance_pp(commodity: str, abv: float) -> float:
    """Regulatory tolerance in percentage points, for display as context only."""
    match commodity:
        case "spirits":
            return SPIRITS_ABV_TOLERANCE_PP
        case "malt":
            return MALT_ABV_TOLERANCE_PP
        case "wine":
            return (
                WINE_ABV_TOLERANCE_PP_AT_OR_BELOW_14
                if abv <= WINE_TABLE_WINE_MAX_ABV
                else WINE_ABV_TOLERANCE_PP_ABOVE_14
            )
        case _:
            raise ValueError(f"unknown commodity: {commodity!r}")


# --------------------------------------------------------------------------------------
# Alcohol content format rules
# --------------------------------------------------------------------------------------

#: On distilled spirits labels only `alc.` and `vol.` abbreviations are permitted. A bare
#: "ABV" on a spirits label is a format finding (TC-22).
SPIRITS_PERMITTED_ABV_ABBREVIATIONS: Final[frozenset[str]] = frozenset({"alc", "vol"})
SPIRITS_FORBIDDEN_ABV_ABBREVIATIONS: Final[frozenset[str]] = frozenset({"abv"})


# --------------------------------------------------------------------------------------
# Citations, for rendering alongside findings in the UI
# --------------------------------------------------------------------------------------

CITATIONS: Final[dict[str, str]] = {
    "warning_text": "27 CFR 16.21",
    "warning_format": "27 CFR 16.22",
    "spirits_fill": "27 CFR 5.203",
    "wine_fill": "27 CFR 4.72",
    "wine_abv": "27 CFR 4.36",
    "malt_abv": "27 CFR 7.65",
}
