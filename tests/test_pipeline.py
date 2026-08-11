"""End-to-end extraction pipeline on the real fixture images — LP-072.

Every test in this module starts from bytes on disk and runs the whole path a request
takes: sniff and sanitize (`api.pipeline.ingest`), score (`api.pipeline.quality`), extract
(an offline provider), merge across images (`api.pipeline.merge`), compare, and aggregate.
The only substitution is the model call, which is what ENG-3 requires — the suite passes
with no network or it is not CI.

The `no_network` fixture below enforces that **for this module only**, and it is scoped to
each test rather than to the session. It is a check on these tests, not a suite-wide
guarantee; the suite-wide claim rests on the fake providers and on `Config.use_fake_provider`.
A session-wide guard belongs in `tests/conftest.py`, which this branch does not own.

The unit suites already cover each stage in isolation. What only an integration test can
catch is a seam: an image index that survives ingest and is lost at merge, a role that is
never passed through, a two-image application that verifies differently depending on which
provider call returned first.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from api.config import Config
from api.models import (
    Application,
    Commodity,
    ExtractedField,
    Extraction,
    FieldName,
    Recommendation,
    Verdict,
    WarningTypography,
)
from api.pipeline import ingest as ingest_module
from api.pipeline import merge as merge_module
from api.pipeline import quality as quality_module
from api.provider.base import (
    ExtractionRequest,
    ExtractionResponse,
    ImageInput,
    ProviderUsage,
)
from api.provider.fake import SpecBackedProvider, spec_name_for_image
from api.verify import verify
from fixtures.generator.catalog import by_name

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "fixtures" / "labels"
GOLDEN = json.loads((ROOT / "golden" / "set.json").read_text())

WARNING = FieldName.GOVERNMENT_WARNING
BRAND = FieldName.BRAND_NAME


# --- ENG-3: prove the offline claim rather than asserting it ---------------------------


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket connection from a test in this module is a failure, not a slow test.

    Autouse and function-scoped, so it covers every test here and nothing elsewhere.
    """

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "The extraction pipeline opened a network connection. Tests run against "
            "fixtures only (ENG-3)."
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


# --- helpers ---------------------------------------------------------------------------


def config() -> Config:
    """A config that cannot reach a provider even if something tried."""
    return Config(use_fake_provider=True)


def role_of(filename: str) -> str:
    """`tc16_front_back_back.png` is the back of a two-image application."""
    stem = Path(filename).stem
    for face in ("front", "back"):
        if stem.endswith(f"_{face}"):
            return face
    return "single"


def prepare(filenames: list[str]) -> list[ImageInput]:
    """Bytes on disk through the real ingest and quality stages, ready for extraction.

    This is the part an integration test exists for: `ingest` renumbers images as it
    expands them, and everything downstream — provenance, evidence, the picture an agent
    is sent to — hangs off the index it assigns.

    Roles are mapped back onto the expanded list rather than zipped onto it. Every fixture
    today is a single-page PNG, so a `zip(..., strict=True)` would work and would then
    raise the first time somebody adds a PDF fixture — one file, several images. Walking
    the page markers costs two lines and does not have a trap in it.
    """
    raw = [(LABELS / name).read_bytes() for name in filenames]
    ingested = ingest_module.ingest(raw, config())

    images: list[ImageInput] = []
    source = -1
    for image in ingested:
        # `page` is None for a plain image and 0 for the first page of a PDF. Either marks
        # the start of the next uploaded file.
        if image.page in (None, 0):
            source += 1
        name = filenames[source]

        assessment = quality_module.assess(ingest_module.to_array(image))
        assert not quality_module.should_skip_extraction(assessment), (
            f"{name} scored hopeless before any model call; the fixture regressed"
        )
        images.append(
            ImageInput(
                index=image.index,
                data=image.data,
                media_type=image.media_type,
                role=role_of(name),
            )
        )

    assert source == len(filenames) - 1, "every uploaded file must produce at least one image"
    return images


def application_of(entry: dict[str, Any]) -> Application:
    return Application(**entry["application"])


def fixtures() -> list[dict[str, Any]]:
    return list(GOLDEN["fixtures"])


