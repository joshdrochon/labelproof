"""Properties of the comparators — where symmetry is required and where it is forbidden.

Comparison looks symmetric and is not. Two values both present is a symmetric question:
whichever side you call the label, `STONE'S THROW` and `Stone's Throw` are the same
brand. One value absent is an asymmetric one, and the asymmetry is the product:

* the application names a brand the label does not carry -> **Missing**, a required
  element is not on the artwork;
* the label carries a brand the application does not name -> **Mismatch**, the label
  claims something nobody filed.

Collapsing those two into one verdict loses the only information the agent needs to
know what to do next. So the properties below assert symmetry where it must hold and
assert the specific asymmetry everywhere else — a comparator that became "helpfully"
symmetric would fail here rather than quietly downgrade a finding.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from api.models import Commodity, ExtractedField, FieldName, Verdict
from api.rules import commodity as com
from api.rules import compare

pytestmark = pytest.mark.property

SETTINGS = settings(max_examples=300, deadline=None)

#: Values a text field can hold. Non-blank, because a blank reading is absence and
#: absence is the asymmetric case tested separately.
VALUES = st.text(min_size=1, max_size=30).filter(lambda s: s.strip())

TEXT_FIELDS = st.sampled_from(
    [
        FieldName.BRAND_NAME,
        FieldName.CLASS_TYPE,
        FieldName.PRODUCER,
        FieldName.COUNTRY_OF_ORIGIN,
    ]
)

COMMODITIES = st.sampled_from(list(Commodity))

#: Verdicts that mean "this field is fine". Everything else needs an agent's eyes.
PASSING = frozenset({Verdict.MATCH, Verdict.NOT_APPLICABLE})


def _read(value: str | None, *, legible: bool = True) -> ExtractedField | None:
    if value is None:
        return None
    return ExtractedField(value=value, confidence=0.95, legible=legible)


def _compare(field: FieldName, found: str | None, expected: str | None) -> object:
    return compare.compare_text(field, _read(found), expected, required=True, label="value")


# --------------------------------------------------------------------------------------
# Symmetry, where it must hold
# --------------------------------------------------------------------------------------


@SETTINGS
@given(TEXT_FIELDS, VALUES, VALUES)
def test_comparison_is_symmetric_when_both_sides_are_present(
    field: FieldName, left: str, right: str
) -> None:
    """With a value on each side, swapping them cannot change the verdict.

    Callers pass the label first by convention, and nothing in the signature says the
    order matters. If it did, a refactor that swapped the arguments would silently move
    verdicts.
    """
    assert _compare(field, left, right).verdict is _compare(field, right, left).verdict  # type: ignore[attr-defined]


@SETTINGS
@given(TEXT_FIELDS, VALUES)
def test_comparison_is_reflexive(field: FieldName, value: str) -> None:
    """A value always matches itself, exactly, with no variation note."""
    result = _compare(field, value, value)
    assert result.verdict is Verdict.MATCH  # type: ignore[attr-defined]
    assert result.tier == 1  # type: ignore[attr-defined]


@SETTINGS
@given(TEXT_FIELDS, VALUES)
def test_a_match_never_carries_a_variation_note(field: FieldName, value: str) -> None:
    """Match means identical. Anything folded is Acceptable variation (MATCH-9)."""
    result = _compare(field, value, value)
    assert result.rationale == "The label matches the application."  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# Asymmetry, where it is the product
# --------------------------------------------------------------------------------------


@SETTINGS
@given(TEXT_FIELDS, VALUES)
def test_absent_from_the_label_is_missing_and_absent_from_the_application_is_mismatch(
    field: FieldName, value: str
) -> None:
    """The deliberate asymmetry, stated as a property.

    Missing tells the agent to ask the applicant for corrected artwork. Mismatch tells
    them the artwork claims something nobody filed. Same pair of values, opposite
    directions, and the two next steps are different.
    """
    assert _compare(field, None, value).verdict is Verdict.MISSING  # type: ignore[attr-defined]
    assert _compare(field, value, None).verdict is Verdict.MISMATCH  # type: ignore[attr-defined]


@SETTINGS
@given(TEXT_FIELDS, VALUES)
def test_an_unreadable_field_outranks_absence(field: FieldName, value: str) -> None:
    """"We could not read it" is reported before "it is not there".

    Reporting Missing on a field that is printed but illegible is a false finding on a
    compliant label, and it sends the agent to the applicant instead of to a better
    photograph.
    """
    illegible = compare.compare_text(
        field, _read(value, legible=False), value, required=True, label="value"
    )
    assert illegible.verdict is Verdict.UNREADABLE
    assert illegible.extracted is None
    assert illegible.confidence == 0.0


@SETTINGS
@given(TEXT_FIELDS, VALUES)
def test_a_field_that_is_not_required_is_not_applicable_rather_than_missing(
    field: FieldName, value: str
) -> None:
    """TC-17, TC-18, TC-19: absence of an optional element is not a defect.

    Reporting a domestic label as missing its country of origin is a false finding
    against a rule that does not apply to it.
    """
    result = compare.compare_text(
        field, None, value, required=False, not_applicable_reason="Not required."
    )
    assert result.verdict is Verdict.NOT_APPLICABLE


# --------------------------------------------------------------------------------------
# The asymmetry law: flag, never pass
# --------------------------------------------------------------------------------------


@SETTINGS
@given(TEXT_FIELDS, VALUES, VALUES)
def test_a_pass_requires_the_normalised_values_to_agree(
    field: FieldName, left: str, right: str
) -> None:
    """Nothing reaches a passing verdict unless Tier 1 says the values are the same.

    This is the asymmetry law at the field level: a gray case falls through to
    Mismatch, which costs an agent thirty seconds. The opposite direction costs a
    non-compliant label an approval.
    """
    from api.rules.normalize import equal_after_normalization

    result = _compare(field, left, right)
    if result.verdict in PASSING or result.verdict is Verdict.ACCEPTABLE_VARIATION:  # type: ignore[attr-defined]
        assert equal_after_normalization(left, right)


@SETTINGS
@given(TEXT_FIELDS, VALUES, VALUES)
def test_a_mismatch_always_shows_the_agent_both_values(
    field: FieldName, left: str, right: str
) -> None:
    """A verdict the agent cannot check is a verdict they have to take on trust.

    HITL-4: every mismatch rationale quotes what the label reads and what the
    application states, so the override decision needs nothing but the screen.
    """
    result = _compare(field, left, right)
    assume(result.verdict is Verdict.MISMATCH)  # type: ignore[attr-defined]
    assert left in result.rationale  # type: ignore[attr-defined]
    assert right in result.rationale  # type: ignore[attr-defined]


#: Brand-name-shaped text, plus the transformations Tier 1 folds away. Built rather
#: than filtered: two random strings are never an acceptable variation of each other,
#: so filtering for one throws away every example.
_BRANDISH = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ'",
    min_size=2,
    max_size=20,
).filter(lambda s: s.strip() and s.strip("'"))

_FOLDABLE = st.sampled_from(
    [
        lambda s: s.upper(),
        lambda s: s.lower(),
        # U+2019 is the STONE'S THROW character specifically; the confusable
        # warning is what this transform exists to exercise.
        lambda s: s.replace("'", "’"),  # noqa: RUF001
        lambda s: f"{s}.",
        lambda s: s.replace(" ", "  "),
    ]
)


@SETTINGS
@given(TEXT_FIELDS, _BRANDISH, _FOLDABLE)
def test_an_acceptable_variation_always_explains_itself(
    field: FieldName, base: str, fold: object
) -> None:
    """Tier 2 never returns a bare chip. The agent sees the judgment call (MATCH-9).

    Dave's case is the specification: `STONE'S THROW` against `Stone's Throw` is
    "technically a mismatch, but obviously the same thing", and the agent must be shown
    that the tool made that call rather than being handed a silent pass.
    """
    variant = fold(base)  # type: ignore[operator]
    assume(variant != base)
    result = _compare(field, variant, base)
    assert result.verdict is Verdict.ACCEPTABLE_VARIATION, (  # type: ignore[attr-defined]
        f"{variant!r} vs {base!r} -> {result.verdict}"  # type: ignore[attr-defined]
    )
    assert result.tier == 2  # type: ignore[attr-defined]
    assert result.rationale.strip()  # type: ignore[attr-defined]
    assert result.rationale != "The label matches the application."  # type: ignore[attr-defined]


# --------------------------------------------------------------------------------------
# Alcohol content
# --------------------------------------------------------------------------------------

_ABV = st.floats(min_value=0.1, max_value=95.0, allow_nan=False).map(lambda v: round(v, 1))
_CONTEXT = com.LabelContext(is_import=False, class_type="Bourbon", application_abv=45.0)


@SETTINGS
@given(_ABV)
def test_alcohol_content_matches_itself_at_every_strength(value: float) -> None:
    result = compare.compare_alcohol_content(
        _read(f"{value:g}% Alc./Vol."), value, Commodity.SPIRITS, _CONTEXT
    )
    assert result.verdict is Verdict.MATCH


@pytest.mark.tc("TC-08")
@SETTINGS
@given(_ABV, st.floats(min_value=0.1, max_value=40.0).map(lambda v: round(v, 1)))
def test_any_alcohol_difference_above_the_reading_tolerance_is_a_mismatch(
    value: float, delta: float
) -> None:
    """MATCH-8: a regulatory tolerance never excuses a label/application difference.

    The tolerance governs the liquid against the label, and this tool cannot measure
    liquid. Spirits tolerance is 0.3 points; a 0.4-point difference here is still a
    Mismatch, and the tolerance appears only as context in the rationale.
    """
    label = value + delta
    assume(label <= 99.0)
    result = compare.compare_alcohol_content(
        _read(f"{label:g}% Alc./Vol."), value, Commodity.SPIRITS, _CONTEXT
    )
    assert result.verdict is Verdict.MISMATCH
    assert "does not excuse" in result.rationale


@SETTINGS
@given(_ABV)
def test_a_label_stating_alcohol_the_application_omits_is_surfaced(value: float) -> None:
    """Surfaced, never passed. The applicant filed one thing and printed another."""
    result = compare.compare_alcohol_content(
        _read(f"{value:g}% Alc./Vol."), None, Commodity.SPIRITS, _CONTEXT
    )
    assert result.verdict is Verdict.MISMATCH


@pytest.mark.tc("TC-18")
@SETTINGS
@given(_ABV)
def test_malt_without_an_alcohol_statement_is_not_applicable(value: float) -> None:
    """No federal rule requires it, so absence is not a defect (TC-18)."""
    result = compare.compare_alcohol_content(
        None, None, Commodity.MALT, com.LabelContext(class_type="India Pale Ale")
    )
    assert result.verdict is Verdict.NOT_APPLICABLE


@pytest.mark.tc("TC-17")
def test_low_alcohol_table_wine_without_an_alcohol_statement_is_not_applicable() -> None:
    """27 CFR 4.36 lets table wine at or below 14% omit it (TC-17)."""
    result = compare.compare_alcohol_content(
        None,
        None,
        Commodity.WINE,
        com.LabelContext(class_type="Table Wine", application_abv=12.0),
    )
    assert result.verdict is Verdict.NOT_APPLICABLE


@SETTINGS
@given(_ABV)
def test_an_illegible_alcohol_statement_is_unreadable_not_missing(value: float) -> None:
    result = compare.compare_alcohol_content(
        _read(f"{value:g}%", legible=False), value, Commodity.SPIRITS, _CONTEXT
    )
    assert result.verdict is Verdict.UNREADABLE


# --------------------------------------------------------------------------------------
# Net contents
# --------------------------------------------------------------------------------------

_SIZES = st.sampled_from([187, 200, 375, 500, 700, 750, 1000, 1750])


@SETTINGS
@given(_SIZES)
def test_net_contents_matches_across_a_unit_change(millilitres: int) -> None:
    """The application in millilitres, the label in centilitres: one volume."""
    result = compare.compare_net_contents(
        _read(f"{millilitres / 10:g} cl"), f"{millilitres} mL", Commodity.SPIRITS
    )
    assert result.verdict is Verdict.MATCH


@SETTINGS
@given(_SIZES, _SIZES)
def test_net_contents_comparison_is_symmetric(left: int, right: int) -> None:
    forward = compare.compare_net_contents(_read(f"{left} mL"), f"{right} mL", Commodity.SPIRITS)
    reverse = compare.compare_net_contents(_read(f"{right} mL"), f"{left} mL", Commodity.SPIRITS)
    assert forward.verdict is reverse.verdict


@SETTINGS
@given(_SIZES)
def test_an_unparseable_net_contents_reading_is_never_a_match(millilitres: int) -> None:
    """A statement we could not turn into a volume cannot be said to agree.

    Falling through to Match here would pass a label on the strength of text nobody
    understood.
    """
    result = compare.compare_net_contents(
        _read("contents as shown"), f"{millilitres} mL", Commodity.SPIRITS
    )
    assert result.verdict is not Verdict.MATCH


@SETTINGS
@given(_SIZES)
def test_an_illegible_net_contents_reading_is_unreadable(millilitres: int) -> None:
    result = compare.compare_net_contents(
        _read(f"{millilitres} mL", legible=False), f"{millilitres} mL", Commodity.SPIRITS
    )
    assert result.verdict is Verdict.UNREADABLE


@SETTINGS
@given(_SIZES)
def test_a_legible_but_empty_net_contents_reading_is_missing(millilitres: int) -> None:
    """The extractor looked, found nothing, and said so — that is Missing, not Unreadable.

    An `ExtractedField` with `legible=True` and no value is the shape the adapter
    produces for a field it searched for and did not find. Net contents is required on
    every commodity, so absence is a defect on the label rather than on the photograph.
    """
    for reading in (_read(""), None):
        result = compare.compare_net_contents(
            reading, f"{millilitres} mL", Commodity.SPIRITS
        )
        assert result.verdict is Verdict.MISSING


# --------------------------------------------------------------------------------------
# Producer address tolerance
# --------------------------------------------------------------------------------------

_STATES = st.sampled_from(
    [("KY", "Kentucky"), ("CA", "California"), ("NY", "New York"), ("TX", "Texas")]
)


@SETTINGS
@given(
    _STATES,
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=3, max_size=15).filter(str.strip),
)
def test_a_state_abbreviation_and_its_full_name_are_the_same_address(
    state: tuple[str, str], town: str
) -> None:
    """`Frankfort, KY` and `Frankfort, Kentucky` are one address written two ways.

    Reporting them as a Mismatch would be a false finding on essentially every label
    whose application was typed by a different person than the artwork.
    """
    abbreviation, full = state
    result = compare.compare_producer(
        _read(f"Old Tom Distillery, {town}, {abbreviation}"),
        "Old Tom Distillery",
        f"{town}, {full}",
    )
    assert result.verdict in {Verdict.MATCH, Verdict.ACCEPTABLE_VARIATION}
