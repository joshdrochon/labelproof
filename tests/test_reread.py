"""The second look (LP-325).

Every test here is a claim about what the re-reader is NOT allowed to do. That is the
shape the module was written in: it can return a strictly better reading or the original
untouched, and there is no third outcome. A pass that improves accuracy while being able
to make a row worse would be a bad trade — the whole product is built on the asymmetry
that a false flag costs seconds and a false pass costs a compliance failure.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from api import reread
from api.models import BoundingBox, Commodity, ExtractedField, Extraction, FieldName
from api.provider.base import (
    ExtractionRequest,
    ExtractionResponse,
    ImageInput,
    ProviderError,
    ProviderUsage,
)

BOX = BoundingBox(x0=0.4, y0=0.4, x1=0.6, y1=0.5)


def _png(width: int = 800, height: int = 600) -> bytes:
    """A real image, because the crop path opens and re-encodes it."""
    image = Image.new("RGB", (width, height), (240, 240, 235))
    for x in range(0, width, 7):  # some structure, so the crop is not a flat field
        for y in range(0, height, 11):
            image.putpixel((x, y), (20, 20, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _extraction(name: FieldName, value: ExtractedField, index: int = 0) -> Extraction:
    return Extraction(image_index=index, fields={name: value})


def _images() -> list[ImageInput]:
    return [ImageInput(index=0, data=_png())]


class Answering:
    """A provider that returns one prepared reading for every crop it is given."""

    name = "fake:reread"

    def __init__(self, field: FieldName, value: ExtractedField | None) -> None:
        self.field = field
        self.value = value
        self.calls: list[ExtractionRequest] = []

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        self.calls.append(request)
        fields = {self.field: self.value} if self.value is not None else {}
        return ExtractionResponse(
            extractions=[Extraction(image_index=0, fields=fields)],
            usage=ProviderUsage(input_tokens=100, output_tokens=20),
        )


def _run(
    provider: object, extractions: list[Extraction], ms: float = 30_000
) -> reread.RereadOutcome:
    return reread.reread(
        extractions,
        _images(),
        provider,  # type: ignore[arg-type]
        commodity=Commodity.SPIRITS,
        budget=reread.Budget(remaining_ms=ms),
    )


def _value_of(outcome: reread.RereadOutcome, name: FieldName) -> ExtractedField:
    return outcome.extractions[0].fields[name]


# --- what it declines to look at ------------------------------------------------------


def test_a_field_with_no_region_is_never_reread() -> None:
    """The branch that makes the cropped-out-of-frame defect invisible here.

    When content is cut off at the edge of the photograph the extractor reports nothing
    and there is no box to crop to. LP-325 is filed against that defect and does not fix
    it; this test pins the reason so nobody later reads the ticket title and assumes it
    was handled.
    """
    provider = Answering(FieldName.CLASS_TYPE, ExtractedField(value="Bourbon", confidence=0.99))
    missing = ExtractedField(value=None, confidence=0.0, bbox=None)

    outcome = _run(provider, [_extraction(FieldName.CLASS_TYPE, missing)])

    assert outcome.considered == 0
    assert provider.calls == []


def test_the_government_warning_is_never_reread() -> None:
    """Its verdict is assembled from text AND typography across every image. Replacing
    the text alone would settle a WARN row on half its evidence — the same reason Tier 3
    is not allowed near it.
    """
    provider = Answering(
        FieldName.GOVERNMENT_WARNING,
        ExtractedField(value="GOVERNMENT WARNING: ...", confidence=0.99),
    )
    unsure = ExtractedField(value="GOVERNMENT WARN...", confidence=0.4, bbox=BOX)

    outcome = _run(provider, [_extraction(FieldName.GOVERNMENT_WARNING, unsure)])

    assert outcome.considered == 0
    assert provider.calls == []


def test_a_confident_reading_is_left_alone() -> None:
    """The trigger is the band where the tool was unsure, not every field on the label."""
    provider = Answering(FieldName.BRAND_NAME, ExtractedField(value="OTHER", confidence=1.0))
    confident = ExtractedField(value="OLD TOM", confidence=0.95, bbox=BOX)

    outcome = _run(provider, [_extraction(FieldName.BRAND_NAME, confident)])

    assert outcome.considered == 0
    assert provider.calls == []
    assert _value_of(outcome, FieldName.BRAND_NAME).value == "OLD TOM"


# --- what it refuses to accept back ---------------------------------------------------


def test_an_empty_reread_never_becomes_a_confident_absence() -> None:
    """A crop that found nothing is evidence the CROP was a bad idea, not evidence the
    field is absent. Accepting it would turn an unsure reading into a confident Missing,
    which is the exact inversion this product exists to prevent.
    """
    provider = Answering(FieldName.NET_CONTENTS, ExtractedField(value=None, confidence=0.99))
    unsure = ExtractedField(value="750 mL", confidence=0.5, bbox=BOX)

    outcome = _run(provider, [_extraction(FieldName.NET_CONTENTS, unsure)])

    assert outcome.reread == 1
    assert outcome.improved == 0
    assert _value_of(outcome, FieldName.NET_CONTENTS).value == "750 mL"


def test_a_less_confident_reread_is_discarded() -> None:
    """The second look disagreeing with the first is not evidence the second is right."""
    provider = Answering(FieldName.PRODUCER, ExtractedField(value="WRONG CO", confidence=0.3))
    unsure = ExtractedField(value="OLD TOM DISTILLERY", confidence=0.6, bbox=BOX)

    outcome = _run(provider, [_extraction(FieldName.PRODUCER, unsure)])

    assert outcome.improved == 0
    assert _value_of(outcome, FieldName.PRODUCER).value == "OLD TOM DISTILLERY"


def test_a_reread_that_is_still_unsure_is_discarded() -> None:
    """Higher than the original but below the floor is not good enough to overwrite with.
    The row stays honestly low-confidence rather than being nudged toward a verdict.
    """
    provider = Answering(FieldName.CLASS_TYPE, ExtractedField(value="Bourbon", confidence=0.7))
    unsure = ExtractedField(value="Bourbn", confidence=0.5, bbox=BOX)

    outcome = _run(provider, [_extraction(FieldName.CLASS_TYPE, unsure)])

    assert outcome.improved == 0
    assert _value_of(outcome, FieldName.CLASS_TYPE).value == "Bourbn"


# --- what it does accept --------------------------------------------------------------


def test_a_clearly_better_reread_replaces_the_original() -> None:
    provider = Answering(
        FieldName.CLASS_TYPE,
        ExtractedField(value="Kentucky Straight Bourbon Whiskey", confidence=0.96),
    )
    unsure = ExtractedField(value="Kentucky Str... Whisk", confidence=0.45, bbox=BOX)

    outcome = _run(provider, [_extraction(FieldName.CLASS_TYPE, unsure)])

    assert outcome.improved == 1
    assert _value_of(outcome, FieldName.CLASS_TYPE).value == "Kentucky Straight Bourbon Whiskey"


def test_the_original_region_is_kept_not_the_crops_own_coordinates() -> None:
    """The box that comes back from a crop is in the CROP's coordinate space. Storing it
    would draw the evidence outline over the wrong part of the photograph — a box that
    points at the desk while the row claims it points at the label.
    """
    provider = Answering(
        FieldName.BRAND_NAME,
        ExtractedField(
            value="OLD TOM", confidence=0.98, bbox=BoundingBox(x0=0.0, y0=0.0, x1=1.0, y1=1.0)
        ),
    )
    unsure = ExtractedField(value="OLD T0M", confidence=0.5, bbox=BOX)

    outcome = _run(provider, [_extraction(FieldName.BRAND_NAME, unsure)])

    assert outcome.improved == 1
    assert _value_of(outcome, FieldName.BRAND_NAME).bbox == BOX


def test_the_crop_sent_is_smaller_than_the_whole_frame() -> None:
    """The entire point. If the region were sent at full frame the model would see the
    same pixels it already failed to read.
    """
    provider = Answering(FieldName.BRAND_NAME, ExtractedField(value="OLD TOM", confidence=0.98))
    unsure = ExtractedField(value="OLD T0M", confidence=0.5, bbox=BOX)

    _run(provider, [_extraction(FieldName.BRAND_NAME, unsure)])

    sent = provider.calls[0].images[0].data
    with Image.open(io.BytesIO(sent)) as cropped:
        assert cropped.size[0] < 800
        assert cropped.size[1] < 600


# --- bounds ---------------------------------------------------------------------------


def test_it_declines_when_the_request_has_no_time_left() -> None:
    provider = Answering(FieldName.BRAND_NAME, ExtractedField(value="OLD TOM", confidence=0.99))
    unsure = ExtractedField(value="OLD T0M", confidence=0.5, bbox=BOX)

    outcome = _run(provider, [_extraction(FieldName.BRAND_NAME, unsure)], ms=10)

    assert outcome.reread == 0
    assert outcome.skipped_reason == "out of time"
    assert provider.calls == []


def test_no_more_than_the_cap_is_ever_read() -> None:
    """And the ones it drops are the ones it was least worried about."""
    provider = Answering(FieldName.BRAND_NAME, ExtractedField(value=None, confidence=0.9))
    fields = {
        FieldName.BRAND_NAME: ExtractedField(value="a", confidence=0.10, bbox=BOX),
        FieldName.CLASS_TYPE: ExtractedField(value="b", confidence=0.20, bbox=BOX),
        FieldName.NET_CONTENTS: ExtractedField(value="c", confidence=0.30, bbox=BOX),
        FieldName.PRODUCER: ExtractedField(value="d", confidence=0.70, bbox=BOX),
    }
    outcome = _run(provider, [Extraction(image_index=0, fields=fields)])

    assert outcome.considered == 4
    assert outcome.reread == reread.MAX_REGIONS == 3


def test_a_provider_failure_leaves_every_row_exactly_as_it_was() -> None:
    """Failing to improve is not failing to verify."""

    class Failing:
        name = "fake:failing"

        def extract(self, request: ExtractionRequest) -> ExtractionResponse:
            raise ProviderError("down", retryable=True)

    unsure = ExtractedField(value="OLD T0M", confidence=0.5, bbox=BOX)

    outcome = _run(Failing(), [_extraction(FieldName.BRAND_NAME, unsure)])

    assert outcome.improved == 0
    assert _value_of(outcome, FieldName.BRAND_NAME).value == "OLD T0M"


def test_a_region_too_small_to_hold_text_is_skipped() -> None:
    """Enlarging four pixels does not reveal detail, it invents it."""
    provider = Answering(FieldName.BRAND_NAME, ExtractedField(value="OLD TOM", confidence=0.99))
    sliver = ExtractedField(
        value="?", confidence=0.4, bbox=BoundingBox(x0=0.5, y0=0.5, x1=0.502, y1=0.502)
    )

    outcome = _run(provider, [_extraction(FieldName.BRAND_NAME, sliver)])

    assert outcome.considered == 1
    assert outcome.reread == 0
    assert provider.calls == []


@pytest.mark.parametrize("name", sorted(reread.REREADABLE, key=lambda f: f.value))
def test_every_rereadable_field_is_actually_reachable(name: FieldName) -> None:
    """A field on the list that some other guard excludes would be a list that lies."""
    assert reread.is_rereadable(name, ExtractedField(value="x", confidence=0.4, bbox=BOX))


def test_the_warning_is_the_only_field_deliberately_excluded() -> None:
    """If a field is added to the model, this fails until someone decides about it."""
    assert set(FieldName) - reread.REREADABLE == {FieldName.GOVERNMENT_WARNING}