def row(result: Any, field: FieldName) -> Any:
    return next(f for f in result.fields if f.field is field)


TC16 = next(f for f in fixtures() if f["name"] == "tc16_front_back")


# --- every fixture, all the way through ------------------------------------------------


@pytest.mark.parametrize("entry", fixtures(), ids=lambda e: e["name"])
def test_every_fixture_runs_end_to_end(entry: dict[str, Any]) -> None:
    """Bytes in, a complete verification out — for all fifteen generated labels.

    Deliberately shallow on verdicts: `eval/run.py` owns accuracy against the golden set.
    This asserts the pipeline holds together and always produces a full, well-formed
    answer, which is the failure mode a per-stage unit test cannot see.
    """
    images = prepare(entry["images"])
    provider = SpecBackedProvider(by_name(entry["name"]))
    result = verify(application_of(entry), images, provider)

    assert [f.field for f in result.fields] != []
    assert {f.field for f in result.fields} == set(FieldName)
    assert result.aggregate.recommendation in set(Recommendation)
    assert all(f.rationale for f in result.fields), "every row must explain itself"


@pytest.mark.parametrize("entry", fixtures(), ids=lambda e: e["name"])
def test_ingest_sanitizes_every_fixture(entry: dict[str, Any]) -> None:
    """SEC-3: what reaches the provider is pixels this process drew, with no metadata."""
    ingested = ingest_module.ingest(
        [(LABELS / name).read_bytes() for name in entry["images"]], config()
    )
    assert [i.index for i in ingested] == list(range(len(entry["images"])))
    assert all(i.metadata_stripped for i in ingested)
    assert all(i.data.startswith(b"\x89PNG\r\n\x1a\n") for i in ingested)


@pytest.mark.parametrize("entry", fixtures(), ids=lambda e: e["name"])
def test_every_reported_value_was_actually_read_off_an_image(entry: dict[str, Any]) -> None:
    """LP-067 end to end: nothing in `extracted` that no picture reported.

    An earlier version of this test asserted `extracted is None` on Unreadable, Missing
    and Not-applicable rows. That cannot fail — `compare._missing`, `_unreadable` and
    `_not_applicable` all hardcode `extracted=None`, so it restated the implementation
    instead of checking it. This compares what the pipeline reports against what the
    provider actually produced, which is the claim LP-067 makes and the one that can break:
    any normalisation, repair, or carry-over between images shows up here as a value the
    extraction never contained.
    """
    images = prepare(entry["images"])
    provider = SpecBackedProvider(by_name(entry["name"]))

    extracted_by_provider = {
        field.value
        for extraction in provider.extract(
            ExtractionRequest(commodity=Commodity(entry["commodity"]), images=images)
        ).extractions
        for field in extraction.fields.values()
        if field.value is not None
    }

    result = verify(application_of(entry), images, provider)
    reported = {f.extracted for f in result.fields if f.extracted is not None}

    assert reported, "a clean fixture must report something, or this test is vacuous"
    assert reported <= extracted_by_provider, (
        f"reported values no image produced: {sorted(reported - extracted_by_provider)}"
    )


@pytest.mark.tc("TC-14")
def test_an_illegible_label_reports_nothing_and_approves_nothing() -> None:
    """The complement, and the half that can regress: every field unreadable.

    A fabricated value would have to come from somewhere, and the somewhere is a pipeline
    that fills a gap rather than reporting it. Nothing may be filled in, and nothing that
    was never read may reach Ready to approve.
    """
    entry = next(f for f in fixtures() if f["name"] == "tc01_old_tom_clean")
    provider = SpecBackedProvider(by_name("tc01_old_tom_clean"), illegible=set(FieldName))
    result = verify(application_of(entry), prepare(entry["images"]), provider)

    assert all(f.extracted is None for f in result.fields)
    assert all(f.verdict is Verdict.UNREADABLE for f in result.fields)
    assert result.aggregate.recommendation is not Recommendation.READY_TO_APPROVE


# --- TC-16: the two-image case -----------------------------------------------------------


