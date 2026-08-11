"""The verification pipeline — application in, per-field verdicts out.

This is the shared spine. Verify Now and Batch differ only in the entry point and the
processing model; both run through `verify()`, so a field verdict means the same thing
everywhere (PRD §The Two Modes).

Extraction is merged across images before any comparison happens. A two-image application
is one label: the brand is on the front and the warning is usually on the back, so
declaring the warning Missing without searching every image would be a false finding
(IMG-8, TC-16).

**Two merges, one for the fields and one for the statement, and they do not overlap.**

`api.pipeline.merge` folds the seven ordinary readings: per-field provenance, best
confidence when the pictures agree, and a refusal to pick when they materially disagree
(LP-058, LP-067). `api.rules.warning` folds the government warning's *typography* across
every picture that carried the statement, because a bold-body violation detected on one
photograph is a violation of the label however the other photograph read it (LP-217).

Those answer different questions, so the warning row is judged once, by the sighting path,
and `_apply_merge` deliberately leaves it alone. Running both over the same row would
report one fact twice and would let the generic merge's evidence box — chosen for a
different reason than the statement was — land on a photograph the quoted text did not
come from.
"""

from __future__ import annotations

import time
import uuid

from api import canon
from api.models import (
    Aggregate,
    Application,
    Cost,
    Evidence,
    ExtractedField,
    Extraction,
    FieldName,
    FieldResult,
    Recommendation,
    Timings,
    VerificationResult,
    WarningTypography,
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


def merge_extractions(
    extractions: list[Extraction],
) -> tuple[dict[FieldName, ExtractedField], int | None, WarningTypography, dict[FieldName, int]]:
    """Combine per-image extractions into one view of the label.

    The field half delegates to `api.pipeline.merge`: highest confidence wins where the
    pictures agree, a legible reading beats an illegible one, two pictures that materially
    disagree establish nothing at all, and every winning value records the image it came
    from so the UI can point at the right picture (IMG-8 provenance).

    The typography half does not, and must not. It is folded across every image that
    carried the statement rather than taken off the chosen sighting — this function is
    public and `_warning_result` is not its only possible caller, so returning the single
    sighting's signals here would hand the next caller a reading that silently drops a
    violation another image established (LP-217).
    """
    label = merge_images.merge(extractions)

    sightings = warning_sightings(extractions)
    chosen = warn.select_sighting(sightings)

    return (
        label.extracted(),
        chosen.image_index if chosen is not None else None,
        warn.merge_sighting_typography(sightings),
        {name: field.image_index for name, field in label.fields.items()},
    )


def warning_sightings(extractions: list[Extraction]) -> list[warn.WarningSighting]:
    """One reading of the warning per image, including the images that showed none.

    The extractor reports the warning twice — as `warning_text` with its typography, and
    as an ordinary field with a confidence and a region. Both are folded in here, because
    the choice of which image to judge the application on has to be made once, on the
    whole picture, rather than differently in two places.

    Two restrictions on what counts as a sighting at all, both of them fail-closed:

    * Images the extractor flagged `is_label=False` do not appear. A carton photo or a
      marketing one-sheet in the same upload is a picture of something else, and a warning
      read off it would answer for artwork that carries none (TC-15).
    * An image that omitted the `government_warning` field said "I looked, it is not on
      this one" (LP-067), and a bare `warning_text` does not overturn that. A provider
      that reports the statement while omitting the field has supplied a warning nothing
      actually read — the one field that must fail closed (WARN-6).
    """
    sightings: list[warn.WarningSighting] = []
    for extraction in merge_images.contributing(extractions):
        field = extraction.fields.get(FieldName.GOVERNMENT_WARNING)
        sightings.append(
            warn.WarningSighting(
                image_index=extraction.image_index,
                text=(extraction.warning_text or field.value) if field else None,
                legible=field.legible if field else True,
                confidence=field.confidence if field else 0.0,
                typography=extraction.warning_typography,
                bbox=field.bbox if field else None,
            )
        )
    return sightings


def _warning_result(
    extractions: list[Extraction],
    net_contents_ml: float | None,
) -> FieldResult:
    """The warning row, judged across every image before Missing is declared (LP-217)."""
    sightings = warning_sightings(extractions)
    result = warn.evaluate_across_images(sightings, net_contents_ml=net_contents_ml)
    chosen = warn.select_sighting(sightings)

    return FieldResult(
        field=FieldName.GOVERNMENT_WARNING,
        verdict=result.verdict,
        extracted=chosen.text if chosen else None,
        # The canonical statement, not a description of it. This is the left-hand side of
        # the diff the UI renders (WARN-8); a placeholder sentence there produces a
        # word-level comparison between the regulation's name and its text, which is
        # noise dressed up as evidence.
        expected=canon.CANONICAL_WARNING,
        confidence=chosen.confidence if chosen else 0.0,
        rationale=result.rationale,
        # Region and image both come off the chosen sighting. Taking the box from the
        # merged field and the index from somewhere else drew image 0's rectangle over
        # image 1's photograph — on the row the PRD most wants outlined.
        evidence=(
            Evidence(image_index=chosen.image_index, bbox=chosen.bbox)
            if chosen is not None and chosen.bbox is not None
            else None
        ),
        findings=list(result.findings),
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

    **The government warning is skipped, and that is not an oversight.** `_warning_result`
    has already judged it across every image: its evidence comes off the sighting the
    statement was quoted from, so region and photograph cannot come apart, and two panels
    carrying different statements already raise `warning_differs_between_images` and
    demote the row off Match. Stamping the generic merge on top would move the box to a
    picture chosen by a different rule and report the same disagreement a second time.
    """
    for result in results:
        if result.field is FieldName.GOVERNMENT_WARNING:
            continue

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
        # The extractions, not the merged label: the warning is judged on sightings, so
        # that a violation seen on one photograph survives the choice of another (LP-217).
        _warning_result(response.extractions, net.ml),
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
