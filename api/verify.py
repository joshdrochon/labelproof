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
from dataclasses import dataclass, replace

from api import canon, timing
from api import logging as applog
from api import reread as reread_mod
from api.config import Config
from api.models import (
    Aggregate,
    Application,
    Cost,
    Evidence,
    ExtractedField,
    Extraction,
    FieldName,
    FieldResult,
    ImageQuality,
    ImageReport,
    Recommendation,
    Timings,
    Verdict,
    VerificationResult,
    WarningTypography,
)
from api.pipeline import ingest as ingest_mod
from api.pipeline import merge as merge_images
from api.pipeline import quality as quality_mod
from api.provider.base import (
    ExtractionProvider,
    ExtractionRequest,
    ImageInput,
    ProviderError,
    ProviderUsage,
)
from api.rules import adjudicate as adjudicate_mod
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

    #: Images the pre-gate refused, by index, when OTHERS survived.
    #:
    #: This exists because the all-or-nothing view was wrong in the dangerous direction.
    #: A two-image application whose back panel is unreadable had that panel silently
    #: dropped and the remaining front sent to the model — which then, correctly, did not
    #: find a government warning on it. The verdict came back **Missing**, with the
    #: rationale "no government warning statement was found on any of the supplied
    #: images". Both halves false: there were two images, one was never looked at, and
    #: the label was compliant.
    #:
    #: Missing is a finding against the LABEL and grounds to return an application.
    #: "We did not read one of your photographs" is a finding about the PHOTOGRAPH. The
    #: whole product turns on not confusing those, and here it did.
    skipped: tuple[int, ...] = ()

    #: Why each skipped image was refused, in the same order, so the agent is told which
    #: picture to retake rather than that something was wrong somewhere.
    skipped_reasons: tuple[str, ...] = ()

    @property
    def pregated(self) -> bool:
        """True when nothing survived the pre-gate, so no model call may be made."""
        return not self.usable

    @property
    def partial(self) -> bool:
        """True when SOME images were refused and others were read.

        While this is true, no field may be reported Missing: "not on the label" is a
        claim about the whole label, and part of it was never seen.
        """
        return bool(self.skipped) and bool(self.usable)


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

    skipped = tuple(
        image.index
        for image, score in zip(ingested, scores, strict=True)
        if quality_mod.should_skip_extraction(score)
    )
    skipped_reasons = tuple(
        score.reason or PREGATE_FALLBACK_REASON
        for score in scores
        if quality_mod.should_skip_extraction(score)
    )

    reason = (
        None
        if usable
        else next((score.reason for score in scores if score.reason), PREGATE_FALLBACK_REASON)
    )
    return PreparedImages(
        reports=reports,
        usable=usable,
        skipped=skipped,
        skipped_reasons=skipped_reasons,
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


def _demote_missing_when_unseen(
    results: list[FieldResult], reasons: tuple[str, ...]
) -> list[FieldResult]:
    """Missing becomes Unreadable while any supplied photograph went unread.

    The defect this closes, measured: a two-image application whose back panel was too
    poor to read had that panel dropped by the pre-gate, the front sent to the model, and
    the government warning reported **Missing** — with the rationale "no government
    warning statement was found on any of the supplied images". There were two images.
    One was never looked at. The label was compliant, and the tool recommended returning
    it to the applicant.

    Missing is a finding against the LABEL. Unreadable is a statement about the
    PHOTOGRAPH. Everything in this product turns on not confusing the two, and the
    all-or-nothing pre-gate quietly did: `pregated` was only true when EVERY image
    failed, so the mixed case — much the more likely one, since agents send a front and a
    back and only one of them is usually bad — took the silent path.

    Only Missing moves. A Mismatch found on an image we DID read is still a Mismatch: an
    unread second photograph does not excuse a defect visible on the first, and demoting
    it would let a bad upload wash out a real finding.
    """
    note = reasons[0] if reasons else PREGATE_FALLBACK_REASON
    demoted: list[FieldResult] = []
    for row in results:
        if row.verdict is not Verdict.MISSING:
            demoted.append(row)
            continue
        demoted.append(
            FieldResult(
                field=row.field,
                verdict=Verdict.UNREADABLE,
                extracted=None,
                expected=row.expected,
                confidence=0.0,
                rationale=(
                    f"Not found on the pictures that could be read — but one of the "
                    f"pictures could not be read at all, so this may simply be on the "
                    f"part nobody has seen. {note}"
                ),
                tier=row.tier,
                evidence=row.evidence,
                findings=row.findings,
            )
        )
    return demoted


def _priced(cost: Cost, model: str) -> Cost:
    """Fill in `usd` from the tokens and the model that actually served the request.

    `response.usage.model` rather than the configured model: it is the one that ran, and
    on a request the provider downgraded or a fixture served they are not the same thing.
    """
    cost.usd = timing.usd_for(cost, model)
    return cost


def verify(
    application: Application,
    images: list[ImageInput],
    provider: ExtractionProvider,
    *,
    adjudicator: adjudicate_mod.Adjudicator | None = None,
    config: Config | None = None,
    unseen: tuple[str, ...] = (),
) -> VerificationResult:
    """Run one application through the pipeline.

    `adjudicator` is Tier 3 and defaults to None, which is what every caller passes today
    — no adapter implements the protocol. Passing None is a stated skip rather than a
    silent one: the outcome records `no adjudicator`, so a log can tell "there were no
    gray cases" apart from "nothing was wired up".

    `unseen` carries the retake reasons for photographs the pre-gate refused while OTHERS
    were read. It is not cosmetic: while any image went unread, no field may be reported
    Missing, because "it is not on the label" is a claim about the whole label and part of
    it was never looked at. See `_demote_missing_when_unseen`.
    """
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

    # LP-325. A second look at the fields that were read badly, from a crop of the region
    # they were found in — before the merge, so the comparison runs once on the best
    # reading available rather than being redone against a patched result.
    #
    # It sits HERE, not after compare, for the same reason the pre-gate sits before
    # extraction: the cheapest place to fix a reading is before anything has reasoned
    # about it. And it can only improve a row — see `api/reread.py` for every path that
    # discards a second reading rather than trusting it.
    settings_for_reread = config or Config()
    if settings_for_reread.reread_enabled:
        spent = int((time.perf_counter() - started) * 1000)
        second = reread_mod.reread(
            response.extractions,
            images,
            provider,
            commodity=application.commodity,
            budget=reread_mod.Budget(
                remaining_ms=max(0, settings_for_reread.request_budget_ms - spent)
            ),
        )
        if second.considered:
            applog.log(
                "reread",
                considered=second.considered,
                judged=second.reread,
                changed=second.improved,
                status=second.skipped_reason or "ran",
                duration_ms=second.elapsed_ms,
            )
        response = replace(
            response,
            extractions=second.extractions,
            usage=ProviderUsage(
                input_tokens=response.usage.input_tokens + second.usage.input_tokens,
                output_tokens=response.usage.output_tokens + second.usage.output_tokens,
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_creation_tokens=response.usage.cache_creation_tokens,
                model=response.usage.model,
            ),
        )

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

    # Before anything else looks at these verdicts. A field the model did not find is
    # only Missing if the model saw the whole label.
    if unseen:
        results = _demote_missing_when_unseen(results, unseen)

    # Tier 3, AFTER the merge and before the aggregate — it needs the final per-field
    # verdicts to know which rows are gray, and the recommendation has to be computed
    # from whatever it decided. What it is allowed to decide is narrow: Mismatch to
    # Acceptable variation on four fields, never the warning, never anything else.
    #
    # The budget is what is left of the request, not a fresh allowance. Extraction has
    # already spent most of the deadline by the time this runs, which is exactly why the
    # check happens before the call rather than after it.
    settings = config or Config()
    spent_ms = int((time.perf_counter() - started) * 1000)
    judgement = adjudicate_mod.adjudicate(
        results,
        adjudicator=adjudicator,
        budget=adjudicate_mod.Budget(
            remaining_ms=max(0, settings.request_budget_ms - spent_ms),
            remaining_usd=max(0.0, settings.max_usd_per_verification),
        ),
        commodity=application.commodity.value,
    )
    results = judgement.results
    timings.adjudicate = judgement.elapsed_ms if judgement.judged else None

    if judgement.considered:
        applog.log(
            "adjudication",
            considered=judgement.considered,
            judged=judgement.judged,
            changed=judgement.changed,
            status=judgement.skipped_reason or "ran",
            duration_ms=judgement.elapsed_ms,
        )

    aggregate = agg.recommend(results)
    timings.compare = int((time.perf_counter() - compare_started) * 1000)
    timings.total = int((time.perf_counter() - started) * 1000)

    return VerificationResult(
        request_id=request_id,
        aggregate=aggregate,
        fields=agg.triage_order(results),
        images=[],
        timings_ms=timings,
        # Priced HERE, where the Cost is built, rather than by the caller.
        #
        # It used to be priced in `api/routes/verify.py` after this function returned, so
        # every caller that was not that route got tokens with `usd = 0.0`. The batch
        # worker calls this function directly, so a 22-application batch reported 40,507
        # input tokens and $0.00 — a cost line that reads as free rather than as unknown,
        # on the one number OPS-4 exists to report.
        cost=_priced(
            Cost(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_creation_tokens=response.usage.cache_creation_tokens,
            ),
            response.usage.model,
        ),
    )