@pytest.mark.tc("TC-16")
def test_two_images_verify_as_one_label() -> None:
    """The brand is on the front and the warning is on the back. Both must be found."""
    images = prepare(TC16["images"])
    result = verify(
        application_of(TC16), images, SpecBackedProvider(by_name("tc16_front_back"))
    )

    assert len(images) == 2
    assert row(result, BRAND).verdict is Verdict.MATCH
    assert row(result, WARNING).verdict is Verdict.MATCH
    assert result.aggregate.recommendation is Recommendation.READY_TO_APPROVE


@pytest.mark.tc("TC-16")
def test_per_field_provenance_sends_the_agent_to_the_right_picture() -> None:
    """IMG-8. The evidence overlay is useless if it points at the wrong photograph."""
    images = prepare(TC16["images"])
    result = verify(
        application_of(TC16), images, SpecBackedProvider(by_name("tc16_front_back"))
    )

    # Brand on the front, producer and warning on the back — three rows, two pictures.
    expected = {BRAND: 0, FieldName.PRODUCER: 1, WARNING: 1}
    for field, index in expected.items():
        evidence = row(result, field).evidence
        assert evidence is not None, f"{field.value} has no evidence to point at"
        assert evidence.image_index == index, f"{field.value} points at the wrong picture"


@pytest.mark.tc("TC-16")
def test_the_front_alone_reports_the_warning_missing() -> None:
    """The complement. With only the front, the statement genuinely is not there."""
    images = prepare(["tc16_front_back_front.png"])
    result = verify(
        application_of(TC16), images, SpecBackedProvider(by_name("tc16_front_back"))
    )
    assert row(result, WARNING).verdict is Verdict.MISSING
    assert result.aggregate.recommendation is Recommendation.RETURN_FOR_CORRECTION


@pytest.mark.tc("TC-16")
def test_image_order_does_not_change_the_verdicts() -> None:
    """Images are extracted concurrently; whichever call returns first must not matter."""
    forward = verify(
        application_of(TC16),
        prepare(TC16["images"]),
        SpecBackedProvider(by_name("tc16_front_back")),
    )
    reversed_ = verify(
        application_of(TC16),
        prepare(TC16["images"]),
        _ReversingProvider(by_name("tc16_front_back")),
    )

    assert _verdicts(forward) == _verdicts(reversed_)
    assert _provenance(forward) == _provenance(reversed_)


def _verdicts(result: Any) -> dict[FieldName, Verdict]:
    return {f.field: f.verdict for f in result.fields}


def _provenance(result: Any) -> dict[FieldName, int | None]:
    return {
        f.field: (f.evidence.image_index if f.evidence else None) for f in result.fields
    }


class _ReversingProvider:
    """The real spec provider, handing its extractions back in the opposite order.

    A provider is free to return whatever order its thread pool finished in. This one
    guarantees the awkward order so the property is exercised rather than hoped for.
    """

    name = "fake:reversed"

    def __init__(self, spec: Any) -> None:
        self._inner = SpecBackedProvider(spec)

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        response = self._inner.extract(request)
        response.extractions.reverse()
        return response


# --- conflicts, end to end ---------------------------------------------------------------


class _DisagreeingProvider:
    """Two pictures of the same label that read one field differently.

    The realistic cause is mundane: a crease, a reflection, or a second image of a
    different bottle slipped into the same application.
    """

    name = "fake:disagreeing"

    def __init__(self, field: FieldName, first: str, second: str, *, confidence: float) -> None:
        self.field = field
        self.first = first
        self.second = second
        self.confidence = confidence

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        base = SpecBackedProvider(by_name("tc16_front_back")).extract(request)
        values = [self.first, self.second]
        for extraction, value in zip(base.extractions, values, strict=False):
            extraction.fields[self.field] = ExtractedField(
                value=value, confidence=self.confidence, legible=True
            )
        return ExtractionResponse(
            extractions=base.extractions,
            usage=ProviderUsage(model="fake:disagreeing"),
        )


def _run_conflict(field: FieldName, first: str, second: str, confidence: float = 0.9) -> Any:
    return verify(
        application_of(TC16),
        prepare(TC16["images"]),
        _DisagreeingProvider(field, first, second, confidence=confidence),
    )


