"""Per-commodity requirement matrix.

LP-041 requires this be a *data table*, not branching code. The table below is the single
place that answers "is this field required?" — adding a commodity or changing a rule is a
data edit, and the resolution logic never changes.

The distinction this module exists to protect: a field that is not required for a
commodity is **Not applicable**, never **Missing**. Reporting a malt beverage as missing
its alcohol content when no rule requires one is a false finding, and false findings are
how an agent learns to ignore the tool (TC-17, TC-18).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto

from api import canon
from api.models import Commodity, FieldName


class Requirement(StrEnum):
    """Whether a field must appear on the label."""

    REQUIRED = auto()
    OPTIONAL = auto()
    REQUIRED_IF_IMPORT = auto()
    REQUIRED_UNLESS_LOW_ALCOHOL_WINE = auto()


@dataclass(frozen=True)
class LabelContext:
    """What the resolver needs to settle a conditional requirement."""

    is_import: bool = False
    class_type: str = ""
    application_abv: float | None = None


#: The matrix. Rows are commodities, columns are fields. This is the only place a
#: requirement is stated — see PRD §Field matrix.
REQUIREMENTS: dict[Commodity, dict[FieldName, Requirement]] = {
    Commodity.SPIRITS: {
        FieldName.BRAND_NAME: Requirement.REQUIRED,
        FieldName.CLASS_TYPE: Requirement.REQUIRED,
        FieldName.ALCOHOL_CONTENT: Requirement.REQUIRED,
        FieldName.NET_CONTENTS: Requirement.REQUIRED,
        FieldName.PRODUCER: Requirement.REQUIRED,
        FieldName.COUNTRY_OF_ORIGIN: Requirement.REQUIRED_IF_IMPORT,
        FieldName.GOVERNMENT_WARNING: Requirement.REQUIRED,
    },
    Commodity.WINE: {
        FieldName.BRAND_NAME: Requirement.REQUIRED,
        FieldName.CLASS_TYPE: Requirement.REQUIRED,
        FieldName.ALCOHOL_CONTENT: Requirement.REQUIRED_UNLESS_LOW_ALCOHOL_WINE,
        FieldName.NET_CONTENTS: Requirement.REQUIRED,
        FieldName.PRODUCER: Requirement.REQUIRED,
        FieldName.COUNTRY_OF_ORIGIN: Requirement.REQUIRED_IF_IMPORT,
        FieldName.GOVERNMENT_WARNING: Requirement.REQUIRED,
    },
    Commodity.MALT: {
        FieldName.BRAND_NAME: Requirement.REQUIRED,
        FieldName.CLASS_TYPE: Requirement.REQUIRED,
        # Optional federally. State law may mandate it, which this prototype does not
        # model — so a malt label with no alcohol content is Not applicable, not Missing.
        FieldName.ALCOHOL_CONTENT: Requirement.OPTIONAL,
        FieldName.NET_CONTENTS: Requirement.REQUIRED,
        FieldName.PRODUCER: Requirement.REQUIRED,
        FieldName.COUNTRY_OF_ORIGIN: Requirement.REQUIRED_IF_IMPORT,
        FieldName.GOVERNMENT_WARNING: Requirement.REQUIRED,
    },
}

#: Wine class designations that permit omitting alcohol content at or below 14% ABV
#: (27 CFR 4.36). Matched case-insensitively against the class/type designation.
LOW_ALCOHOL_WINE_DESIGNATIONS: frozenset[str] = frozenset({"table wine", "light wine"})


def is_low_alcohol_wine(class_type: str, abv: float | None) -> bool:
    """Does this wine qualify to omit its alcohol content?

    Requires both halves: the designation *and* an ABV at or below 14%. A wine labelled
    "Table Wine" at 15% does not qualify, and neither does a 12% wine designated
    "Cabernet Sauvignon".

    When ABV is unknown, the designation alone is taken as sufficient — a producer using
    the "table wine" designation is asserting the ≤14% condition, and treating it as
    non-qualifying would produce a false Missing, which is the failure this module
    exists to prevent.
    """
    designation = class_type.strip().casefold()
    if not any(term in designation for term in LOW_ALCOHOL_WINE_DESIGNATIONS):
        return False
    return abv is None or abv <= canon.WINE_TABLE_WINE_MAX_ABV


def requirement_for(commodity: Commodity, field: FieldName) -> Requirement:
    """The raw, unresolved requirement from the matrix."""
    return REQUIREMENTS[commodity][field]


def is_required(commodity: Commodity, field: FieldName, context: LabelContext) -> bool:
    """Resolve a requirement against the label's context.

    This is the only branching in the module, and it branches on the *requirement kind*
    rather than on the commodity — so adding a commodity never touches this function.
    """
    match requirement_for(commodity, field):
        case Requirement.REQUIRED:
            return True
        case Requirement.OPTIONAL:
            return False
        case Requirement.REQUIRED_IF_IMPORT:
            return context.is_import
        case Requirement.REQUIRED_UNLESS_LOW_ALCOHOL_WINE:
            return not is_low_alcohol_wine(context.class_type, context.application_abv)


def required_fields(commodity: Commodity, context: LabelContext) -> list[FieldName]:
    """Every field the label must carry, in the canonical display order."""
    return [f for f in FieldName if is_required(commodity, f, context)]


def not_applicable_reason(
    commodity: Commodity, field: FieldName, context: LabelContext
) -> str:
    """Plain-language explanation of why a field is not applicable.

    Shown to the agent instead of a bare "Not applicable" chip, so the verdict explains
    itself rather than looking like the tool gave up (UX-6, HITL-4).
    """
    match requirement_for(commodity, field):
        case Requirement.OPTIONAL if field is FieldName.ALCOHOL_CONTENT:
            return (
                "Alcohol content is not required on malt beverage labels under federal "
                "rules. Some states require it; this tool does not check state law."
            )
        case Requirement.REQUIRED_IF_IMPORT:
            return "Country of origin is required only for imported products."
        case Requirement.REQUIRED_UNLESS_LOW_ALCOHOL_WINE:
            return (
                'Wine labelled "table wine" or "light wine" at 14% alcohol or below may '
                "omit the alcohol content."
            )
        case _:
            return "This field is not required for this product."
