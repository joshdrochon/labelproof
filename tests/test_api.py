"""HTTP integration tests (LP-089). Fixture providers only — no socket is ever opened.

Every test here drives the real app through the real stack: multipart parsing, ingest,
quality scoring, the rules engine, and the error handlers. The only thing swapped out is
the model call, which is what ENG-3 requires — CI passes offline or it is not CI.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import logging as applog
from api.config import Config
from api.main import create_app
from api.models import Recommendation, Verdict
from api.provider.base import ExtractionRequest, ExtractionResponse
from api.provider.fake import FailingProvider, SpecBackedProvider
from fixtures.generator.catalog import by_name

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "fixtures" / "labels"
SAMPLE = ROOT / "assets" / "samples" / "old_tom.json"

FRONT = "tc16_front_back_front.png"
BACK = "tc16_front_back_back.png"


# --- helpers -------------------------------------------------------------------------

def make_config(**overrides: Any) -> Config:
    """A config that never needs a key, so no test can accidentally reach the network."""
    base: dict[str, Any] = {"use_fake_provider": True}
    base.update(overrides)
    return Config(**base)


def make_client(provider: Any = None, **config_overrides: Any) -> TestClient:
    app = create_app(config=make_config(**config_overrides), provider=provider)
    return TestClient(app)


def sample_application(**overrides: Any) -> dict[str, Any]:
    raw = json.loads(SAMPLE.read_text())
    application = {k: v for k, v in raw.items() if not k.startswith("_")}
    application.update(overrides)
    return application


def label_files(*names: str) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        ("images", (name, (LABELS / name).read_bytes(), "image/png")) for name in names
    ]


def png_bytes(color: tuple[int, int, int], size: tuple[int, int] = (1000, 1400)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def post_verify(
    client: TestClient,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    application: Any = None,
    roles: list[str] | None = None,
) -> Any:
    form: dict[str, Any] = {
        "application": application
        if isinstance(application, str)
        else json.dumps(application if application is not None else sample_application())
    }
    if roles:
        form["roles"] = roles
    return client.post(
        "/verify", files=files if files is not None else label_files(FRONT, BACK), data=form
    )


# --- happy path ----------------------------------------------------------------------

def test_verify_returns_a_verdict_for_the_demo_pair() -> None:
    """The one-click demo, end to end over HTTP."""
    response = post_verify(make_client(), roles=["front", "back"])
    assert response.status_code == 200

    body = response.json()
    assert body["aggregate"]["recommendation"] == Recommendation.READY_TO_APPROVE.value
    assert len(body["fields"]) == 7
    assert body["fields"][0]["field"] == "government_warning"


def test_verify_reports_every_uploaded_image_with_its_quality() -> None:
    body = post_verify(make_client(), roles=["front", "back"]).json()
    assert [image["index"] for image in body["images"]] == [0, 1]
    assert [image["role"] for image in body["images"]] == ["front", "back"]
    for image in body["images"]:
        assert image["quality"]["verdict"] in ("ok", "degraded", "hopeless")


def test_verify_reports_stage_timings() -> None:
    """LP-078 — an agent should never wonder where the time went."""
    timings = post_verify(make_client()).json()["timings_ms"]
    for stage in ("ingest", "quality", "extract", "compare", "total"):
        assert timings[stage] >= 0
    assert timings["total"] >= timings["ingest"]


def test_request_id_in_the_body_matches_the_header() -> None:
    """LP-077 — the ID on the screen is the ID in the logs."""
    response = post_verify(make_client())
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert response.headers["X-Request-ID"].startswith("req_")


def test_a_single_image_is_accepted() -> None:
    response = post_verify(make_client(), files=label_files("tc01_old_tom_clean.png"))
    assert response.status_code == 200
    assert response.json()["images"][0]["role"] == "single"


def test_a_defective_label_comes_back_as_a_correction() -> None:
    """TC-03 over HTTP: the API layer must not soften what the rules engine found."""
    spec = by_name("tc03_title_case_warning")
    client = make_client(provider=SpecBackedProvider(spec))
    body = post_verify(
        client,
        files=label_files("tc03_title_case_warning.png"),
        application=sample_application(),
    ).json()
    assert body["aggregate"]["recommendation"] == Recommendation.RETURN_FOR_CORRECTION.value


# --- request validation --------------------------------------------------------------

def test_unparseable_application_json_is_a_plain_400() -> None:
    response = post_verify(make_client(), application="{not json at all")
    assert response.status_code == 400

    error = response.json()["error"]
    assert error["kind"] == "user"
    assert error["code"] == "invalid_application_json"
    assert "application" in error["message"].lower()
    assert error["next_step"]


def test_incomplete_application_names_the_missing_field_in_plain_words() -> None:
    application = sample_application()
    del application["brand_name"]
    response = post_verify(make_client(), application=application)
    assert response.status_code == 400

    message = response.json()["error"]["message"]
    assert "brand name" in message
    assert "brand_name" not in message


def test_unknown_commodity_lists_the_allowed_ones() -> None:
    response = post_verify(make_client(), application=sample_application(commodity="cider"))
    assert response.status_code == 400
    assert "spirits" in response.json()["error"]["message"]


def test_a_non_multipart_post_is_explained_rather_than_rejected_silently() -> None:
    client = make_client()
    response = client.post("/verify", json=sample_application())
    assert response.status_code == 400
    assert response.json()["error"]["kind"] == "user"
    assert "label" in response.json()["error"]["message"].lower()


def test_missing_images_says_so() -> None:
    client = make_client()
    response = client.post("/verify", data={"application": json.dumps(sample_application())})
    assert response.status_code == 400
    assert "images" in response.json()["error"]["message"].lower()


def test_oversized_upload_is_rejected_with_a_size_to_aim_for() -> None:
    client = make_client(max_image_bytes=4096)
    response = post_verify(client, files=label_files(FRONT))
    assert response.status_code == 400

    error = response.json()["error"]
    assert error["code"] == "file_too_large"
    assert error["next_step"] == "resize"


def test_an_impossible_upload_is_refused_before_it_is_buffered() -> None:
    """A 2GB post should cost a header read, not a disk spool."""
    client = make_client(max_image_bytes=1024, max_images=1)
    response = client.post(
        "/verify",
        content=b"x",
        headers={"Content-Type": "multipart/form-data; boundary=x", "Content-Length": "1"},
    )
    # Sanity: a normal-sized body still reaches the route's own validation.
    assert response.status_code == 400

    huge = client.post(
        "/verify",
        content=b"x",
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(500 * 1024 * 1024),
        },
    )
    assert huge.status_code == 400
    assert huge.json()["error"]["code"] == "file_too_large"


def test_too_many_images_is_rejected_before_any_reading() -> None:
    client = make_client(max_images=2)
    response = post_verify(client, files=label_files(FRONT, BACK, "tc01_old_tom_clean.png"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "too_many_images"


def test_wrong_file_type_is_named_by_its_bytes_not_its_extension() -> None:
    """SEC-5 — the extension says .png and the tool is not fooled."""
    files = [("images", ("label.png", b"PK\x03\x04 not an image at all", "image/png"))]
    response = post_verify(make_client(), files=files)
    assert response.status_code == 400

    error = response.json()["error"]
    assert error["code"] == "unsupported_file_type"
    assert "zip" in error["message"] or "Word" in error["message"]


# --- provider down (TC-21) -----------------------------------------------------------

def test_provider_down_is_503_and_not_500() -> None:
    client = make_client(provider=FailingProvider())
    response = post_verify(client)
    assert response.status_code == 503
    assert response.json()["error"]["kind"] == "provider"


def test_provider_down_speaks_plain_language() -> None:
    client = make_client(provider=FailingProvider())
    message = post_verify(client).json()["error"]["message"]
    for jargon in ("Traceback", "Exception", "Connection refused", "provider", "inference"):
        assert jargon not in message
    assert "try again" in message.lower()
    assert "nothing has been checked" in message.lower()


# --- the pre-gate and the budget -----------------------------------------------------

def test_hopeless_image_never_reaches_the_provider() -> None:
    """LP-321 — a black frame costs zero model calls, and FailingProvider proves it."""
    client = make_client(provider=FailingProvider())
    files = [("images", ("dark.png", png_bytes((2, 2, 2)), "image/png"))]
    response = post_verify(client, files=files)

    assert response.status_code == 200
    body = response.json()
    assert body["aggregate"]["recommendation"] == Recommendation.NEEDS_REVIEW.value
    assert all(field["verdict"] == Verdict.UNREADABLE.value for field in body["fields"])
    assert "retake" in body["aggregate"]["rationale"].lower()


def test_hopeless_image_still_shows_the_application_side_of_every_row() -> None:
    client = make_client(provider=FailingProvider())
    files = [("images", ("dark.png", png_bytes((2, 2, 2)), "image/png"))]
    body = post_verify(client, files=files).json()
    brand = next(f for f in body["fields"] if f["field"] == "brand_name")
    assert brand["expected"] == sample_application()["brand_name"]
    assert brand["extracted"] is None


def test_an_exhausted_budget_returns_needs_review_rather_than_blowing_the_deadline() -> None:
    """LP-079 — over budget is a partial answer, never a hang and never a 504."""
    client = make_client(request_budget_ms=1, provider_timeout_ms=0)
    response = post_verify(client)

    assert response.status_code == 200
    body = response.json()
    assert body["aggregate"]["recommendation"] == Recommendation.NEEDS_REVIEW.value
    assert all(field["verdict"] == Verdict.UNREADABLE.value for field in body["fields"])
    assert "not verified" in body["aggregate"]["rationale"]


class SlowProvider(SpecBackedProvider):
    """A provider that answers correctly, but too late to be useful."""

    name = "fake:slow"

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        time.sleep(1.0)
        return super().extract(request)


def test_a_slow_provider_is_cut_off_at_the_budget() -> None:
    client = make_client(
        provider=SlowProvider(by_name("tc16_front_back")),
        request_budget_ms=400,
        provider_timeout_ms=200,
    )
    response = post_verify(client)

    assert response.status_code == 200
    body = response.json()
    assert body["aggregate"]["recommendation"] == Recommendation.NEEDS_REVIEW.value
    # Server-side elapsed, not wall clock: TestClient tears down a fresh event loop per
    # request and waits on the abandoned worker thread, which a running server does not.
    assert body["timings_ms"]["total"] < 1000


# --- health and readiness ------------------------------------------------------------

def test_health_is_up_and_touches_nothing() -> None:
    assert make_client().get("/health").json() == {"status": "ok"}


def test_health_is_up_even_when_the_provider_is_down() -> None:
    """A liveness check that fails on somebody else's outage gets the container killed."""
    client = make_client(provider=FailingProvider())
    assert client.get("/health").status_code == 200


