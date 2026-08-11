"""DEFECT: `1.75 liters` did not parse, because the unit table held only singulars.

**What happened.** `_UNIT_TO_ML` maps unit spellings to millilitres and was written in
the singular: `liter`, `milliliter`, `fluid ounce`. A label reading `1.75 liters` — the
ordinary way an American label writes it — matched the quantity pattern, looked up
`liters`, found nothing, and returned `ml=None`.

**What that produced.** `compare_net_contents` reports an unparseable reading as
**Missing**: "The application states 1.75 L, but none appears on the label." On a label
that states its net contents perfectly clearly, in plural, on the front. A false finding
on a compliant label — which is how an agent learns that the tool is wrong often enough
to skim past.

**The fix.** On a miss, retry the lookup with a trailing `s` stripped.

The regression is pinned as a table of *every* spelling the module claims to accept, not
as the one example that broke. The bug was a missing row; the test that prevents it is a
complete row list.
"""

from __future__ import annotations

import pytest

from api.models import Commodity, ExtractedField, Verdict
from api.rules import compare as C
from api.rules import fills as F

pytestmark = pytest.mark.regression

#: `(singular, plural)` for every unit the table spells out in words. Both forms must
#: denote the same volume — that is the whole of the defect.
SINGULAR_AND_PLURAL = [
    ("milliliter", "milliliters"),
    ("millilitre", "millilitres"),
    ("centiliter", "centiliters"),
    ("centilitre", "centilitres"),
    ("deciliter", "deciliters"),
    ("decilitre", "decilitres"),
    ("liter", "liters"),
    ("litre", "litres"),
    ("fluid ounce", "fluid ounces"),
]


@pytest.mark.parametrize(("singular", "plural"), SINGULAR_AND_PLURAL, ids=lambda p: p)
def test_the_plural_of_every_unit_parses_to_the_same_volume(
    singular: str, plural: str
) -> None:
    """The regression itself, across every unit rather than the one that was reported."""
    one = F.parse(f"1.75 {singular}")
    many = F.parse(f"1.75 {plural}")
    assert one.ml is not None, f"{singular} did not parse"
    assert many.ml is not None, f"{plural} did not parse"
    assert one.ml == many.ml


@pytest.mark.parametrize(("singular", "plural"), SINGULAR_AND_PLURAL, ids=lambda p: p)
def test_a_plural_unit_is_never_reported_as_a_missing_net_contents(
    singular: str, plural: str
) -> None:
    """The consequence, pinned at the layer an agent actually sees.

    Parsing is internal; the false Missing verdict is what reached the screen. Pinning
    only the parser would let a future rewrite move the failure one layer up and stay
    green.
    """
    result = C.compare_net_contents(
        ExtractedField(value=f"1.75 {plural}", confidence=0.95),
        f"1.75 {singular}",
        Commodity.SPIRITS,
    )
    assert result.verdict is Verdict.MATCH


def test_the_reported_case_parses() -> None:
    """`1.75 liters` — the exact string from the report."""
    assert F.parse("1.75 liters").ml == pytest.approx(1750.0)


@pytest.mark.parametrize(
    "text",
    ["750 mL", "75 cl", "1.75 L", "25.4 fl oz", "12 FL. OZ.", "1,75 L", "750ML"],
)
def test_the_abbreviated_spellings_still_parse(text: str) -> None:
    """Adding plural handling must not disturb the abbreviations.

    The fix strips a trailing `s` on a miss. `oz` and `ml` end in no `s`, but a
    careless implementation could strip one from something that needed it.
    """
    assert F.parse(text).ml is not None


def test_stripping_a_trailing_s_does_not_invent_units() -> None:
    """The retry is a fallback, not a wildcard.

    Stripping `s` from `gallons` gives `gallon`, which is still not in the table. If it
    started resolving, the parser would be guessing at units it does not model — and a
    guessed volume is worse than an unreadable one.
    """
    assert F.parse("5 gallons").ml is None
    assert F.parse("5 gallon").ml is None
    assert F.parse("500 grams").ml is None
