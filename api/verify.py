"""The verification pipeline — application in, per-field verdicts out.

This is the shared spine. Verify Now and Batch differ only in the entry point and the
processing model; both run through `verify()`, so a field verdict means the same thing
everywhere (PRD §The Two Modes).

Extraction is merged across images before any comparison happens. A two-image application
is one label: the brand is on the front and the warning is usually on the back, so
declaring the warning Missing without searching every image would be a false finding
(IMG-8, TC-16).

**The pre-model path is shared too.** Everything that happens between "bytes arrived" and
"call the model" — ingest, quality scoring, and the pre-gate — lives here as
`prepare_images`, and both entry points call it. It used to be written twice, once in
`api/routes/verify.py` and once in `api/batch/worker.py`, and the second copy is where the
pre-gate would eventually be dropped: an importer dump is the least visible place in the
product, and it is exactly where hopeless artwork arrives in bulk. One copy means the
gate cannot be true of Verify Now and false of Batch (LP-321, BATCH-6).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from api import logging as applog
from api.config import Config
from api.models import (
    Aggregate,
    Application,
    Cost,
    Extraction,
    ExtractedField,
    FieldName,
    FieldResult,
    ImageQuality,
    ImageReport,
    Recommendation,
    Timings,
    VerificationResult,
    Verdict,
    WarningTypography,
)
from api.pipeline import ingest as ingest_mod
from api.pipeline import quality as quality_mod
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

#: Said when every image was pre-gated but no scorer produced a specific reason. Should be
#: unreachable — `quality.assess` always explains a hopeless verdict — but a pre-gate that
#: refuses without saying why is a dead end for the agent holding the label.
PREGATE_FALLBACK_REASON = "The images are too poor to read the label."


# --- the shared pre-model path ----------------------------------------------------------


@dataclass(frozen=True)
class PreparedImages:
    """Everything both entry points need before deciding whether to call the model.

    `usable` is empty exactly when the pre-gate refused every image, and `reason` is then
    the sentence that says which defect refused it. The two are kept together so no caller
    can report "we could not read it" without also having something to tell the agent.
    """

    reports: list[ImageReport]
    usable: list[ImageInput]
    ingest_ms: int
    quality_ms: int
    reason: str | None

    @property
    def pregated(self) -> bool:
        """True when nothing survived the pre-gate, so no model call may be made."""
        return not self.usable


def default_roles(count: int, supplied: Sequence[str] | None = None) -> list[str | None]:
    """Which face of the label each image shows.

    Supplied roles win. Otherwise one image is the whole label and two are assumed front
    then back, which is how agents send them and what TC-16 turns on. Beyond two the
    honest answer is "unknown" — guessing would put the warning on the wrong image in the
    evidence panel.
    """
    if supplied and len(supplied) == count:
        return [role.strip().lower() or None for role in supplied]
    if count == 1:
        return ["single"]
    if count == 2:
        return ["front", "back"]
    return [None] * count


def prepare_images(
    payloads: Sequence[bytes],
    config: Config,
    *,
    roles: Sequence[str] | None = None,
    job_id: str | None = None,
    item_id: str | None = None,
) -> PreparedImages:
    """Ingest, score, and pre-gate. The one copy of the path to the model.

    Ingest is the security boundary (magic-byte sniffing, caps before decode, metadata
    stripped, re-encoded — SEC-5). Quality scoring is deterministic and costs no tokens.
    The pre-gate is what stops a model call on artwork nobody could read, which is the one
    spend in this product with a guaranteed zero return (LP-321).

    Both entry points call this, so the gate cannot hold on one path and quietly not on
    the other.

    `job_id` and `item_id` say whose images these are, and batch has to pass them. On the
    interactive path the request ID travels by ContextVar and every line is attributed for
    free; a worker thread inherits no ContextVar, so a batch line carries no request ID.
    With six workers interleaving, 600 lines whose only identifier is `image_index` — which
    is 0 or 1 — cannot answer the single question they exist for: which application was
    pre-gated, and on what defect. Both names are on the SEC-4 allowlist and neither can
    carry label text.

    They are logged as null rather than omitted on the interactive path, so one query
    shape reads both modes.
    """
    ingest_started = time.perf_counter()
    ingested = ingest_mod.ingest(list(payloads), config)
    ingest_ms = int((time.perf_counter() - ingest_started) * 1000)

    quality_started = time.perf_counter()
    scores: list[ImageQuality] = [
        quality_mod.assess(ingest_mod.to_array(image)) for image in ingested
    ]
    quality_ms = int((time.perf_counter() - quality_started) * 1000)

    faces = default_roles(len(ingested), roles)
    reports = [
        ImageReport(index=image.index, role=faces[position], quality=score)
        for position, (image, score) in enumerate(zip(ingested, scores, strict=True))
    ]
    for report in reports:
        applog.log(
            "image_scored",
            image_index=report.index,
            blur=report.quality.blur,
            exposure=report.quality.exposure,
            glare=report.quality.glare,
            skew_deg=report.quality.skew_deg,
            quality=report.quality.verdict,
            job_id=job_id,
            item_id=item_id,
        )

    usable = [
        ImageInput(
            index=image.index,
            data=image.data,
            media_type=image.media_type,
            role=faces[position],
        )
        for position, (image, score) in enumerate(zip(ingested, scores, strict=True))
        if not quality_mod.should_skip_extraction(score)
    ]

    reason = (
        None
        if usable
        else next((score.reason for score in scores if score.reason), PREGATE_FALLBACK_REASON)
    )
    return PreparedImages(
        reports=reports,
        usable=usable,
        ingest_ms=ingest_ms,
        quality_ms=quality_ms,
        reason=reason,
    )


def pregate_headline(reason: str) -> str:
    """The aggregate rationale for a label the pre-gate refused.

    Identical wording on both paths on purpose. An agent who reads "the image could not be
    read" in a batch table and then re-uploads that one label through Verify Now must not
    be told something different about the same picture.
    """
    return f"{reason} Nothing on this label could be checked. The final decision is yours."


def expected_values(application: Application) -> dict[FieldName, str | None]:
    """The application side of every row, so an unverified result still shows something."""
    producer = ", ".join(
        part for part in (application.producer_name, application.producer_address) if part
    )
    abv = application.alcohol_content
    return {
        FieldName.BRAND_NAME: application.brand_name,
        FieldName.CLASS_TYPE: application.class_type,
        FieldName.ALCOHOL_CONTENT: f"{abv:g}% alc/vol" if abv is not None else None,
        FieldName.NET_CONTENTS: application.net_contents,
        FieldName.PRODUCER: producer or None,
        FieldName.COUNTRY_OF_ORIGIN: application.country_of_origin,
        FieldName.GOVERNMENT_WARNING: "the statement required by 27 CFR 16.21",
    }


def unverified(
    application: Application,
    *,
    headline: str,
    per_field: str,
    request_id: str = "",
    reports: Sequence[ImageReport] = (),
    timings: Timings | None = None,
) -> VerificationResult:
    """A result that verified nothing, and says so on every row.

    Used by every path that stops before comparison: the pre-gate on both modes, and the
    budget stop on Verify Now (LP-079). All of them return Unreadable per field rather
    than an empty response, because a blank checklist reads as "fine" at a glance — in a
    single verdict card and even more so in a 300-row table — and this is the opposite of
    fine. There is no seventh verdict for "not attempted" and there should not be:
    Unreadable already means "we did not verify this", which is exactly true here and can
    never be mistaken for a pass.
    """
    expected = expected_values(application)
    return VerificationResult(
        request_id=request_id,
        aggregate=Aggregate(
            recommendation=Recommendation.NEEDS_REVIEW,
            rationale=headline,
            driving_field=None,
        ),
        fields=[
            FieldResult(
                field=name,
                verdict=Verdict.UNREADABLE,
                extracted=None,
                expected=expected[name],
                confidence=0.0,
                rationale=per_field,
            )
            for name in FieldName
        ],
        images=list(reports),
        timings_ms=timings if timings is not None else Timings(),
        cost=Cost(),
    )


# --- extraction and comparison ----------------------------------------------------------


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
        ),
    )
