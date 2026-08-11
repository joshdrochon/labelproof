"""Regulatory constants — the data the rules engine encodes.

Every value here is transcribed from the Code of Federal Regulations and carries its
citation. Nothing in this module may be "fixed" to make a test pass. If a value here
disagrees with an implementation, the implementation is wrong.

**Two layers of guard, and they guard different things.** LP-022 asserts that the
constants match PRD Appendix B, which catches a typo introduced while transcribing. It
cannot catch an appendix that was wrong to begin with. LP-328 is the second layer:
each figure re-read against a primary source, with the source actually fetched and the
date recorded, in `VERIFICATIONS` below. Provenance is data here rather than a comment
so a stale check is visible in a test rather than in a paragraph nobody reads.

eCFR (ecfr.gov) redirects automated fetches to an unblock page, so the primary source
used is the GPO's govinfo CFR XML — the same text, published by the same office —
cross-checked against Cornell LII. Both are named per item.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

# --------------------------------------------------------------------------------------
# Government health warning statement — 27 CFR 16.21
# --------------------------------------------------------------------------------------

#: The statement, character for character.
#:
#: The heading ends in a COLON. 16.22(a)(2) quotes the phrase as `"GOVERNMENT WARNING,"`
#: when stating the bold requirement, and that comma sits *inside* the closing quotation
#: mark — American typographic convention applied to the regulation's own sentence, not
#: punctuation it prescribes. The statement itself, in 16.21, ends the heading with a
#: colon. (Appendix B reads this as the regulation citing the phrase with a comma; the
#: conclusion is the same either way, and the reasoning is corrected here. See LP-328.)
#:
#: 16.21 sets the statement as two paragraphs, with `(2)` starting a new one. Stored on
#: one line because `warning.collapse_layout_whitespace` treats the break as layout, so
#: a label that sets it as one paragraph, two paragraphs, or seven wrapped lines all
#: compare equal — which is correct: the regulation prescribes words, not a column width.
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

#: (max container volume in mL or None for unbounded, min type size mm).
#: Bands are upper-inclusive: 237 mL falls in the first band and 238 in the second,
#: because 16.22(b) reads "237 milliliters (8 fl. oz.) or less" then "more than 237
#: milliliters ... up to 3 liters" then "more than 3 liters".
WARNING_MIN_TYPE_SIZE_BANDS: Final[tuple[tuple[float | None, float], ...]] = (
    (237.0, 1.0),
    (3000.0, 2.0),
    (None, 3.0),
)

#: Maximum characters per inch, keyed to *type size* rather than to container volume.
#: This is the regulation's own shape: 16.22(b)'s table is headed "Minimum required type
#: size for warning statement" and maps that to a character density. Volume selects the
#: type size; the type size selects the density. Collapsing the two steps gives the same
#: answer today and would silently give a wrong one if either half were ever amended.
WARNING_MAX_CHARACTERS_PER_INCH: Final[dict[float, int]] = {
    1.0: 40,
    2.0: 25,
    3.0: 12,
}

#: Legacy shape — (volume, mm, cpi) — kept because Appendix B and LP-022 describe the
#: bands this way. Derived, never hand-maintained, so the two cannot drift apart.
WARNING_TYPE_SIZE_BANDS: Final[tuple[tuple[float | None, float, int], ...]] = tuple(
    (upper, mm, WARNING_MAX_CHARACTERS_PER_INCH[mm])
    for upper, mm in WARNING_MIN_TYPE_SIZE_BANDS
)


def warning_min_type_size_mm(net_contents_ml: float) -> float:
    """Minimum type size for a container, in millimetres (16.22(b))."""
    for upper, min_mm in WARNING_MIN_TYPE_SIZE_BANDS:
        if upper is None or net_contents_ml <= upper:
            return min_mm
    raise AssertionError("unreachable: final band is unbounded")


def warning_type_size_for(net_contents_ml: float) -> tuple[float, int]:
    """Return (minimum type size in mm, maximum characters per inch) for a container.

    Displayed to the agent as *context only*. Absolute type size is not measurable from
    an unscaled photograph — see WARN-9. The app must never claim to have verified it.
    """
    min_mm = warning_min_type_size_mm(net_contents_ml)
    return min_mm, WARNING_MAX_CHARACTERS_PER_INCH[min_mm]


#: 16.10 defines the beverages Part 16 applies to: "not less than one-half of one percent
#: (.5%) of alcohol by volume". Two further conditions ride with it — the beverage must be
#: "in liquid form" and "intended for human consumption" — so ABV alone is not the trigger.
WARNING_REQUIRED_MIN_ABV: Final[float] = 0.5


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

#: Wine containers of 4 L and above may be filled "in quantities of even liters (4
#: liters, 5 liters, 6 liters, etc.)" — 27 CFR 4.72(b). **"Even" means whole, not
#: even-numbered.** The regulation's own example lists 5 liters, so a `% 2 == 0` test
#: would reject an authorized size. Confirmed 2026-08-11 against govinfo's CFR XML;
#: `api/rules/fills.py` reads it the same way.
WINE_EVEN_LITER_FLOOR_ML: Final[float] = 4000.0

#: Malt beverages have no federal standards of fill. Net contents must simply be
#: stated accurately, so any value is acceptable and only the statement is checked.
#: Confirmed by absence: the 2025 edition of 27 CFR part 7 contains no occurrence of
#: "standards of fill"; 7.70 governs only how net contents are expressed.
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
#:
#: The authority here is TTB guidance, not the CFR alone, and the finding cites it that
#: way. 5.65(b)(3) permits four substitutions — `alc` for alcohol, `%` for percent, `/`
#: for "by", `vol` for volume — and 5.65(b)(2)(i)'s three statement formats "must appear
#: as shown". It never mentions "ABV". TTB G 2021-4 draws the conclusion out loud: "Only
#: 'alc.' and 'vol.' may be used ... The abbreviation 'ABV' is not allowed." Checked
#: 2026-08-11; Appendix B attributes this to the regulation and should say guidance.
SPIRITS_PERMITTED_ABV_ABBREVIATIONS: Final[frozenset[str]] = frozenset({"alc", "vol"})
SPIRITS_FORBIDDEN_ABV_ABBREVIATIONS: Final[frozenset[str]] = frozenset({"abv"})


# --------------------------------------------------------------------------------------
# Citations, for rendering alongside findings in the UI
# --------------------------------------------------------------------------------------

CITATIONS: Final[dict[str, str]] = {
    "warning_text": "27 CFR 16.21",
    "warning_format": "27 CFR 16.22",
    # "separate and apart from all other information" is in 16.21, with the statement
    # itself — not in 16.22 with the type-style rules, where Appendix B files it.
    "warning_placement": "27 CFR 16.21",
    "warning_scope": "27 CFR 16.10",
    "spirits_fill": "27 CFR 5.203",
    "wine_fill": "27 CFR 4.72",
    "wine_abv": "27 CFR 4.36",
    "malt_abv": "27 CFR 7.65",
    "spirits_abv": "27 CFR 5.65",
    # The CFR authorizes four abbreviations and is silent on "ABV". The prohibition is
    # TTB guidance, and a finding that cites 5.65 for it is overstating its source.
    "spirits_abv_abbreviation": "TTB G 2021-4",
}


# --------------------------------------------------------------------------------------
# Verification record (LP-328)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verification:
    """One figure in this module, re-read against a primary source on a known date.

    `source` is the document actually fetched, not the one we would have liked to fetch.
    eCFR redirects automated requests to an unblock page, so these read the GPO's govinfo
    CFR XML — same text, same office — cross-checked against Cornell LII.
    """

    subject: str
    citation: str
    source: str
    checked: date
    confirmed: bool
    note: str = ""


#: The CFR annual edition the text below was read from. Anything Congress or TTB amends
#: after this date would not appear in it, which is the known limit of this check.
CFR_EDITION: Final[str] = "2025-04-01"

_GOVINFO = "govinfo.gov/content/pkg/CFR-2025-title27-vol1/xml"

VERIFICATIONS: Final[tuple[Verification, ...]] = (
    Verification(
        subject="CANONICAL_WARNING",
        citation="27 CFR 16.21",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec16-21.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "Character-identical, including the colon, both commas, the (1)/(2) "
            "numbering and the final period. The CFR sets it as two paragraphs; stored "
            "here on one line because paragraph breaks are layout."
        ),
    ),
    Verification(
        subject="WARNING_HEADER",
        citation="27 CFR 16.22(a)(2)",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec16-22.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "Reads: the first two words \"GOVERNMENT WARNING,\" shall appear in "
            "capital letters and in bold type; the remainder may not appear in bold "
            "type. The comma is inside the quotation mark and is not prescribed "
            "punctuation — Appendix B's reasoning is corrected above."
        ),
    ),
    Verification(
        subject="WARNING_MIN_TYPE_SIZE_BANDS / WARNING_MAX_CHARACTERS_PER_INCH",
        citation="27 CFR 16.22(b)",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec16-22.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "Boundaries confirmed as '237 mL or less' / 'more than 237 mL "
            "up to 3 liters' / 'more than 3 liters', and 1/2/3 mm mapping "
            "to 40/25/12 characters per inch. The table is keyed to type size, not to "
            "volume; encoded here as two steps for that reason."
        ),
    ),
    Verification(
        subject="WARNING_REQUIRED_MIN_ABV",
        citation="27 CFR 16.10",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec16-10.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "'not less than one-half of one percent (.5%) of alcohol by volume', "
            "and also 'in liquid form' and 'intended for human "
            "consumption'. ABV alone is not the trigger."
        ),
    ),
    Verification(
        subject="SPIRITS_STANDARDS_OF_FILL_ML",
        citation="27 CFR 5.203",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec5-203.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "All 25 sizes match, 5.203(a)(1)-(25). The can/non-can distinction was "
            "removed by T.D. TTB-200 (90 FR 1873), effective 2025-01-10; TTB declined "
            "the broader proposal to abolish standards of fill, so the list stays closed."
        ),
    ),
    Verification(
        subject="WINE_STANDARDS_OF_FILL_ML / WINE_EVEN_LITER_FLOOR_ML",
        citation="27 CFR 4.72",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec4-72.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "All 25 sizes match. 4.72(b) reads 'even liters (4 liters, 5 liters, 6 "
            "liters, etc.)' — even means whole, not even-numbered. The "
            "govinfo source credit for 4.72 carries a typo dating T.D. TTB-200 to "
            "Jan 20; the rule itself is Jan 10."
        ),
    ),
    Verification(
        subject="MALT_HAS_STANDARDS_OF_FILL",
        citation="27 CFR part 7",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-part7.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "Confirmed by absence — no occurrence of 'standards of fill' "
            "anywhere in part 7. 7.70 governs only how net contents are expressed."
        ),
    ),
    Verification(
        subject="SPIRITS_ABV_TOLERANCE_PP",
        citation="27 CFR 5.65(c)",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec5-65.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "±0.3. Framed as actual alcohol content of the liquid against the "
            "labelled figure — a laboratory tolerance, not a labelling one, which "
            "is why this tool never uses it to excuse a mismatch."
        ),
    ),
    Verification(
        subject="MALT_ABV_TOLERANCE_PP",
        citation="27 CFR 7.65(c)",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec7-65.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "±0.3, with conditions this module does not encode: a beverage labelled "
            "at 0.5% or above may never actually fall below 0.5%, and 7.65(d)/(f) allow "
            "no tolerance at all for 'low alcohol', 'reduced alcohol' "
            "or 'alcohol free'."
        ),
    ),
    Verification(
        subject="WINE_ABV_TOLERANCE_PP_AT_OR_BELOW_14 / _ABOVE_14",
        citation="27 CFR 4.36(b)",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec4-36.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "1.5 at or below 14%, 1.0 above. The regulation says 'percent' "
            "where we say percentage points. 4.36(b)(2) also allows a stated range with "
            "no tolerance outside it, which this module does not encode."
        ),
    ),
    Verification(
        subject="WINE_TABLE_WINE_MAX_ABV",
        citation="27 CFR 4.36(a)",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec4-36.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "Alcohol content need not be stated for wine at 14% or less if the type "
            "designation 'table' wine or 'light' wine appears on the "
            "brand label per 4.32(a)(2). The designation is a condition, not a synonym."
        ),
    ),
    Verification(
        subject="PROOF_PER_ABV_POINT",
        citation="27 CFR 5.1",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec5-1.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "'Proof. The ethyl alcohol content of a liquid at 60 degrees "
            "Fahrenheit, stated as twice the percentage of ethyl alcohol by volume.'"
        ),
    ),
    Verification(
        subject="SPIRITS_FORBIDDEN_ABV_ABBREVIATIONS",
        citation="TTB G 2021-4",
        source="ttb.gov/.../ds-labeling-home/ds-alcohol-content",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "The prohibition is guidance, not regulation. 5.65 authorizes four "
            "substitutions and never mentions 'ABV'; TTB G 2021-4 says "
            "'The abbreviation \"ABV\" is not allowed.' The conclusion "
            "holds — 5.65(b)(2)(i)'s formats 'must appear as shown' — "
            "but a finding should cite the guidance."
        ),
    ),
    Verification(
        subject="MALT_ABV optionality",
        citation="27 CFR 7.65(a)",
        source=f"{_GOVINFO}/CFR-2025-title27-vol1-sec7-65.xml",
        checked=date(2026, 8, 11),
        confirmed=True,
        note=(
            "'may be stated on any malt beverage label, unless prohibited by State "
            "law'. Note the direction: the regulation contemplates States "
            "*prohibiting* the statement as well as requiring it."
        ),
    ),
)


def verification_for(subject: str) -> Verification | None:
    """The verification record covering a constant, if one exists."""
    return next((v for v in VERIFICATIONS if v.subject == subject), None)