def test_ready_reports_the_configured_model() -> None:
    response = make_client().get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["model"] == "claude-opus-5"


def test_ready_is_503_when_the_service_is_not_configured() -> None:
    client = make_client(warnings=["ANTHROPIC_API_KEY is not set."])
    response = client.get("/ready")
    assert response.status_code == 503

    error = response.json()["error"]
    assert error["kind"] == "provider"
    assert "ANTHROPIC_API_KEY" not in error["message"]


# --- the sample (LP-088) --------------------------------------------------------------

def test_sample_serves_the_old_tom_application() -> None:
    body = make_client().get("/sample").json()
    assert body["application"]["brand_name"] == "OLD TOM DISTILLERY"
    assert body["application"]["commodity"] == "spirits"
    assert "_source" not in body["application"]
    assert body["note"]


def test_sample_offers_a_front_and_a_back() -> None:
    images = make_client().get("/sample").json()["images"]
    assert [image["role"] for image in images] == ["front", "back"]
    assert all(image["url"].startswith("/sample/images/") for image in images)


def test_sample_images_are_servable() -> None:
    client = make_client()
    url = client.get("/sample").json()["images"][0]["url"]
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_sample_image_path_cannot_be_used_to_read_other_files() -> None:
    response = make_client().get("/sample/images/..%2F..%2Fapi%2Fconfig.py")
    assert response.status_code in (400, 404)
    assert "error" in response.json()


