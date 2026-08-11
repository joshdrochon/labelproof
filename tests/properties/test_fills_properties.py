"""Properties of net-contents parsing and standards of fill.

The trap this module exists to avoid is stated in its own docstring: comparison and
compliance are independent. `733 mL` against an application reading `733 mL` *matches*
and is still non-compliant. Every property below fixes one of those two and varies the
other, so a change that collapses them fails a named test rather than quietly turning
a finding into a verdict.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api import canon
from api.models import Commodity
from api.rules import fills

pytestmark = pytest.mark.property

SETTINGS = settings(max_examples=300, deadline=None)

COMMODITIES = st.sampled_from(list(Commodity))

SPIRITS_SIZES = st.sampled_from(sorted(canon.SPIRITS_STANDARDS_OF_FILL_ML))
WINE_SIZES = st.sampled_from(sorted(canon.WINE_STANDARDS_OF_FILL_ML))

#: Every spelling of every unit the table claims to accept, with its factor. Written
#: out rather than derived from `_UNIT_TO_ML` on purpose: deriving the expectation from
#: the implementation would make the test agree with whatever the implementation does,
#: including a missing plural.
UNIT_SPELLINGS: list[tuple[str, float]] = [
    ("mL", 1.0), ("ml", 1.0), ("ML", 1.0), ("milliliter", 1.0),
    ("milliliters", 1.0), ("millilitre", 1.0), ("millilitres", 1.0),
    ("cl", 10.0), ("cL", 10.0), ("centiliter", 10.0), ("centiliters", 10.0),
    ("centilitre", 10.0), ("centilitres", 10.0),
    ("dl", 100.0), ("deciliter", 100.0), ("deciliters", 100.0),
    ("decilitre", 100.0), ("decilitres", 100.0),
    ("L", 1000.0), ("l", 1000.0), ("liter", 1000.0), ("liters", 1000.0),
    ("litre", 1000.0), ("litres", 1000.0),
    ("fl oz", 29.5735295625), ("fl. oz.", 29.5735295625),
    ("FL. OZ.", 29.5735295625), ("fluid ounce", 29.5735295625),
    ("fluid ounces", 29.5735295625), ("oz", 29.5735295625),
]


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("unit", "factor"), UNIT_SPELLINGS, ids=[u for u, _ in UNIT_SPELLINGS])
@settings(max_examples=60, deadline=None)
@given(st.integers(min_value=1, max_value=5000))
def test_every_documented_unit_spelling_parses(
    unit: str, factor: float, amount: int
) -> None:
    """Singular, plural, British, American, spaced, abbreviated — all the same volume.

    The plural forms are the reason this is a table rather than a handful of examples:
    a label reading `1.5 liters` and one reading `1.5 liter` state the same contents,
    and an earlier version of the unit table held only singular keys.
    """
    parsed = fills.parse(f"{amount} {unit}")
    assert parsed.ml is not None, f"{amount} {unit} did not parse"
    assert parsed.ml == pytest.approx(amount * factor, abs=0.01)


@SETTINGS
@given(st.integers(min_value=1, max_value=9999))
def test_the_european_decimal_comma_is_read_as_a_decimal_point(amount: int) -> None:
    """`1,75 L` is 1.75 litres, not 175. Imported labels write it this way."""
    assert fills.parse(f"{amount},5 L").ml == pytest.approx(amount * 1000 + 500)


@SETTINGS
@given(st.integers(min_value=1, max_value=5000))
def test_a_missing_space_does_not_change_the_volume(amount: int) -> None:
    assert fills.parse(f"{amount}ML").ml == fills.parse(f"{amount} mL").ml


@SETTINGS
@given(st.text(max_size=30).filter(lambda s: not any(c.isdigit() for c in s)))
def test_text_with_no_quantity_is_unreadable_rather_than_zero(text: str) -> None:
    parsed = fills.parse(text)
    assert parsed.ml is None
    assert not parsed.is_readable


@SETTINGS
@given(
    st.integers(min_value=1, max_value=500),
    st.sampled_from(["gallons", "furlongs", "kg", "grams"]),
)
def test_an_unrecognised_unit_is_unreadable_rather_than_guessed(
    amount: int, unit: str
) -> None:
    """A unit we do not model is "we could not read it", never a number.

    Guessing a factor here would put a fabricated volume in front of an agent, and
    nothing downstream can tell a guess from a reading.
    """
    assert fills.parse(f"{amount} {unit}").ml is None


# --------------------------------------------------------------------------------------
# Comparison — numeric, symmetric, unit-blind
# --------------------------------------------------------------------------------------


@SETTINGS
@given(st.integers(min_value=1, max_value=4000))
def test_the_same_volume_in_different_units_compares_equal(millilitres: int) -> None:
    """`750 mL` and `75 cl` are one volume written two ways.

    The comparison is numeric rather than textual precisely so an application filed in
    millilitres and a label printed in centilitres do not reach an agent as a Mismatch.
    """
    assert fills.equal(fills.parse(f"{millilitres} mL"), fills.parse(f"{millilitres / 10:g} cl"))


@SETTINGS
@given(
    st.floats(min_value=1, max_value=5000, allow_nan=False),
    st.floats(min_value=1, max_value=5000, allow_nan=False),
)
def test_volume_comparison_is_symmetric(left: float, right: float) -> None:
    """Which side is the label cannot change whether the volumes agree."""
    a, b = fills.NetContents(ml=left), fills.NetContents(ml=right)
    assert fills.equal(a, b) == fills.equal(b, a)


@SETTINGS
@given(st.floats(min_value=1, max_value=5000, allow_nan=False))
def test_volume_comparison_is_reflexive(value: float) -> None:
    assert fills.equal(fills.NetContents(ml=value), fills.NetContents(ml=value))


@SETTINGS
@given(st.floats(min_value=1, max_value=5000, allow_nan=False))
def test_an_unreadable_side_never_compares_equal(value: float) -> None:
    """Unreadable is not a match. Fail closed on both sides.

    If an unread volume compared equal to anything, an illegible net-contents
    statement would pass as agreeing with the application — a false pass produced by
    the absence of evidence.
    """
    unreadable = fills.NetContents(ml=None, raw="???")
    assert not fills.equal(unreadable, fills.NetContents(ml=value))
    assert not fills.equal(fills.NetContents(ml=value), unreadable)
    assert not fills.equal(unreadable, unreadable)


# --------------------------------------------------------------------------------------
# Compliance — independent of the comparison (TC-10)
# --------------------------------------------------------------------------------------


@SETTINGS
@given(SPIRITS_SIZES)
def test_every_authorised_spirits_size_passes_compliance(size: float) -> None:
    """All twenty-five sizes in 27 CFR 5.203, not the four somebody thought of."""
    assert fills.is_authorized(size, Commodity.SPIRITS)
    assert fills.check_standards_of_fill(fills.NetContents(ml=size), Commodity.SPIRITS) == []


@SETTINGS
@given(WINE_SIZES)
def test_every_authorised_wine_size_passes_compliance(size: float) -> None:
    assert fills.is_authorized(size, Commodity.WINE)
    assert fills.check_standards_of_fill(fills.NetContents(ml=size), Commodity.WINE) == []


@SETTINGS
@given(st.integers(min_value=4, max_value=40))
def test_wine_at_or_above_four_litres_may_be_any_whole_litre(litres: int) -> None:
    """27 CFR 4.72's open band: `(4 liters, 5 liters, 6 liters, etc.)`."""
    assert fills.is_authorized(litres * 1000.0, Commodity.WINE)


