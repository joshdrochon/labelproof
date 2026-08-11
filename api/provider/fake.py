"""Offline providers. CI uses these exclusively — zero live calls (ENG-3, LP-065).

Two implementations:

**`SpecBackedProvider`** derives an extraction from the `LabelSpec` that generated the
image. Since the spec is ground truth, this exercises the entire rules pipeline —
comparison, tiers, warning checks, aggregation — with no model in the loop at all. It
tests *our* logic rather than the model's, which is exactly what a unit suite should do.

**`RecordedProvider`** replays real provider responses captured by the fixture recorder
(LP-064). This is what proves the pipeline handles real model output, including its
imperfections.

**`FailingProvider`** simulates the provider being unreachable (TC-21).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from api.models import (
    BoundingBox,
    ExtractedField,
    Extraction,
    FieldName,
    WarningTypography,
)
from api.provider.base import (
    ExtractionRequest,
    ExtractionResponse,
    ProviderError,
    ProviderUsage,
)
from fixtures.generator.catalog import by_name
from fixtures.generator.spec import LabelSpec

#: Where each field sits on a rendered label, roughly. Evidence boxes only ever point an
#: agent's eye at a region, so approximate is correct here — see BUILD.md §1.
_APPROX_REGIONS: dict[FieldName, BoundingBox] = {
    FieldName.BRAND_NAME: BoundingBox(x0=0.08, y0=0.06, x1=0.92, y1=0.18),
    FieldName.CLASS_TYPE: BoundingBox(x0=0.08, y0=0.20, x1=0.92, y1=0.28),
    FieldName.ALCOHOL_CONTENT: BoundingBox(x0=0.08, y0=0.32, x1=0.92, y1=0.38),
    FieldName.NET_CONTENTS: BoundingBox(x0=0.08, y0=0.38, x1=0.92, y1=0.44),
    FieldName.COUNTRY_OF_ORIGIN: BoundingBox(x0=0.08, y0=0.44, x1=0.92, y1=0.50),
    FieldName.PRODUCER: BoundingBox(x0=0.08, y0=0.54, x1=0.92, y1=0.62),
    FieldName.GOVERNMENT_WARNING: BoundingBox(x0=0.08, y0=0.66, x1=0.92, y1=0.88),
}


class SpecBackedProvider:
    """Extracts what the generator drew, because it has the spec that drew it."""

    name = "fake:spec"

    def __init__(self, spec: LabelSpec | str, *, illegible: set[FieldName] | None = None):
        self.spec = by_name(spec) if isinstance(spec, str) else spec
        self.illegible = illegible or set()

    def _put(
        self,
        fields: dict[FieldName, ExtractedField],
        name: FieldName,
        value: str | None,
        present: bool,
    ) -> None:
        """Record one extracted field.

        A method rather than a closure over the per-image `fields` dict: a function
        defined inside the loop captures the loop variable by reference, which is a live
        bug the day someone defers the call (ruff B023).
        """
        if not present:
            return
        if name in self.illegible:
            fields[name] = ExtractedField(
                value=None, confidence=0.0, legible=False,
                bbox=_APPROX_REGIONS.get(name),
            )
            return
        fields[name] = ExtractedField(
            value=value or None,
            confidence=0.95 if value else 0.0,
            legible=True,
            bbox=_APPROX_REGIONS.get(name),
        )

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        spec = self.spec
        extractions: list[Extraction] = []

        for image in request.images:
            face = image.role or spec.face
            on_front = face in ("front", "single")
            on_back = face in ("back", "single")

            fields: dict[FieldName, ExtractedField] = {}
            put = self._put

            put(fields, FieldName.BRAND_NAME, spec.brand_name, on_front)
            put(fields, FieldName.CLASS_TYPE, spec.class_type, on_front)
            put(fields, FieldName.ALCOHOL_CONTENT, spec.alcohol_text, on_front or face == "back")
            put(fields, FieldName.NET_CONTENTS, spec.net_contents, on_front or face == "back")
            put(
                fields,
                FieldName.COUNTRY_OF_ORIGIN,
                spec.country_of_origin,
                on_front or face == "back",
            )
            put(fields, FieldName.PRODUCER, spec.producer, on_back)

            warning_text: str | None = None
            typography = WarningTypography()
            if spec.include_warning and on_back:
                if FieldName.GOVERNMENT_WARNING in self.illegible:
                    fields[FieldName.GOVERNMENT_WARNING] = ExtractedField(
                        value=None, confidence=0.0, legible=False,
                        bbox=_APPROX_REGIONS[FieldName.GOVERNMENT_WARNING],
                    )
                else:
                    warning_text = spec.rendered_warning()
                    typography = WarningTypography(
                        header_is_all_caps=spec.warning_header_case == "upper",
                        header_is_bold=spec.warning_header_bold,
                        body_is_bold=spec.warning_body_bold,
                        relative_size=spec.warning_scale,
                        contrast_ok=spec.warning_contrast >= 0.6,
                    )
                    fields[FieldName.GOVERNMENT_WARNING] = ExtractedField(
                        value=warning_text, confidence=0.95, legible=True,
                        bbox=_APPROX_REGIONS[FieldName.GOVERNMENT_WARNING],
                    )

            extractions.append(
                Extraction(
                    image_index=image.index,
                    is_label=True,
                    fields=fields,
                    warning_text=warning_text,
                    warning_typography=typography,
                )
            )

        return ExtractionResponse(
            extractions=extractions,
            usage=ProviderUsage(model="fake:spec"),
            latency_ms=0,
        )


class RecordedProvider:
    """Replays provider responses captured from a real model (LP-064)."""

    name = "fake:recorded"

    def __init__(self, directory: Path, key: str):
        self.path = directory / f"{key}.json"

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        if not self.path.exists():
            raise ProviderError(
                f"No recorded fixture at {self.path}. Record one with the capture tool "
                f"before running this test offline.",
                retryable=False,
            )
        payload = json.loads(self.path.read_text())
        return ExtractionResponse(
            extractions=[Extraction.model_validate(e) for e in payload["extractions"]],
            usage=ProviderUsage(**payload.get("usage", {})),
            latency_ms=payload.get("latency_ms", 0),
        )


class FailingProvider:
    """Always unreachable. TC-21 — the app must degrade in a sentence, not a stack trace."""

    name = "fake:failing"

    def __init__(self, message: str = "Connection refused", *, retryable: bool = True):
        self.message = message
        self.retryable = retryable

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        raise ProviderError(self.message, retryable=self.retryable)


class NonLabelProvider:
    """Reports that the image is not a label at all. TC-15 — somebody uploads a cat."""

    name = "fake:non-label"

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        return ExtractionResponse(
            extractions=[
                Extraction(image_index=i.index, is_label=False, fields={})
                for i in request.images
            ],
            usage=ProviderUsage(model="fake:non-label"),
        )


_FIXTURE_KEY = re.compile(r"^(tc\d{2}_[a-z0-9_]+?)(?:_(?:front|back))?$")


def spec_name_for_image(filename: str) -> str | None:
    """Map `tc03_title_case_warning_back.png` back to its fixture name."""
    match = _FIXTURE_KEY.match(Path(filename).stem)
    return match.group(1) if match else None