@pytest.mark.tc("TC-16")
def test_a_conflicted_field_routes_to_needs_review() -> None:
    result = _run_conflict(BRAND, "OLD TOM DISTILLERY", "OLDE TOWNE DISTILLERY")
    assert row(result, BRAND).verdict is Verdict.UNREADABLE
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


@pytest.mark.tc("TC-16")
def test_a_conflicted_field_is_never_silently_approved() -> None:
    """The failure this ticket exists to prevent.

    One picture agrees with the application and the other does not. Taking the confident
    reading would return Ready to approve on a label nobody has actually established the
    contents of.
    """
    result = _run_conflict(BRAND, "OLD TOM DISTILLERY", "OLDE TOWNE DISTILLERY")
    assert result.aggregate.recommendation is not Recommendation.READY_TO_APPROVE
    assert row(result, BRAND).extracted is None


@pytest.mark.tc("TC-16")
def test_a_conflicted_field_is_not_returned_for_correction_either() -> None:
    """Flag, never block. A reading we do not trust is no basis for a rejection."""
    result = _run_conflict(BRAND, "OLD TOM DISTILLERY", "OLDE TOWNE DISTILLERY")
    assert result.aggregate.recommendation is not Recommendation.RETURN_FOR_CORRECTION


@pytest.mark.tc("TC-16")
def test_the_agent_is_shown_both_readings() -> None:
    result = _run_conflict(BRAND, "OLD TOM DISTILLERY", "OLDE TOWNE DISTILLERY")
    brand = row(result, BRAND)
    assert "OLD TOM DISTILLERY" in brand.rationale
    assert "OLDE TOWNE DISTILLERY" in brand.rationale
    assert [f.code for f in brand.findings] == [merge_module.CONFLICT_CODE]


@pytest.mark.tc("TC-16")
def test_a_conflicted_warning_does_not_pass_on_the_more_confident_picture() -> None:
    """WARN-6 fails closed, and the merge gives it no leniency it does not give others."""
    canonical = by_name("tc16_front_back").rendered_warning()
    result = _run_conflict(WARNING, canonical.replace("GOVERNMENT WARNING", "Government Warning"),
                           canonical)
    assert row(result, WARNING).verdict is Verdict.UNREADABLE
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


@pytest.mark.tc("TC-16")
def test_two_pictures_that_merely_style_a_brand_differently_still_match() -> None:
    """The other half of the judgment: not every difference is a disagreement (TC-02)."""
    result = _run_conflict(BRAND, "OLD TOM DISTILLERY", "Old Tom Distillery")
    assert row(result, BRAND).verdict is Verdict.MATCH
    assert result.aggregate.recommendation is Recommendation.READY_TO_APPROVE


# --- the two false passes this branch shipped and then fixed --------------------------------


class _TypographyDisagreementProvider:
    """Two photographs of the same back panel that disagree about how it is printed.

    Same statement, word for word, on both. One picture says the heading is not bold —
    a WARN-2 violation — and the sharper picture says it is.
    """

    name = "fake:typography-disagreement"

    def __init__(self, *, confident_says_bold: bool) -> None:
        self.confident_says_bold = confident_says_bold

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        spec = by_name("tc16_front_back")
        statement = spec.rendered_warning()
        base = SpecBackedProvider(spec).extract(request)

        for offset, extraction in enumerate(base.extractions):
            bold = self.confident_says_bold if offset else not self.confident_says_bold
            extraction.fields[WARNING] = ExtractedField(
                value=statement, confidence=0.90 + 0.05 * offset, legible=True
            )
            extraction.warning_text = statement
            extraction.warning_typography = WarningTypography(
                header_is_all_caps=True, header_is_bold=bold, body_is_bold=False
            )
        return base


@pytest.mark.tc("TC-16")
@pytest.mark.parametrize("confident_says_bold", [True, False])
def test_pictures_that_disagree_about_typography_do_not_settle_it_by_confidence(
    confident_says_bold: bool,
) -> None:
    """WARN-2 must not be decided by which photograph was sharper.

    Parametrized both ways on purpose. Only one direction is a false pass, but a fix that
    happens to work because the violation was the confident reading is not a fix.
    """
    result = verify(
        application_of(TC16),
        prepare(TC16["images"]),
        _TypographyDisagreementProvider(confident_says_bold=confident_says_bold),
    )
    assert row(result, WARNING).verdict is not Verdict.MATCH
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW
    assert any(
        f.code == "warning_header_bold_unverified" for f in row(result, WARNING).findings
    )