def test_the_sample_verifies_cleanly_end_to_end() -> None:
    """UX-1 — the grader clicks once and reaches a verdict."""
    client = make_client()
    body = client.get("/sample").json()
    files = [
        ("images", (image["filename"], client.get(image["url"]).content, "image/png"))
        for image in body["images"]
    ]
    response = post_verify(
        client, files=files, application=body["application"],
        roles=[image["role"] for image in body["images"]],
    )
    assert response.status_code == 200
    assert response.json()["aggregate"]["recommendation"] == (
        Recommendation.READY_TO_APPROVE.value
    )


# --- error shape everywhere ------------------------------------------------------------

def test_unknown_routes_answer_in_the_taxonomy() -> None:
    """OPS-5 — the front end has one error renderer, so there is one error shape."""
    response = make_client().get("/nope")
    assert response.status_code == 400
    assert response.json()["error"]["kind"] == "user"
    assert response.json()["error"]["code"] == "not_found"


def test_a_get_on_verify_is_explained() -> None:
    response = make_client().get("/verify")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_an_unexpected_failure_becomes_a_sentence_not_a_traceback() -> None:
    class Exploding:
        name = "fake:exploding"

        def extract(self, request: ExtractionRequest) -> ExtractionResponse:
            raise RuntimeError("index out of range in extract()")

    app = create_app(config=make_config(), provider=Exploding())
    client = TestClient(app, raise_server_exceptions=False)
    response = post_verify(client)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["kind"] == "internal"
    assert "index out of range" not in error["message"]
    assert "no application data has been changed" in error["message"]


