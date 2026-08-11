"""Per-commodity requirement matrix. TC-17 and TC-18 are the named cases.

The failure this module prevents: reporting a field as Missing when no rule required it.
A false Missing is how an agent learns to ignore the tool.
"""

import pytest

from api.models import Commodity, FieldName
from api.rules import commodity as com
from api.rules.commodity import LabelContext, Requirement

CLEAN = LabelContext()


def test_every_commodity_covers_every_field() -> None:
    """A gap in the matrix would silently resolve to a KeyError at verification time."""
    for c in Commodity:
        assert set(com.REQUIREMENTS[c]) == set(FieldName)


@pytest.mark.parametrize("c", list(Commodity))
@pytest.mark.parametrize(
    "field",
    [
        FieldName.BRAND_NAME,
        FieldName.CLASS_TYPE,
        FieldName.NET_CONTENTS,
        FieldName.PRODUCER,
        FieldName.GOVERNMENT_WARNING,
    ],
)
def test_universally_required_fields(c: Commodity, field: FieldName) -> None:
    """These five are required on every commodity, no exceptions."""
    assert com.is_required(c, field, CLEAN)


@pytest.mark.parametrize("c", list(Commodity))
def test_warning_is_required_everywhere(c: Commodity) -> None:
    """WARN-6: mandatory on all alcohol beverages. No commodity escapes it."""
    assert com.is_required(c, FieldName.GOVERNMENT_WARNING, CLEAN)


# --- country of origin ----------------------------------------------------------------

@pytest.mark.parametrize("c", list(Commodity))
def test_origin_not_required_domestically(c: Commodity) -> None:
    assert not com.is_required(c, FieldName.COUNTRY_OF_ORIGIN, LabelContext(is_import=False))


@pytest.mark.tc("TC-19")
@pytest.mark.parametrize("c", list(Commodity))
def test_origin_required_for_imports(c: Commodity) -> None:
    assert com.is_required(c, FieldName.COUNTRY_OF_ORIGIN, LabelContext(is_import=True))


# --- TC-17: table wine ----------------------------------------------------------------

@pytest.mark.tc("TC-17")
@pytest.mark.parametrize(
    "designation", ["Table Wine", "table wine", "LIGHT WINE", "Red Table Wine"]
)
def test_low_alcohol_wine_may_omit_alcohol_content(designation: str) -> None:
    ctx = LabelContext(class_type=designation, application_abv=12.5)
    assert not com.is_required(Commodity.WINE, FieldName.ALCOHOL_CONTENT, ctx)


@pytest.mark.tc("TC-17")
def test_table_wine_at_exactly_14_percent_still_qualifies() -> None:
    ctx = LabelContext(class_type="Table Wine", application_abv=14.0)
    assert not com.is_required(Commodity.WINE, FieldName.ALCOHOL_CONTENT, ctx)


@pytest.mark.tc("TC-17")
def test_table_wine_above_14_percent_must_state_alcohol_content() -> None:
    """The designation alone is not enough — the ABV condition is half the rule."""
    ctx = LabelContext(class_type="Table Wine", application_abv=15.0)
    assert com.is_required(Commodity.WINE, FieldName.ALCOHOL_CONTENT, ctx)


def test_ordinary_wine_must_state_alcohol_content() -> None:
    ctx = LabelContext(class_type="Cabernet Sauvignon", application_abv=13.0)
    assert com.is_required(Commodity.WINE, FieldName.ALCOHOL_CONTENT, ctx)


def test_table_wine_with_unknown_abv_is_treated_as_qualifying() -> None:
    """Erring the other way would produce a false Missing, which is the worse error."""
    ctx = LabelContext(class_type="Table Wine", application_abv=None)
    assert not com.is_required(Commodity.WINE, FieldName.ALCOHOL_CONTENT, ctx)


# --- TC-18: malt ----------------------------------------------------------------------

@pytest.mark.tc("TC-18")
def test_malt_alcohol_content_is_optional() -> None:
    assert not com.is_required(Commodity.MALT, FieldName.ALCOHOL_CONTENT, CLEAN)


@pytest.mark.tc("TC-18")
def test_malt_not_applicable_reason_mentions_state_law() -> None:
    """The honest caveat: federal rules do not require it, states may."""
    reason = com.not_applicable_reason(Commodity.MALT, FieldName.ALCOHOL_CONTENT, CLEAN)
    assert "state" in reason.lower()


def test_spirits_always_require_alcohol_content() -> None:
    assert com.is_required(Commodity.SPIRITS, FieldName.ALCOHOL_CONTENT, CLEAN)


# --- resolution -----------------------------------------------------------------------

def test_required_fields_are_returned_in_canonical_order() -> None:
    fields = com.required_fields(Commodity.SPIRITS, CLEAN)
    assert fields == [f for f in FieldName if f in fields]


def test_domestic_spirits_require_six_of_seven_fields() -> None:
    fields = com.required_fields(Commodity.SPIRITS, LabelContext(is_import=False))
    assert FieldName.COUNTRY_OF_ORIGIN not in fields
    assert len(fields) == 6


def test_imported_spirits_require_all_seven() -> None:
    fields = com.required_fields(Commodity.SPIRITS, LabelContext(is_import=True))
    assert len(fields) == 7


def test_every_not_applicable_reason_is_plain_language() -> None:
    """UX-6: no jargon, no enum names leaking to screen."""
    for c in Commodity:
        for field in FieldName:
            if not com.is_required(c, field, CLEAN):
                reason = com.not_applicable_reason(c, field, CLEAN)
                assert reason and reason[0].isupper() and reason.endswith(".")
                assert "_" not in reason


def test_requirement_kinds_are_exhaustively_handled() -> None:
    """is_required must resolve every Requirement variant, not fall through to None."""
    for kind in Requirement:
        assert any(
            com.requirement_for(c, f) is kind for c in Commodity for f in FieldName
        ), f"{kind} appears in no matrix cell — dead requirement kind"
