"""Net contents parsing and standards-of-fill validation.

Two independent checks, and conflating them is the trap:

1. **Comparison** — does the label's net contents match the application's?
2. **Compliance** — is that volume an authorized container size?

A label reading `733 mL` against an application reading `733 mL` *matches* and is still
non-compliant, because 733 mL is not an authorized size (TC-10). The verdict carries the
comparison; a Finding carries the compliance failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from api import canon
from api.models import Commodity, Finding

#: US fluid ounce. TTB works in US customary units.
_ML_PER_FL_OZ = 29.5735295625

_UNIT_TO_ML: dict[str, float] = {
    "ml": 1.0,
    "milliliter": 1.0,
    "millilitre": 1.0,
    "cl": 10.0,
    "centiliter": 10.0,
    "centilitre": 10.0,
    "dl": 100.0,
    "deciliter": 100.0,
    "decilitre": 100.0,
    "l": 1000.0,
    "liter": 1000.0,
    "litre": 1000.0,
    "floz": _ML_PER_FL_OZ,
    "flozs": _ML_PER_FL_OZ,
    "fluidounce": _ML_PER_FL_OZ,
    "fluidounces": _ML_PER_FL_OZ,
    "oz": _ML_PER_FL_OZ,
}

_QUANTITY = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(fl\.?\s*oz\.?|fluid\s*ounces?|ml|cl|dl|l|milli ?lit(?:er|re)s?|"
    r"centi ?lit(?:er|re)s?|deci ?lit(?:er|re)s?|lit(?:er|re)s?|oz\.?)\b",
    re.IGNORECASE,
)

#: Wine 4 L and above may be filled in even liters. The regulation's own example reads
#: "(4 liters, 5 liters, 6 liters, etc.)", so "even" means whole liters, not
#: even-numbered ones. See JUDGMENT-LOG — this reading is not yet verified against eCFR.
_WINE_EVEN_LITER_FLOOR_ML = 4000.0


@dataclass(frozen=True)
class NetContents:
    """A parsed net contents statement, canonicalized to millilitres."""

    ml: float | None = None
    raw: str = ""

    @property
    def is_readable(self) -> bool:
        return self.ml is not None


def parse(text: str | None) -> NetContents:
    """Parse net contents to millilitres. Returns ml=None when unreadable.

    Handles `750 mL`, `750ML`, `75 cl`, `1 L`, `1.75L`, `25.4 fl oz`, and the European
    decimal comma (`1,75 L`).
    """
    if not text or not text.strip():
        return NetContents(raw=text or "")

    match = _QUANTITY.search(text)
    if not match:
        return NetContents(raw=text)

    amount = float(match.group(1).replace(",", "."))
    unit = re.sub(r"[.\s]", "", match.group(2)).lower()
    factor = _UNIT_TO_ML.get(unit)
    if factor is None and unit.endswith("s"):
        # `liters`, `milliliters`, `fluid ounces` — the table holds singular forms.
        factor = _UNIT_TO_ML.get(unit[:-1])
    if factor is None:
        return NetContents(raw=text)

    return NetContents(ml=round(amount * factor, 3), raw=text)


def is_authorized(ml: float, commodity: Commodity) -> bool:
    """Is this an authorized container size for the commodity?"""
    match commodity:
        case Commodity.SPIRITS:
            return any(
                abs(ml - size) < 0.5 for size in canon.SPIRITS_STANDARDS_OF_FILL_ML
            )
        case Commodity.WINE:
            if any(abs(ml - size) < 0.5 for size in canon.WINE_STANDARDS_OF_FILL_ML):
                return True
            # 4 L and above: any whole liter.
            return ml >= _WINE_EVEN_LITER_FLOOR_ML and abs(ml % 1000.0) < 0.5
        case Commodity.MALT:
            # No federal standards of fill; net contents must simply be accurate.
            return True


def check_standards_of_fill(parsed: NetContents, commodity: Commodity) -> list[Finding]:
    """Compliance check, independent of whether the label matched the application (TC-10)."""
    if parsed.ml is None:
        return []
    if commodity is Commodity.MALT:
        return []
    if is_authorized(parsed.ml, commodity):
        return []

    citation = (
        canon.CITATIONS["spirits_fill"]
        if commodity is Commodity.SPIRITS
        else canon.CITATIONS["wine_fill"]
    )
    nearest = _nearest_authorized(parsed.ml, commodity)
    suffix = f" The closest authorized size is {_format_ml(nearest)}." if nearest else ""

    return [
        Finding(
            code="non_standard_fill",
            message=(
                f"{_format_ml(parsed.ml)} is not an authorized container size for "
                f"{commodity.value}.{suffix}"
            ),
            citation=citation,
            severity="finding",
        )
    ]


def _nearest_authorized(ml: float, commodity: Commodity) -> float | None:
    sizes = (
        canon.SPIRITS_STANDARDS_OF_FILL_ML
        if commodity is Commodity.SPIRITS
        else canon.WINE_STANDARDS_OF_FILL_ML
    )
    return min(sizes, key=lambda size: abs(size - ml)) if sizes else None


def _format_ml(ml: float) -> str:
    """Render a volume the way a label would state it."""
    if ml >= 1000 and abs(ml % 1000) < 0.5:
        return f"{ml / 1000:g} L"
    if ml >= 1000:
        return f"{ml / 1000:g} L"
    return f"{ml:g} mL"


def equal(left: NetContents, right: NetContents, tolerance_ml: float = 0.5) -> bool:
    """Do two net contents statements denote the same volume?

    `750 mL` and `75 cl` are the same volume in different units, so the comparison is
    numeric rather than textual.
    """
    if left.ml is None or right.ml is None:
        return False
    return abs(left.ml - right.ml) < tolerance_ml