def test_the_index_page_does_not_crash_without_a_built_ui() -> None:
    assert make_client().get("/").status_code == 200


# --- SEC-4 and LP-092 -------------------------------------------------------------------

def test_a_verification_writes_no_label_text_to_the_logs() -> None:
    """SEC-4 — the allowlist is the mechanism; this is the proof at the HTTP layer."""
    client = make_client()
    stream = io.StringIO()
    applog.configure(stream=stream)
    try:
        post_verify(client)
        written = stream.getvalue()
    finally:
        applog.configure()

    assert written
    for secret in ("OLD TOM", "Bardstown", "Kentucky Straight", "750 mL"):
        assert secret not in written


def test_the_request_path_keeps_no_state_between_processes() -> None:
    """LP-092 — a restart loses nothing, because nothing is kept."""
    first = post_verify(make_client()).json()
    second = post_verify(make_client()).json()

    assert first["request_id"] != second["request_id"]
    assert first["aggregate"] == second["aggregate"]
    assert [f["verdict"] for f in first["fields"]] == [f["verdict"] for f in second["fields"]]


def test_no_upload_is_written_to_disk(tmp_path: Path) -> None:
    storage = tmp_path / "data"
    client = make_client(storage_dir=str(storage))
    assert post_verify(client).status_code == 200
    assert not storage.exists()


@pytest.mark.tc("TC-21")
def test_provider_down_leaves_no_partial_verdict_behind() -> None:
    """A failed request must not look like a verified one to the next reader."""
    client = make_client(provider=FailingProvider())
    body = post_verify(client).json()
    assert "aggregate" not in body
    assert "fields" not in body