class _StrayNonLabelProvider:
    """The artwork has no warning on it. Something else in the upload does.

    A carton photo, a marketing one-sheet, a printout of the regulation itself — the
    extractor flags it `is_label=False` and still reads text off it.
    """

    name = "fake:stray-non-label"

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        spec = by_name("tc16_front_back")
        base = SpecBackedProvider(spec).extract(request)

        artwork = base.extractions[0]
        artwork.fields.pop(WARNING, None)
        artwork.warning_text = None
        artwork.warning_typography = WarningTypography()

        stray = base.extractions[1]
        stray.is_label = False
        stray.fields[WARNING] = ExtractedField(
            value=spec.rendered_warning(), confidence=0.99, legible=True
        )
        stray.warning_text = spec.rendered_warning()
        stray.warning_typography = WarningTypography(
            header_is_all_caps=True, header_is_bold=True, body_is_bold=False
        )
        return base


@pytest.mark.tc("TC-15")
def test_a_warning_on_a_non_label_image_does_not_answer_for_the_label() -> None:
    """The worst outcome this product can produce: no warning on the label, approved.

    TC-15 covers the case where nothing uploaded is a label. This is the mixed upload,
    which is likelier and quieter — the response looks completely normal.
    """
    result = verify(
        application_of(TC16), prepare(TC16["images"]), _StrayNonLabelProvider()
    )
    assert row(result, WARNING).verdict is Verdict.MISSING
    assert result.aggregate.recommendation is Recommendation.RETURN_FOR_CORRECTION
    assert result.aggregate.driving_field is WARNING


# --- unreadable on one picture, readable on the other -------------------------------------


class _GlareOnOneImageProvider:
    """TC-12 across two pictures: the back is fine, the front has glare on the same field."""

    name = "fake:glare-one-image"

    def __init__(self, field: FieldName) -> None:
        self.field = field

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        base = SpecBackedProvider(by_name("tc16_front_back")).extract(request)
        for extraction in base.extractions:
            if self.field not in extraction.fields:
                # The field is not on this picture at all. Leaving it out is the whole
                # point — reporting it as unreadable here would invent a defect.
                continue
            extraction.fields[self.field] = ExtractedField(
                value=None, confidence=0.0, legible=False
            )
            break
        return base


@pytest.mark.tc("TC-12")
def test_a_field_readable_on_one_picture_is_not_unreadable() -> None:
    images = prepare(TC16["images"])
    two_readings = [
        Extraction(
            image_index=0,
            fields={BRAND: ExtractedField(value=None, confidence=0.0, legible=False)},
        ),
        Extraction(
            image_index=1,
            fields={
                BRAND: ExtractedField(value="OLD TOM DISTILLERY", confidence=0.6, legible=True)
            },
        ),
    ]
    label = merge_module.merge(two_readings)
    assert label.fields[BRAND].value == "OLD TOM DISTILLERY"
    assert label.provenance(BRAND) == 1
    assert len(images) == 2


@pytest.mark.tc("TC-12")
def test_glare_on_the_front_leaves_the_back_verified() -> None:
    """Per-field honesty across pictures: one bad region does not condemn the label."""
    result = verify(
        application_of(TC16),
        prepare(TC16["images"]),
        _GlareOnOneImageProvider(BRAND),
    )
    assert row(result, BRAND).verdict is Verdict.UNREADABLE
    assert row(result, WARNING).verdict is Verdict.MATCH


# --- the fixture set itself ---------------------------------------------------------------


def test_the_fixture_images_all_exist() -> None:
    """If `fixtures/labels` is empty, run `python -m fixtures.generator.build`."""
    missing = [
        name
        for entry in fixtures()
        for name in entry["images"]
        if not (LABELS / name).exists()
    ]
    assert missing == []


def test_image_filenames_map_back_to_their_spec() -> None:
    """The recorder and the offline providers both key off this mapping."""
    for entry in fixtures():
        for name in entry["images"]:
            assert spec_name_for_image(name) == entry["name"]
