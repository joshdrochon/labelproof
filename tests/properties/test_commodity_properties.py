"""Properties of the per-commodity requirement matrix.

LP-041 requires this be a data table rather than branching code, and the reason is the
distinction the module exists to protect: **a field that is not required for a commodity
is Not applicable, never Missing.** Reporting a malt beverage as missing its alcohol
content when no rule requires one is a false finding, and false findings are how an
agent learns to ignore the tool (TC-17, TC-18).

The properties below hold the table and the resolver to that. They are written against
`FieldName` and `Commodity` rather than against a list of the fields that exist today,
so adding an eighth element or a fourth commodity fails here until somebody decides what
its requirement is — which is exactly the decision a data table is supposed to force.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api import canon
from api.models import Commodity, FieldName
from api.rules import commodity as com

pytestmark = pytest.mark.property

SETTINGS = settings(max_examples=200, deadline=None)

COMMODITIES = st.sampled_from(list(Commodity))
FIELDS = st.sampled_from(list(FieldName))
CONTEXTS = st.builds(
    com.LabelContext,
    is_import=st.booleans(),
    class_type=st.sampled_from(
        ["Bourbon", "Table Wine", "Light Wine", "Cabernet Sauvignon", "India Pale Ale", ""]
    ),
    application_abv=st.one_of(st.none(), st.floats(min_value=0.0, max_value=60.0)),
)


# --------------------------------------------------------------------------------------
# The table is complete
# --------------------------------------------------------------------------------------


@SETTINGS
@given(COMMODITIES, FIELDS)
def test_every_commodity_declares_a_requirement_for_every_field(
    commodity: Commodity, field: FieldName
) -> None:
    """No cell is left blank.

    A missing cell would be a `KeyError` in the middle of a verification — a 500 on a
    label whose only sin was being a commodity somebody added without finishing the
    table.
    """
    assert isinstance(com.requirement_for(commodity, field), com.Requirement)


@SETTINGS
@given(COMMODITIES, FIELDS, CONTEXTS)
def test_every_requirement_resolves_to_a_yes_or_no(
    commodity: Commodity, field: FieldName, context: com.LabelContext
) -> None:
    """Resolution is total: every requirement kind has a branch.

    `is_required` matches on the requirement *kind* rather than the commodity, so
    adding a commodity never touches it — but adding a requirement kind does, and an
    unhandled kind would fall off the end of the match and return `None`, which is
    falsy and would silently make a required field optional.
    """
    assert isinstance(com.is_required(commodity, field, context), bool)


@SETTINGS
@given(COMMODITIES, FIELDS, CONTEXTS)
def test_every_field_has_an_agent_facing_reason_for_not_applying(
    commodity: Commodity, field: FieldName, context: com.LabelContext
) -> None:
    """"Not applicable" on its own looks like the tool gave up (UX-6, HITL-4).

    Every field must be able to explain itself, including the ones that are always
    required and would only reach this path through a caller's mistake — a bare chip
    with no sentence is the worst version of that mistake.
    """
    reason = com.not_applicable_reason(commodity, field, context)
    assert reason.strip()
    assert reason.endswith(".")


# --------------------------------------------------------------------------------------
# The warning is required everywhere
# --------------------------------------------------------------------------------------


@SETTINGS
@given(COMMODITIES, CONTEXTS)
def test_the_government_warning_is_required_for_every_commodity_and_context(
    commodity: Commodity, context: com.LabelContext
) -> None:
    """27 CFR 16.21 admits no exceptions, and neither may the table.

    Any context that made the warning optional would let it resolve to Not applicable —
    which `aggregate.recommend` currently treats as clean. The two defects compound, so
    this is asserted here as well as in tests/regression/test_aggregate_warning_holes.py.
    """
    assert com.is_required(commodity, FieldName.GOVERNMENT_WARNING, context)


@SETTINGS
@given(COMMODITIES, CONTEXTS)
def test_the_core_identity_fields_are_required_for_every_commodity(
    commodity: Commodity, context: com.LabelContext
) -> None:
    """Brand, class/type, net contents and producer are required on every label."""
    for field in (
        FieldName.BRAND_NAME,
        FieldName.CLASS_TYPE,
        FieldName.NET_CONTENTS,
        FieldName.PRODUCER,
    ):
        assert com.is_required(commodity, field, context)


# --------------------------------------------------------------------------------------
# The conditional requirements resolve on their own terms
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-19")
@SETTINGS
@given(COMMODITIES)
def test_country_of_origin_is_required_only_for_imports(commodity: Commodity) -> None:
    """A domestic label with no country of origin is compliant, not defective."""
    imported = com.LabelContext(is_import=True)
    domestic = com.LabelContext(is_import=False)
    assert com.is_required(commodity, FieldName.COUNTRY_OF_ORIGIN, imported)
    assert not com.is_required(commodity, FieldName.COUNTRY_OF_ORIGIN, domestic)


@pytest.mark.tc("TC-18")
@SETTINGS
@given(CONTEXTS)
def test_malt_never_requires_an_alcohol_statement(context: com.LabelContext) -> None:
    """Optional federally, whatever the class designation or the filed ABV.

    Some states require it; this prototype does not model state law and says so in the
    Not-applicable reason rather than inventing a federal rule.
    """
    assert not com.is_required(Commodity.MALT, FieldName.ALCOHOL_CONTENT, context)
    reason = com.not_applicable_reason(
        Commodity.MALT, FieldName.ALCOHOL_CONTENT, context
    )
    assert "states" in reason


@pytest.mark.tc("TC-17")
@SETTINGS
@given(
    st.sampled_from(["Table Wine", "table wine", "LIGHT WINE", "California Table Wine"]),
    st.floats(min_value=0.0, max_value=canon.WINE_TABLE_WINE_MAX_ABV),
)
def test_low_alcohol_table_wine_may_omit_its_alcohol_content(
    designation: str, abv: float
) -> None:
    """27 CFR 4.36, matched case-insensitively and as a substring.

    Producers write "California Table Wine", not "Table Wine". An exact-match rule
    would report a compliant label as missing a required element.
    """
    context = com.LabelContext(class_type=designation, application_abv=abv)
    assert not com.is_required(Commodity.WINE, FieldName.ALCOHOL_CONTENT, context)


@SETTINGS
@given(st.floats(min_value=14.01, max_value=60.0))
def test_a_table_wine_above_fourteen_percent_must_still_state_its_alcohol(
    abv: float,
) -> None:
    """Both halves of the exemption are required: the designation *and* the strength."""
    context = com.LabelContext(class_type="Table Wine", application_abv=abv)
    assert com.is_required(Commodity.WINE, FieldName.ALCOHOL_CONTENT, context)


@SETTINGS
@given(
    st.sampled_from(["Cabernet Sauvignon", "Chardonnay", "Sparkling Wine", ""]),
    st.floats(min_value=0.0, max_value=14.0),
)
def test_a_low_strength_wine_without_the_designation_must_state_its_alcohol(
    designation: str, abv: float
) -> None:
    """A 12% Cabernet does not qualify; the exemption is tied to the designation."""
    context = com.LabelContext(class_type=designation, application_abv=abv)
    assert com.is_required(Commodity.WINE, FieldName.ALCOHOL_CONTENT, context)


@SETTINGS
@given(st.sampled_from(["Table Wine", "Light Wine"]))
def test_an_unknown_strength_does_not_defeat_the_designation(designation: str) -> None:
    """When the ABV is unknown the designation alone is taken as sufficient.

    A producer using the "table wine" designation is asserting the ≤14% condition.
    Treating an unknown strength as non-qualifying would produce a false Missing, which
    is the failure this module exists to prevent — so the fail-closed direction here
    points the other way from usual, and deliberately.
    """
    context = com.LabelContext(class_type=designation, application_abv=None)
    assert not com.is_required(Commodity.WINE, FieldName.ALCOHOL_CONTENT, context)


# --------------------------------------------------------------------------------------
# Required fields render in a stable order
# --------------------------------------------------------------------------------------


@SETTINGS
@given(COMMODITIES, CONTEXTS)
def test_required_fields_are_returned_in_canonical_order_without_duplicates(
    commodity: Commodity, context: com.LabelContext
) -> None:
    """The checklist an agent reads is ordered the same way every time."""
    required = com.required_fields(commodity, context)
    assert required == [f for f in FieldName if f in required]
    assert len(required) == len(set(required))


@SETTINGS
@given(COMMODITIES, CONTEXTS)
def test_required_fields_agrees_with_the_resolver(
    commodity: Commodity, context: com.LabelContext
) -> None:
    """One source of truth: the list is derived from the same answer, not a second copy."""
    required = set(com.required_fields(commodity, context))
    assert required == {f for f in FieldName if com.is_required(commodity, f, context)}