@SETTINGS
@given(st.integers(min_value=4, max_value=40))
def test_wine_between_whole_litres_above_four_is_still_non_standard(litres: int) -> None:
    """The band is whole litres, not "anything above four litres"."""
    assert not fills.is_authorized(litres * 1000.0 + 250.0, Commodity.WINE)


@pytest.mark.tc("TC-10")
@SETTINGS
@given(st.integers(min_value=1, max_value=3999))
def test_an_unauthorised_size_always_produces_a_cited_finding(millilitres: int) -> None:
    """Every non-standard spirits volume is reported, with the regulation and a nearest size.

    "It is not on the list" is not an answer an agent can act on. The finding names the
    closest authorized size so the note back to the applicant writes itself.
    """
    if fills.is_authorized(float(millilitres), Commodity.SPIRITS):
        return
    findings = fills.check_standards_of_fill(
        fills.NetContents(ml=float(millilitres)), Commodity.SPIRITS
    )
    assert [f.code for f in findings] == ["non_standard_fill"]
    assert findings[0].citation == canon.CITATIONS["spirits_fill"]
    assert "closest authorized size" in findings[0].message


@SETTINGS
@given(st.integers(min_value=1, max_value=100000))
def test_malt_never_raises_a_standards_of_fill_finding(millilitres: int) -> None:
    """Malt beverages have no federal standards of fill (27 CFR 7).

    Every volume is authorized, so raising a finding would be a false finding against a
    regulation that does not exist.
    """
    assert fills.is_authorized(float(millilitres), Commodity.MALT)
    volume = fills.NetContents(ml=float(millilitres))
    assert fills.check_standards_of_fill(volume, Commodity.MALT) == []


