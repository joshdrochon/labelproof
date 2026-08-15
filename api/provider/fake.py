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
import time
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
from api.rules import adjudicate as adjudicate_mod
from fixtures.generator.catalog import by_name
from fixtures.generator.layout import FIELD_BANDS
from fixtures.generator.spec import LabelSpec

#: Where each field actually sits on a generated label.
#:
#: Taken from `fixtures.generator.layout.FIELD_BANDS`, which is measured from the rendered
#: pixels and guarded by `test_robustness` — move a block in `render.py` and that suite
#: fails rather than silently drifting.
#:
#: This used to be a second, hand-guessed table living here. It disagreed with the
#: measured one about every field, and about the warning by twenty percent of the image
#: height: it put the statement at 0.66–0.88, which is inside `layout.BLANK_BAND`, the
#: region defined as "a band with nothing printed in it". So every sample on the demo drew
#: the government warning's outline on blank paper — the first thing a reviewer clicks,
#: pointing at nothing, under a caption promising "outlined areas are where each checked
#: value was read".
#:
#: Two tables describing one layout, and only one of them was measured.
_APPROX_REGIONS: dict[FieldName, BoundingBox] = dict(FIELD_BANDS)


def _unless_unread(unread: frozenset[str], name: str, value: bool) -> bool | None:
    """None when the fixture says the extractor could not judge this signal."""
    return None if name in unread else value


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

                    # A signal named in `warning_signals_unread` comes back None — the
                    # extractor could not judge it. Deriving every signal from the spec
                    # meant the fake always answered, so the abstention paths were
                    # unreachable from any fixture and every golden-set typography
                    # assertion was circular.
                    unread = spec.warning_signals_unread
                    typography = WarningTypography(
                        header_is_all_caps=_unless_unread(
                            unread, "header_is_all_caps",
                            spec.warning_header_case == "upper",
                        ),
                        header_is_bold=_unless_unread(
                            unread, "header_is_bold", spec.warning_header_bold
                        ),
                        body_is_bold=_unless_unread(
                            unread, "body_is_bold", spec.warning_body_bold
                        ),
                        relative_size=(
                            None if "relative_size" in unread else spec.warning_scale
                        ),
                        contrast_ok=_unless_unread(
                            unread, "contrast_ok", spec.warning_contrast >= 0.6
                        ),
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


#: `tc03b_non_bold_warning_header.png` is a fixture too. The letter suffix marks a variant
#: of a PRD test case rather than a new one, and leaving it out of the pattern meant every
#: variant fixture mapped to no spec at all — the offline providers and the recorder both
#: key off this, so a fixture they cannot name is a fixture they cannot serve.
_FIXTURE_KEY = re.compile(r"^(tc\d{2}[a-z]?_[a-z0-9_]+?)(?:_(?:front|back))?$")


def spec_name_for_image(filename: str) -> str | None:
    """Map `tc03_title_case_warning_back.png` back to its fixture name."""
    match = _FIXTURE_KEY.match(Path(filename).stem)
    return match.group(1) if match else None


# --------------------------------------------------------------------------------------
# Tier 3 (LP-232) — adjudicator fakes, so CI can exercise judgement without a model
# --------------------------------------------------------------------------------------


class ScriptedAdjudicator:
    """Answers from a table, so a test can state the judgement it is testing against.

    Keyed on the pair of values rather than on the field: the question this tier answers
    is "are these two strings the same thing", and the same pair should get the same
    answer wherever it appears.

    An unlisted pair returns `same_thing=False`, which leaves the Mismatch standing. That
    default is the safe direction and it means a test that forgets to script a case fails
    by NOT adjudicating rather than by silently passing something.
    """

    name = "fake:scripted-adjudicator"

    def __init__(
        self,
        answers: dict[tuple[str, str], tuple[bool, float, str]] | None = None,
    ) -> None:
        self._answers = answers or {}
        self.calls: list[adjudicate_mod.AdjudicationRequest] = []

    def judge(
        self, request: adjudicate_mod.AdjudicationRequest
    ) -> adjudicate_mod.Judgement:
        self.calls.append(request)
        key = (request.expected, request.extracted)
        same, confidence, rationale = self._answers.get(
            key, (False, 1.0, "These do not appear to be the same thing.")
        )
        return adjudicate_mod.Judgement(
            same_thing=same, confidence=confidence, rationale=rationale
        )


class FailingAdjudicator:
    """Raises on every call. A failed judgement must never become a pass."""

    name = "fake:failing-adjudicator"

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or RuntimeError("the adjudicator is unavailable")
        self.calls = 0

    def judge(
        self, request: adjudicate_mod.AdjudicationRequest
    ) -> adjudicate_mod.Judgement:
        self.calls += 1
        raise self._error


class SlowAdjudicator:
    """Answers, but only after `delay_s`. For asserting the time budget is respected."""

    name = "fake:slow-adjudicator"

    def __init__(self, delay_s: float = 5.0) -> None:
        self._delay_s = delay_s
        self.calls = 0

    def judge(
        self, request: adjudicate_mod.AdjudicationRequest
    ) -> adjudicate_mod.Judgement:
        self.calls += 1
        time.sleep(self._delay_s)
        return adjudicate_mod.Judgement(True, 1.0, "Eventually.")
