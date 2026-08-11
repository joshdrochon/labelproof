"""End-to-end verification against generated fixtures, with no model in the loop.

The last section is about `api.verify`'s other half: the pre-model path both entry points
share. It is tested here rather than in `test_api.py` or `test_batch.py` because the
property under test only exists across the two — that Verify Now and Batch spend the same
zero tokens on artwork nobody could read.
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api.batch.store import BatchStore
from api.batch.worker import process
from api.batch.models import ItemState
from api.config import Config
from api.main import create_app
from api.models import Application, Commodity, FieldName, Recommendation, Verdict
from api.provider.base import ExtractionRequest, ExtractionResponse, ImageInput
from api.provider.fake import (
    FailingProvider,
    NonLabelProvider,
    SpecBackedProvider,
    spec_name_for_image,
)
from api.provider.base import ProviderError
from api.verify import prepare_images, verify
from fixtures.generator.catalog import by_name

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "fixtures" / "labels"
SAMPLE = ROOT / "assets" / "samples" / "old_tom.json"

READABLE_LABEL = "tc01_old_tom_clean.png"


def images(n: int = 1, roles: list[str] | None = None) -> list[ImageInput]:
    roles = roles or ["single"] * n
    return [ImageInput(index=i, data=b"", role=r) for i, r in enumerate(roles)]


def application_for(name: str, **overrides: object) -> Application:
    spec = by_name(name)
    producer_name, _, producer_address = spec.producer.partition(", ")
    base = {
        "commodity": Commodity(spec.commodity),
        "brand_name": spec.brand_name,
        "class_type": spec.class_type,
        "alcohol_content": 45.0 if spec.commodity == "spirits" else None,
        "net_contents": spec.net_contents,
        "producer_name": producer_name,
        "producer_address": producer_address,
        "country_of_origin": spec.country_of_origin,
        "is_import": False,
    }
    base.update(overrides)
    return Application(**base)  # type: ignore[arg-type]


def run(name: str, **overrides: object):
    spec = by_name(name)
    return verify(application_for(name, **overrides), images(1), SpecBackedProvider(spec))


# --- TC-01 ------------------------------------------------------------------------------

@pytest.mark.tc("TC-01")
def test_clean_label_is_ready_to_approve() -> None:
    result = run("tc01_old_tom_clean")
    assert result.aggregate.recommendation is Recommendation.READY_TO_APPROVE
    assert all(f.verdict in (Verdict.MATCH, Verdict.NOT_APPLICABLE) for f in result.fields)


@pytest.mark.tc("TC-01")
def test_clean_label_returns_all_seven_fields() -> None:
    assert len(run("tc01_old_tom_clean").fields) == 7


def test_warning_is_listed_first() -> None:
    assert run("tc01_old_tom_clean").fields[0].field is FieldName.GOVERNMENT_WARNING


# --- defects ------------------------------------------------------------------------------

@pytest.mark.tc("TC-02")
def test_stones_throw_is_acceptable_variation() -> None:
    result = run("tc02_stones_throw", brand_name="Stone's Throw")
    brand = next(f for f in result.fields if f.field is FieldName.BRAND_NAME)
    assert brand.verdict is Verdict.ACCEPTABLE_VARIATION
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


@pytest.mark.tc("TC-03")
def test_title_case_warning_returns_for_correction() -> None:
    result = run("tc03_title_case_warning")
    assert result.aggregate.recommendation is Recommendation.RETURN_FOR_CORRECTION
    assert result.aggregate.driving_field is FieldName.GOVERNMENT_WARNING


@pytest.mark.tc("TC-04")
def test_bold_warning_body_returns_for_correction() -> None:
    assert run("tc04_bold_warning_body").aggregate.recommendation is (
        Recommendation.RETURN_FOR_CORRECTION
    )


@pytest.mark.tc("TC-05")
def test_reworded_warning_returns_for_correction() -> None:
    assert run("tc05_reworded_warning").aggregate.recommendation is (
        Recommendation.RETURN_FOR_CORRECTION
    )


@pytest.mark.tc("TC-07")
def test_missing_warning_returns_for_correction() -> None:
    result = run("tc07_missing_warning")
    warning = next(f for f in result.fields if f.field is FieldName.GOVERNMENT_WARNING)
    assert warning.verdict is Verdict.MISSING
    assert result.aggregate.recommendation is Recommendation.RETURN_FOR_CORRECTION


@pytest.mark.tc("TC-08")
def test_abv_mismatch_needs_review() -> None:
    result = run("tc08_abv_mismatch")
    abv = next(f for f in result.fields if f.field is FieldName.ALCOHOL_CONTENT)
    assert abv.verdict is Verdict.MISMATCH
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


@pytest.mark.tc("TC-10")
def test_non_standard_fill_matches_and_still_flags() -> None:
    result = run("tc10_non_standard_fill", net_contents="733 mL")
    net = next(f for f in result.fields if f.field is FieldName.NET_CONTENTS)
    assert net.verdict is Verdict.MATCH
    assert any(f.code == "non_standard_fill" for f in net.findings)


@pytest.mark.tc("TC-17")
def test_table_wine_alcohol_content_is_not_applicable() -> None:
    result = run("tc17_table_wine", alcohol_content=None)
    abv = next(f for f in result.fields if f.field is FieldName.ALCOHOL_CONTENT)
    assert abv.verdict is Verdict.NOT_APPLICABLE


@pytest.mark.tc("TC-18")
def test_malt_alcohol_content_is_not_applicable() -> None:
    result = run("tc18_malt_no_abv", alcohol_content=None)
    abv = next(f for f in result.fields if f.field is FieldName.ALCOHOL_CONTENT)
    assert abv.verdict is Verdict.NOT_APPLICABLE


@pytest.mark.tc("TC-19")
def test_import_without_origin_is_missing() -> None:
    result = run("tc19_import_missing_origin", is_import=True, country_of_origin="France")
    origin = next(f for f in result.fields if f.field is FieldName.COUNTRY_OF_ORIGIN)
    assert origin.verdict is Verdict.MISSING


# --- TC-16: multi-image ------------------------------------------------------------------

@pytest.mark.tc("TC-16")
def test_warning_on_the_back_is_found() -> None:
    """Declaring Missing without searching every image is a false finding."""
    spec = by_name("tc16_front_back")
    result = verify(
        application_for("tc16_front_back"),
        images(2, roles=["front", "back"]),
        SpecBackedProvider(spec),
    )
    warning = next(f for f in result.fields if f.field is FieldName.GOVERNMENT_WARNING)
    assert warning.verdict is not Verdict.MISSING


@pytest.mark.tc("TC-16")
def test_front_only_cannot_see_the_warning() -> None:
    """The complement: with only the front, the warning genuinely is not there."""
    spec = by_name("tc16_front_back")
    result = verify(application_for("tc16_front_back"), images(1, roles=["front"]),
                    SpecBackedProvider(spec))
    warning = next(f for f in result.fields if f.field is FieldName.GOVERNMENT_WARNING)
    assert warning.verdict is Verdict.MISSING


# --- TC-12 / TC-15 / TC-21 ---------------------------------------------------------------

@pytest.mark.tc("TC-12")
def test_glare_over_the_warning_is_unreadable_not_missing() -> None:
    spec = by_name("tc01_old_tom_clean")
    provider = SpecBackedProvider(spec, illegible={FieldName.GOVERNMENT_WARNING})
    result = verify(application_for("tc01_old_tom_clean"), images(1), provider)
    warning = next(f for f in result.fields if f.field is FieldName.GOVERNMENT_WARNING)
    assert warning.verdict is Verdict.UNREADABLE
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


@pytest.mark.tc("TC-12")
def test_glare_on_the_warning_leaves_other_fields_verified() -> None:
    """Per-field honesty: one bad region does not condemn the whole label."""
    spec = by_name("tc01_old_tom_clean")
    provider = SpecBackedProvider(spec, illegible={FieldName.GOVERNMENT_WARNING})
    result = verify(application_for("tc01_old_tom_clean"), images(1), provider)
    brand = next(f for f in result.fields if f.field is FieldName.BRAND_NAME)
    assert brand.verdict is Verdict.MATCH


@pytest.mark.tc("TC-15")
def test_non_label_image_is_handled_gracefully() -> None:
    result = verify(application_for("tc01_old_tom_clean"), images(1), NonLabelProvider())
    assert result.fields == []
    assert "does not look like a label" in result.aggregate.rationale


@pytest.mark.tc("TC-21")
def test_provider_down_raises_a_typed_error_not_a_crash() -> None:
    with pytest.raises(ProviderError) as exc:
        verify(application_for("tc01_old_tom_clean"), images(1), FailingProvider())
    assert exc.value.retryable


# --- invariants ---------------------------------------------------------------------------

def test_no_verification_ever_fabricates_a_value() -> None:
    """LP-067 across the whole pipeline, not just the comparators."""
    spec = by_name("tc01_old_tom_clean")
    provider = SpecBackedProvider(spec, illegible=set(FieldName))
    result = verify(application_for("tc01_old_tom_clean"), images(1), provider)
    for field in result.fields:
        if field.verdict is Verdict.UNREADABLE:
            assert field.extracted is None


def test_every_result_carries_timings() -> None:
    assert run("tc01_old_tom_clean").timings_ms.total >= 0


def test_image_filename_maps_back_to_its_fixture() -> None:
    assert spec_name_for_image("tc03_title_case_warning_back.png") == "tc03_title_case_warning"
    assert spec_name_for_image("tc01_old_tom_clean.png") == "tc01_old_tom_clean"


# --- the shared pre-model path (LP-321) ---------------------------------------------------
#
# `prepare_images` is the one copy of ingest -> quality -> pre-gate. It used to be written
# twice, and the second copy — in the batch worker — is where the gate would quietly be
# lost: an importer dump is the least-watched surface in the product and precisely where
# hopeless artwork arrives by the hundred. These tests assert the property across both
# entry points at once, because that is the only place it can be false.


class CountingProvider:
    """A fixture-backed provider that counts every model call it is asked to make."""

    name = "fake:counting"

    def __init__(self, inner: Any):
        self.inner = inner
        self.calls = 0
        self._lock = threading.Lock()

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        with self._lock:
            self.calls += 1
        return self.inner.extract(request)


def hopeless_png() -> bytes:
    """A near-black frame — the photo taken with a thumb over the lens."""
    buffer = io.BytesIO()
    Image.new("RGB", (1000, 1400), (2, 2, 2)).save(buffer, format="PNG")
    return buffer.getvalue()


def readable_png() -> bytes:
    return (LABELS / READABLE_LABEL).read_bytes()


def old_tom_application() -> dict[str, Any]:
    raw = json.loads(SAMPLE.read_text())
    return {key: value for key, value in raw.items() if not key.startswith("_")}


def verify_now_calls(image: bytes, filename: str, tmp_path: Path) -> tuple[int, Any]:
    """Drive `POST /verify` over HTTP and report (model calls, response body)."""
    provider = CountingProvider(SpecBackedProvider(by_name("tc01_old_tom_clean")))
    app = create_app(
        config=Config(use_fake_provider=True, storage_dir=str(tmp_path / "verify")),
        provider=provider,
    )
    client = TestClient(app)
    response = client.post(
        "/verify",
        files=[("images", (filename, image, "image/png"))],
        data={"application": json.dumps(old_tom_application())},
    )
    assert response.status_code == 200, response.text
    return provider.calls, response.json()


def batch_calls(image: bytes, filename: str, tmp_path: Path) -> tuple[int, Any]:
    """Drive one batch item through the worker and report (model calls, stored result).

    The result is serialized so both helpers hand back the same shape — the point of these
    tests is that the two paths agree, and comparing a model to a JSON body would hide a
    difference rather than surface one.
    """
    provider = CountingProvider(SpecBackedProvider(by_name("tc01_old_tom_clean")))
    config = Config(use_fake_provider=True, storage_dir=str(tmp_path / "batch"))
    store = BatchStore(tmp_path / "batch")
    job = store.create_job()
    store.save_image(job.job_id, filename, image)
    store.add_items(
        job.job_id, [(2, Application.model_validate(old_tom_application()), [filename])]
    )
    item = store.claim()
    assert item is not None

    state = process(item, store, config, provider)
    assert state is ItemState.DONE, "the pre-gate produced a failure instead of a verdict"
    stored = store.get_item(item.item_id)
    assert stored is not None and stored.result is not None
    return provider.calls, stored.result.model_dump(mode="json")


@pytest.mark.tc("TC-13")
def test_a_hopeless_image_costs_zero_model_calls_on_both_paths(tmp_path: Path) -> None:
    """LP-321 — the pre-gate holds on Verify Now AND on batch, or it does not hold.

    The counter is the assertion. A verdict of Unreadable proves the agent was told the
    truth; only a call count of zero proves it cost nothing, and a batch of 300 hopeless
    photographs is where that difference is measured in dollars.
    """
    interactive_calls, body = verify_now_calls(hopeless_png(), "dark.png", tmp_path)
    assert interactive_calls == 0
    assert body["aggregate"]["recommendation"] == Recommendation.NEEDS_REVIEW.value
    assert {field["verdict"] for field in body["fields"]} == {Verdict.UNREADABLE.value}

    queued_calls, result = batch_calls(hopeless_png(), "dark.png", tmp_path)
    assert queued_calls == 0
    assert result["aggregate"]["recommendation"] == Recommendation.NEEDS_REVIEW.value
    assert {field["verdict"] for field in result["fields"]} == {Verdict.UNREADABLE.value}


def test_both_paths_explain_a_pre_gated_label_in_the_same_words(tmp_path: Path) -> None:
    """An agent who re-uploads a batch item through Verify Now must hear the same thing."""
    _, body = verify_now_calls(hopeless_png(), "dark.png", tmp_path)
    _, result = batch_calls(hopeless_png(), "dark.png", tmp_path)
    assert body["aggregate"]["rationale"] == result["aggregate"]["rationale"]
    assert "retake" in body["aggregate"]["rationale"].lower()


def test_a_readable_image_does_reach_the_model_on_both_paths(tmp_path: Path) -> None:
    """The control. Without it, "zero calls" would also pass on a pipeline that is broken."""
    interactive_calls, body = verify_now_calls(readable_png(), READABLE_LABEL, tmp_path)
    assert interactive_calls == 1
    assert body["aggregate"]["recommendation"] == Recommendation.READY_TO_APPROVE.value

    queued_calls, result = batch_calls(readable_png(), READABLE_LABEL, tmp_path)
    assert queued_calls == 1
    assert result["aggregate"]["recommendation"] == Recommendation.READY_TO_APPROVE.value


def test_prepare_images_reports_the_defect_that_refused_the_label() -> None:
    """A pre-gate that refuses without saying why is a dead end for the agent."""
    prepared = prepare_images([hopeless_png()], Config(use_fake_provider=True))
    assert prepared.pregated
    assert prepared.usable == []
    assert prepared.reason and prepared.reason.strip().endswith(".")
    assert len(prepared.reports) == 1
    assert prepared.reports[0].quality.verdict == "hopeless"


def test_prepare_images_keeps_the_readable_image_and_labels_its_face() -> None:
    prepared = prepare_images([readable_png()], Config(use_fake_provider=True))
    assert not prepared.pregated
    assert prepared.reason is None
    assert [image.role for image in prepared.usable] == ["single"]

    pair = prepare_images([readable_png(), readable_png()], Config(use_fake_provider=True))
    assert [image.role for image in pair.usable] == ["front", "back"]

    supplied = prepare_images(
        [readable_png(), readable_png()],
        Config(use_fake_provider=True),
        roles=["BACK ", "front"],
    )
    assert [image.role for image in supplied.usable] == ["back", "front"]


def test_one_hopeless_image_of_two_does_not_pre_gate_the_label(tmp_path: Path) -> None:
    """TC-16's shape: the back is unusable, the front still gets read."""
    prepared = prepare_images(
        [readable_png(), hopeless_png()], Config(use_fake_provider=True)
    )
    assert not prepared.pregated
    assert [image.index for image in prepared.usable] == [0]
    assert len(prepared.reports) == 2