@SETTINGS
@given(COMMODITIES)
def test_an_unreadable_volume_produces_no_compliance_finding(
    commodity: Commodity,
) -> None:
    """We did not read it, so we did not check it — and must not claim we did.

    A `non_standard_fill` finding on a volume nobody could read is a determination we
    never made.
    """
    assert fills.check_standards_of_fill(fills.NetContents(ml=None, raw="???"), commodity) == []


# --------------------------------------------------------------------------------------
# The two checks stay independent (TC-10 is the whole point)
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-10")
@SETTINGS
@given(st.integers(min_value=1, max_value=3999))
def test_matching_the_application_never_suppresses_the_compliance_finding(
    millilitres: int,
) -> None:
    """A label that agrees with its application can still be non-compliant.

    This is TC-10 as a property rather than one fixture: for *every* volume, the
    comparison result and the compliance result are computed from different inputs and
    neither can silence the other.
    """
    volume = fills.NetContents(ml=float(millilitres))
    assert fills.equal(volume, volume)
    authorized = fills.is_authorized(float(millilitres), Commodity.SPIRITS)
    expected_findings = [] if authorized else ["non_standard_fill"]
    assert [
        f.code for f in fills.check_standards_of_fill(volume, Commodity.SPIRITS)
    ] == expected_findings


@SETTINGS
@given(SPIRITS_SIZES, SPIRITS_SIZES)
def test_disagreeing_with_the_application_never_creates_a_compliance_finding(
    label: float, application: float
) -> None:
    """Compliance is judged on what the LABEL says, whatever the application says.

    Both sides here are authorized sizes. Whether they match is the verdict; neither
    answer may invent a standards-of-fill finding.
    """
    assert fills.check_standards_of_fill(fills.NetContents(ml=label), Commodity.SPIRITS) == []
    assert fills.equal(fills.NetContents(ml=label), fills.NetContents(ml=application)) == (
        label == application
    )


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


@SETTINGS
@given(st.one_of(SPIRITS_SIZES, WINE_SIZES))
def test_every_authorised_size_survives_a_render_and_reparse(size: float) -> None:
    """The volume we print back to the agent parses to the volume we measured.

    The finding message renders the nearest authorized size. If that rendering did not
    round-trip, the size in the advice would not be a size the tool would accept.
    """
    reparsed = fills.parse(fills._format_ml(size))
    assert reparsed.ml == pytest.approx(size, abs=0.5)
