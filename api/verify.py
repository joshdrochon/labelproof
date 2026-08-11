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
    Evidence,
    FieldName,
    FieldResult,
    Recommendation,
    Timings,
    Verdict,
    VerificationResult,
)
from api.pipeline import merge as merge_images
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


def _warning_result(
    label: merge_images.MergedLabel,
    net_contents_ml: float | None,
) -> FieldResult:
    field = label.fields.get(FieldName.GOVERNMENT_WARNING)
    result = warn.evaluate(
        field.value if field else None,
        label.warning_typography,
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
        # On a front/back application the statement is on the back, and this is the one
        # row where an agent most needs to be sent to the right picture (TC-16, IMG-8).
        evidence=(
            Evidence(image_index=field.image_index, bbox=field.bbox)
            if field and field.bbox
            else None
        ),
        findings=findings,
    )


def _apply_merge(results: list[FieldResult], label: merge_images.MergedLabel) -> None:
    """Stamp each row with the picture its value came from, and surface any conflict.

    Two jobs, both of which only the merge knows the answer to. The comparators see one
    value and cannot know which of four photographs it was read off (IMG-8), and they
    cannot know that a second picture read the same field differently.

    A conflicted field already arrives from the merge as not-legible, so the comparator
    has independently produced Unreadable — the right verdict for "we have not established
    what the label says", and one that routes to Needs review rather than to either
    Ready to approve or Return for correction. What it cannot produce is the reason, so
    the rationale is replaced with one that names both readings.
    """
    for result in results:
        merged = label.fields.get(result.field)
        if merged is None:
            continue

        if merged.bbox is not None and result.evidence is not None:
            result.evidence = Evidence(image_index=merged.image_index, bbox=merged.bbox)

        if merged.conflict is not None:
            result.rationale = merge_images.conflict_rationale(merged.conflict)
            result.findings = [
                merge_images.conflict_finding(merged.conflict),
                *result.findings,
            ]


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
    label = merge_images.merge(response.extractions)
    merged = label.extracted()

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
        _warning_result(label, net.ml),
    ]
    _apply_merge(results, label)

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
        ),
    )
