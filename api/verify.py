"""The verification pipeline — application in, per-field verdicts out.

This is the shared spine. Verify Now and Batch differ only in the entry point and the
processing model; both run through `verify()`, so a field verdict means the same thing
everywhere (PRD §The Two Modes).

Extraction is merged across images before any comparison happens. A two-image application
is one label: the brand is on the front and the warning is usually on the back, so
declaring the warning Missing without searching every image would be a false finding
(IMG-8, TC-16).
"""

from __future__ import annotations

import time
import uuid

from api.models import (
    Aggregate,
    Application,
    Cost,
    Extraction,
    ExtractedField,
    FieldName,
    FieldResult,
    ImageReport,
    Recommendation,
    Timings,
    VerificationResult,
    Verdict,
    WarningTypography,
)
from api.provider.base import (
    ExtractionProvider,
    ExtractionRequest,
    ImageInput,
    ProviderError,
)
from api.rules import aggregate as agg
from api.rules import compare
from api.rules import warning as warn
from api.rules.commodity import LabelContext


def merge_extractions(
    extractions: list[Extraction],
) -> tuple[dict[FieldName, ExtractedField], int | None, WarningTypography, dict[FieldName, int]]:
    """Combine per-image extractions into one view of the label.

    Highest confidence wins per field, and the image it came from is recorded so the UI
    can point at the right picture (IMG-8 provenance). A legible reading always beats an
    illegible one — one image having glare over the warning does not make the warning
    unreadable when the other image shows it clearly.
    """
    merged: dict[FieldName, ExtractedField] = {}
    provenance: dict[FieldName, int] = {}
    warning_text: str | None = None
    warning_image: int | None = None
    typography = WarningTypography()

    for extraction in extractions:
        for name, field in extraction.fields.items():
            current = merged.get(name)
            better = (
                current is None
                or (field.legible and not current.legible)
                or (field.legible == current.legible and field.confidence > current.confidence)
            )
            if better:
                merged[name] = field
                provenance[name] = extraction.image_index

        if extraction.warning_text and warning_text is None:
            warning_text = extraction.warning_text
            warning_image = extraction.image_index
            typography = extraction.warning_typography

    return merged, warning_image, typography, provenance


def _warning_result(
    merged: dict[FieldName, ExtractedField],
    typography: WarningTypography,
    net_contents_ml: float | None,
) -> FieldResult:
    field = merged.get(FieldName.GOVERNMENT_WARNING)
    result = warn.evaluate(
        field.value if field else None,
        typography,
        legible=field.legible if field else True,
    )

    findings = list(result.findings)
    rationale = result.rationale
    if result.verdict is not Verdict.MATCH:
        rationale = f"{rationale} {warn.type_size_context(net_contents_ml)}".strip()

    return FieldResult(
        field=FieldName.GOVERNMENT_WARNING,
        verdict=result.verdict,
        extracted=field.value if field else None,
        expected="the statement required by 27 CFR 16.21",
        confidence=field.confidence if field else 0.0,
        rationale=rationale,
        evidence=None,
        findings=findings,
    )


def verify(
    application: Application,
    images: list[ImageInput],
    provider: ExtractionProvider,
) -> VerificationResult:
    """Run one application through the pipeline."""
    request_id = f"req_{uuid.uuid4().hex[:16]}"
    timings = Timings()
    started = time.perf_counter()

    try:
        extract_started = time.perf_counter()
        response = provider.extract(
            ExtractionRequest(commodity=application.commodity, images=images)
        )
        timings.extract = int((time.perf_counter() - extract_started) * 1000)
    except ProviderError as exc:
        raise exc

    # TC-15 — nobody uploaded a label.
    if response.extractions and not any(e.is_label for e in response.extractions):
        return VerificationResult(
            request_id=request_id,
            aggregate=Aggregate(
                recommendation=Recommendation.NEEDS_REVIEW,
                rationale=(
                    "This does not look like a label. Nothing has been checked — "
                    "upload the label artwork for this application."
                ),
                driving_field=None,
            ),
            fields=[],
            images=[],
            timings_ms=timings,
            cost=Cost(),
        )

    compare_started = time.perf_counter()
    merged, _warning_image, typography, _provenance = merge_extractions(response.extractions)

    context = LabelContext(
        is_import=application.is_import,
        class_type=application.class_type,
        application_abv=application.alcohol_content,
    )

    from api.rules import fills as fill_rules

    net = fill_rules.parse(application.net_contents)

    results = [
        compare.compare_brand_name(merged.get(FieldName.BRAND_NAME), application.brand_name),
        compare.compare_class_type(merged.get(FieldName.CLASS_TYPE), application.class_type),
        compare.compare_alcohol_content(
            merged.get(FieldName.ALCOHOL_CONTENT),
            application.alcohol_content,
            application.commodity,
            context,
        ),
        compare.compare_net_contents(
            merged.get(FieldName.NET_CONTENTS),
            application.net_contents,
            application.commodity,
        ),
        compare.compare_producer(
            merged.get(FieldName.PRODUCER),
            application.producer_name,
            application.producer_address,
        ),
        compare.compare_country_of_origin(
            merged.get(FieldName.COUNTRY_OF_ORIGIN),
            application.country_of_origin,
            is_import=application.is_import,
        ),
        _warning_result(merged, typography, net.ml),
    ]

    aggregate = agg.recommend(results)
    timings.compare = int((time.perf_counter() - compare_started) * 1000)
    timings.total = int((time.perf_counter() - started) * 1000)

    return VerificationResult(
        request_id=request_id,
        aggregate=aggregate,
        fields=agg.triage_order(results),
        images=[],
        timings_ms=timings,
        cost=Cost(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
        ),
    )
