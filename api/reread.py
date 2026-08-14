"""Read a low-confidence field again, from a crop of the region it was found in (LP-325).

**What this is for, stated narrowly.** Ingest downscales every upload to
`Config.target_long_edge_px` before extraction, and the provider downsizes further on its
own side. A field occupying five percent of that frame is a few dozen pixels of text by
the time a model sees it, and the honest outcome is a low-confidence read. Send the region
BY ITSELF and those same source pixels are the whole image instead of a fraction of a
shrunken one — the text gets the detail the frame was spending on the bottle, the desk and
the background.

**What this is NOT for, and the ticket is misleading about it.** LP-325 is filed against
the cropped-out-of-frame defect, and it does not fix it. When the content is cut off at
the edge of the photograph the extractor sees nothing there, reports nothing, and there is
no bounding box to crop to. A field with no evidence region is invisible to this module by
construction. That defect needs a frame-boundary signal the pipeline does not compute; it
stays open and stays in the README.

What this does reach is the other kind of miss: text that IS in the frame, WAS read, and
was read badly. On the committed Tier B run those are the conservative rows — a correct
warning demoted to Unreadable, a correct class type softened to a variation — where the
tool over-flagged because it could not see well enough to commit.

**It can only improve a row.** Every path out of `reread` either returns a strictly better
reading or returns the original untouched:

  - a field with no bounding box is never re-read — there is nothing to crop to;
  - the government warning is never re-read, for the same reason Tier 3 never touches it:
    its verdict is assembled from text AND typography across every image, and replacing
    the text alone would settle a WARN row on half its evidence;
  - a re-read that comes back empty is discarded, so a confident absence can never be
    manufactured out of a hopeful crop;
  - a re-read that comes back LESS confident is discarded, because the second look
    disagreeing with the first is not evidence that the second is right;
  - any provider failure leaves every row exactly as it was.

**It is bounded before it is called.** The budget is what remains of the request, not a
fresh allowance, and the cap on regions is deliberately small: this runs on the labels
that were already the slowest to read.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field, replace
from typing import Final

from PIL import Image

from api import logging as applog
from api.models import BoundingBox, Commodity, ExtractedField, Extraction, FieldName
from api.provider.base import (
    ExtractionProvider,
    ExtractionRequest,
    ImageInput,
    ProviderError,
    ProviderUsage,
)

#: Fields worth a second look. The government warning is absent on purpose — see the
#: module docstring. Country of origin is here because an import's origin is often set in
#: small type near the producer block, which is exactly the size that reads badly.
REREADABLE: Final[frozenset[FieldName]] = frozenset(
    {
        FieldName.BRAND_NAME,
        FieldName.CLASS_TYPE,
        FieldName.ALCOHOL_CONTENT,
        FieldName.NET_CONTENTS,
        FieldName.PRODUCER,
        FieldName.COUNTRY_OF_ORIGIN,
    }
)

#: Below this, a reading is worth questioning. Deliberately the same number the warning
#: path uses for its own demotion — a confidence this product already treats as "do not
#: rely on this" is the right trigger for looking again, and having two different floors
#: for "unsure" would be two definitions of the same word.
FLOOR: Final[float] = 0.75

#: At or above this, a re-read is accepted. A crop that comes back still unsure has told
#: us the text is genuinely hard to read, which is information — and the original reading
#: is kept, so the row stays honestly low-confidence rather than being nudged.
ACCEPT: Final[float] = 0.75

#: How many regions one verification may re-read. Small on purpose: each is a provider
#: call, and this only ever runs on labels that were already slow.
MAX_REGIONS: Final[int] = 3

#: A tight crop cuts glyph edges and loses the words either side that give a value its
#: meaning — "750" without "mL" beside it is not net contents. Padding is a fraction of
#: the region's own size, so a small region gets proportionally more room.
_PAD: Final[float] = 0.15

#: Below this many pixels on an edge, there is nothing left to recover; the region was
#: tiny in the source too, and enlarging it invents detail rather than revealing it.
_MIN_EDGE_PX: Final[int] = 24


@dataclass(frozen=True)
class Budget:
    """What is left of the request when the re-reader is offered a turn."""

    remaining_ms: float
    estimated_ms: float = 1200.0

    def allows_one(self) -> bool:
        return self.remaining_ms >= self.estimated_ms


@dataclass(frozen=True)
class RereadOutcome:
    """What happened, in numbers a log line can carry (OPS-1, OPS-4)."""

    extractions: list[Extraction]
    considered: int
    reread: int
    improved: int
    elapsed_ms: int = 0
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    skipped_reason: str | None = None


def is_rereadable(name: FieldName, value: ExtractedField) -> bool:
    """Whether this field is a candidate at all.

    Every condition is a reason NOT to spend a provider call, which is the point — the
    cheapest re-read is the one that does not happen.
    """
    if name not in REREADABLE:
        return False
    if value.bbox is None:
        # Nothing was seen, so there is no region to look at again. This is the branch
        # that makes the cropped-out-of-frame defect invisible here.
        return False
    # Not `return value.confidence < FLOOR` — the three guards read as three separate
    # reasons to decline, which is what they are.
    return value.confidence < FLOOR


def _crop(data: bytes, bbox: BoundingBox) -> bytes | None:
    """Cut the region out of the preprocessed image, with padding. None if not worth it.

    The bounding box is normalised against the PREPROCESSED image, which is exactly the
    bytes handed to the extractor — so these coordinates need no transform. Drawing them
    against the original upload would be off by whatever deskew and downscale did.

    DECLINES rather than raises when the bytes will not open. This is an improvement pass
    bolted onto a verification that has already succeeded; a decode error here must cost
    the agent a slightly-unsure row, never the whole result. The test suite found this by
    handing it placeholder bytes, and it would have been a 500 on any upload our own
    pipeline could write but Pillow could not read back.
    """
    try:
        opened = Image.open(io.BytesIO(data))
    except Exception:  # noqa: BLE001 — any decode failure is the same decision: decline
        return None

    with opened:
        image = opened.convert("RGB")
        width, height = image.size

        pad_x = (bbox.x1 - bbox.x0) * _PAD
        pad_y = (bbox.y1 - bbox.y0) * _PAD
        left = max(0.0, bbox.x0 - pad_x) * width
        top = max(0.0, bbox.y0 - pad_y) * height
        right = min(1.0, bbox.x1 + pad_x) * width
        bottom = min(1.0, bbox.y1 + pad_y) * height

        box = (int(left), int(top), round(right), round(bottom))
        if box[2] - box[0] < _MIN_EDGE_PX or box[3] - box[1] < _MIN_EDGE_PX:
            return None

        region = image.crop(box)
        buffer = io.BytesIO()
        # PNG, not JPEG. The whole point is detail on small glyphs, and JPEG spends its
        # error budget precisely on high-frequency edges — which is what text is.
        region.save(buffer, format="PNG")
        return buffer.getvalue()


def _crop_or_none(data: bytes, bbox: BoundingBox) -> bytes | None:
    """`_crop`, with every remaining failure mode collapsed to 'do not re-read'."""
    try:
        return _crop(data, bbox)
    except Exception:  # noqa: BLE001 — see `_crop`; the decision is the same for all of them
        return None


def _better(original: ExtractedField, candidate: ExtractedField | None) -> bool:
    """Whether the second reading should replace the first. Conservative by construction."""
    if candidate is None:
        return False
    if candidate.value is None or not candidate.value.strip():
        # A crop that found nothing is not evidence the field is absent — it is evidence
        # this crop was a bad idea. Manufacturing an absence here would turn a
        # low-confidence reading into a confident Missing, which is the exact inversion
        # this product exists to avoid.
        return False
    if candidate.confidence < ACCEPT:
        return False
    return candidate.confidence > original.confidence


def reread(
    extractions: list[Extraction],
    images: list[ImageInput],
    provider: ExtractionProvider,
    *,
    commodity: Commodity,
    budget: Budget,
) -> RereadOutcome:
    """Re-read low-confidence fields from crops. Returns extractions, improved or not."""
    started = time.perf_counter()
    by_index = {image.index: image for image in images}

    candidates: list[tuple[int, FieldName, ExtractedField]] = []
    for extraction in extractions:
        # A carton photo, a marketing sheet, a printout of the regulation — the extractor
        # flags these `is_label=False` and still reads text off them. Re-reading a region
        # of one at higher resolution would spend a call sharpening something that must
        # not answer for the label in the first place.
        if not extraction.is_label:
            continue
        for name, value in extraction.fields.items():
            if is_rereadable(name, value) and extraction.image_index in by_index:
                candidates.append((extraction.image_index, name, value))

    if not candidates:
        return RereadOutcome(extractions=extractions, considered=0, reread=0, improved=0)

    # Worst first. If the cap bites, it should bite on the readings that were already
    # good enough rather than on the one nobody can read.
    candidates.sort(key=lambda item: item[2].confidence)
    considered = len(candidates)
    candidates = candidates[:MAX_REGIONS]

    if not budget.allows_one():
        return RereadOutcome(
            extractions=extractions,
            considered=considered,
            reread=0,
            improved=0,
            skipped_reason="out of time",
        )

    updated = {e.image_index: dict(e.fields) for e in extractions}
    usage = ProviderUsage()
    done = improved = 0
    remaining = budget

    for image_index, name, original in candidates:
        if not remaining.allows_one():
            break
        region = original.bbox
        if region is None:  # `is_rereadable` guarantees this, but narrowing needs it said
            continue

        cropped = _crop_or_none(by_index[image_index].data, region)
        if cropped is None:
            continue

        call_started = time.perf_counter()
        try:
            response = provider.extract(
                ExtractionRequest(
                    commodity=commodity,
                    images=[ImageInput(index=0, data=cropped, media_type="image/png")],
                )
            )
        except ProviderError:
            # The first reading stands. A re-read is an improvement pass; failing to
            # improve is not failing to verify, and taking the request down over it would
            # trade a slightly-unsure row for no row at all.
            applog.warn("reread_failed", kind="provider", code="provider_unavailable")
            break

        actual_ms = max(1, int((time.perf_counter() - call_started) * 1000))
        remaining = replace(remaining, remaining_ms=remaining.remaining_ms - actual_ms)
        usage.input_tokens += response.usage.input_tokens
        usage.output_tokens += response.usage.output_tokens
        done += 1

        candidate = next(
            (e.fields.get(name) for e in response.extractions if name in e.fields), None
        )
        if _better(original, candidate) and candidate is not None:
            # The bounding box from the CROP is in the crop's own coordinates and would
            # draw a box over the wrong part of the photograph. Keep the original region:
            # it is where the field is, and it is what the agent will be shown.
            updated[image_index][name] = candidate.model_copy(update={"bbox": region})
            improved += 1

    return RereadOutcome(
        extractions=[
            e.model_copy(update={"fields": updated[e.image_index]})
            if e.image_index in updated
            else e
            for e in extractions
        ],
        considered=considered,
        reread=done,
        improved=improved,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        usage=usage,
    )
