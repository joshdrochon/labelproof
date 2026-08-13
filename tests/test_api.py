"""HTTP integration tests (LP-089). Fixture providers only — no socket is ever opened.

Every test here drives the real app through the real stack: multipart parsing, ingest,
quality scoring, the rules engine, and the error handlers. The only thing swapped out is
the model call, which is what ENG-3 requires — CI passes offline or it is not CI.
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import logging as applog
from api import main as main_mod
from api.config import Config
from api.main import create_app
from api.models import Recommendation, Verdict
from api.provider.base import ExtractionRequest, ExtractionResponse
from api.provider.fake import FailingProvider, SpecBackedProvider
from api.routes import batch as batch_routes
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


def declare(client: TestClient, path: str, length: int) -> Any:
    """Post a one-byte body that *claims* to be `length` bytes.

    The whole-request ceiling reads `Content-Length` and answers before the body is
    buffered, which is the behaviour under test. Actually sending a gigabyte to assert
    that we refuse a gigabyte would make the suite unrunnable.
    """
    return client.post(
        path,
        content=b"x",
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(length),
        },
    )


def test_an_impossible_upload_is_refused_before_it_is_buffered() -> None:
    """A 2GB post should cost a header read, not a disk spool."""
    client = make_client(max_image_bytes=1024, max_images=1)
    response = declare(client, "/verify", 1)
    # Sanity: a normal-sized body still reaches the route's own validation.
    assert response.status_code == 400

    huge = declare(client, "/verify", 500 * 1024 * 1024)
    assert huge.status_code == 400
    assert huge.json()["error"]["code"] == "file_too_large"


def test_the_verify_ceiling_names_the_verify_limit() -> None:
    huge = declare(make_client(), "/verify", 500 * 1024 * 1024)
    message = huge.json()["error"]["message"]
    assert "4 images of up to 10 MB each" in message
    assert "batch" not in message.lower()


def test_a_real_batch_upload_is_not_refused_by_the_verify_ceiling(tmp_path: Path) -> None:
    """The blocker this test exists for.

    A 300-application dump is roughly 600 photographs — over a gigabyte. The ceiling used
    to be `max_images x max_image_bytes + 1 MB` = 41 MB on every path, so the real thing
    this feature was built for was refused before it reached a route, and the refusal told
    the agent to "send at most 4 images". The prototype passed only because every fixture
    is kilobytes.
    """
    client = make_client(storage_dir=str(tmp_path))
    response = declare(client, "/batch", 600 * 1024 * 1024)

    assert response.status_code == 400  # malformed multipart, having got past the ceiling
    code = response.json()["error"]["code"]
    assert code not in ("file_too_large", "batch_too_large"), (
        "a 600 MB batch was refused by a size ceiling"
    )


def test_the_batch_ceiling_still_refuses_the_impossible_and_names_the_batch_limit(
    tmp_path: Path,
) -> None:
    """Path-aware does not mean unbounded — a 4 GB post still costs a header read."""
    client = make_client(storage_dir=str(tmp_path))
    response = declare(client, "/batch", 4 * 1024 * 1024 * 1024)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "batch_too_large"
    assert error["next_step"] == "reduce"
    # The refusal names the limit for the path that refused, not the other mode's.
    assert "2 GB" in error["message"]
    assert "4 images" not in error["message"]


def test_the_verify_ceiling_is_unchanged_by_the_batch_one() -> None:
    """Verify Now keeps the tight cap. Loosening it there is how a 2GB post gets spooled."""
    client = make_client(max_image_bytes=1024 * 1024, max_images=2)
    assert declare(client, "/verify", 50 * 1024 * 1024).json()["error"]["code"] == (
        "file_too_large"
    )


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
    """In sample mode /ready must NOT name a model — it is not calling one.

    Naming one would let an operator, or a grader without a key, read a simulated
    verdict as a real check. See tests/test_sample_mode_fails_closed.py.
    """
    body = make_client().get("/ready").json()
    assert body["simulated"] is True
    assert body["status"] == "sample_mode"
    assert "sample mode" in body["model"].lower()

def test_ready_is_503_when_the_service_is_not_configured() -> None:
    client = make_client(warnings=["ANTHROPIC_API_KEY is not set."])
    response = client.get("/ready")
    assert response.status_code == 503

    error = response.json()["error"]
    assert error["kind"] == "provider"
    assert "ANTHROPIC_API_KEY" not in error["message"]


def test_ready_stays_green_when_the_only_finding_is_an_advisory() -> None:
    """An advisory must never take the service out of rotation.

    This is not a style preference about status payloads. On Fly a critical `/ready`
    stops the proxy routing anything, so whatever fails this endpoint switches the whole
    deployment off. The first live deploy put the documented PERF-1 gap in `warnings`,
    and the public URL answered 503 to every request — including `/health` — while the
    process was up and behaving exactly as designed. The operator-facing message was
    "this service is not finished being set up", which was false and sent whoever read
    it looking for a missing environment variable that was present the whole time.

    Asserted through the route rather than on `Config`, because the defect was in what
    `/ready` chose to fail on, not in how the note was worded.
    """
    client = make_client(advisories=["Slower than the PERF-1 target."])
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["advisories"] == ["Slower than the PERF-1 target."]


def test_the_shipped_configuration_is_routable() -> None:
    """The real production config — live provider, real model — must pass `/ready`.

    The one above proves an advisory is survivable in isolation. This proves the config
    we actually deploy generates no `warnings` at all, which is the thing that was
    untrue. `use_fake_provider=False` with a key present is exactly what runs on Fly.
    """
    config = Config(use_fake_provider=False, anthropic_api_key="sk-ant-test")

    assert config.warnings == []
    assert config.exceeds_latency_target, (
        "Sonnet 5 is slower than the 5s target, so this test is only meaningful while "
        "the gap exists — it is the advisory's whole reason for being."
    )


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
    """OPS-5 — the front end has one error renderer, so there is one error shape.

    Scoped to API paths. A path outside them is a client-side route and must render the
    app, not an error, or reloading the page mid-workflow looks like a crash.
    """
    response = make_client().get("/sample/nope")
    # 404, not 400: an unknown address now keeps its own status. `kind` still groups by
    # who can act (this is the caller's to fix), but the status line is the status line.
    # See tests/regression/test_routing_defects.py.
    assert response.status_code == 404
    assert "error" in response.json()


def test_unknown_non_api_routes_render_the_app_not_an_error() -> None:
    """A browser reload on a client-side route must reach the SPA (only when built)."""
    from api.main import _WEB_DIST

    response = make_client().get("/some/client/route")
    if _WEB_DIST.is_dir():
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
    else:
        assert response.status_code == 404

def test_a_get_on_verify_is_explained() -> None:
    response = make_client().get("/verify")
    # 405, not 400. `_install_spa` raises 405 deliberately to say "wrong verb, not wrong
    # URL"; the handler used to discard it. Both halves are asserted: the status for
    # machines, the code for the error renderer.
    assert response.status_code == 405
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


# --- app wiring (LP-073) ----------------------------------------------------------------
#
# Everything below drives the app `create_app` actually builds. A feature that exists in a
# module and is never mounted is indistinguishable, from the agent's seat, from a feature
# that was never written — and both of the blockers this section covers shipped green
# because the tests reached around `create_app` instead of through it.


def built_spa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand in a built `web/dist`, which is the shipped configuration.

    The SPA catch-all only exists once the UI has been built, so the route-ordering bug is
    invisible to a suite that runs without it. This makes it visible.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>LabelProof</title>")
    monkeypatch.setattr(main_mod, "_WEB_DIST", dist)
    return dist


def test_the_batch_router_is_reachable_over_http(tmp_path: Path) -> None:
    """`GET /batch/{id}` must answer as batch, not as an unknown address."""
    response = make_client(storage_dir=str(tmp_path)).get("/batch/job_does_not_exist")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "batch_not_found"


def test_batch_stays_reachable_once_the_spa_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocker: the batch router must be mounted BEFORE `_install_spa`.

    Starlette matches in registration order, so a `GET /{path:path}` catch-all registered
    first out-ranks a real route. With batch mounted after it — or not mounted at all —
    `GET /batch/{id}` answers "that address is not part of this tool" while the endpoint
    sits there unreachable, and every batch an agent started becomes unreadable the moment
    the UI is built into the image.
    """
    built_spa(tmp_path, monkeypatch)
    client = make_client(storage_dir=str(tmp_path / "data"))

    # The SPA really is live: a client-side route renders the app.
    assert "text/html" in client.get("/some/client/route").headers["content-type"]

    response = client.get("/batch/job_does_not_exist")
    assert "text/html" not in response.headers["content-type"]
    assert response.json()["error"]["code"] == "batch_not_found"

    template = client.get("/batch/manifest-template.csv")
    assert template.status_code == 200
    assert "text/csv" in template.headers["content-type"]


def route_paths(app: Any) -> list[str]:
    """Every path in the table, in match order.

    FastAPI wraps an included router in a single opaque entry, so reading `.path` off the
    top-level routes silently misses every API route. Flattening is what makes an ordering
    assertion mean anything.
    """
    paths: list[str] = []
    for route in app.router.routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            paths.extend(str(getattr(inner, "path", "")) for inner in included.routes)
        else:
            paths.append(str(getattr(route, "path", "")))
    return paths


def test_the_batch_routes_are_registered_ahead_of_the_spa(tmp_path: Path) -> None:
    """The ordering invariant itself, so a reorder fails here and not in production."""
    paths = route_paths(create_app(config=make_config(storage_dir=str(tmp_path))))
    # `_install_spa` always registers "/" last, built UI or not.
    assert paths.index("/batch/{job_id}") < paths.index("/")
    assert paths.index("/batch") < paths.index("/")


def test_verify_now_is_announced_to_the_shared_provider_budget() -> None:
    """`create_app` must call `install_verify_priority` (BATCH-9, PERF-5).

    Nothing else in the process knows a single verification is in flight, so without this
    middleware batch never stands aside and the priority lane is dead code that still
    passes its own unit tests. The observable proof is that the budget saw the request.
    """
    client = make_client()
    assert post_verify(client).status_code == 200

    budget = getattr(client.app.state, "provider_budget", None)
    assert budget is not None, "no provider budget: the priority middleware is not installed"
    assert budget.interactive_seen >= 1


def test_the_priority_middleware_covers_the_sample_route_too() -> None:
    client = make_client()
    assert client.get("/sample").status_code == 200
    budget = getattr(client.app.state, "provider_budget", None)
    assert budget is not None and budget.interactive_seen >= 1


def test_batch_traffic_is_not_marked_as_interactive(tmp_path: Path) -> None:
    """The rule is asymmetric on purpose: batch yields to Verify Now, not the reverse."""
    client = make_client(storage_dir=str(tmp_path))
    client.get("/batch/job_does_not_exist")
    budget = getattr(client.app.state, "provider_budget", None)
    assert budget is None or budget.interactive_seen == 0


def test_the_interactive_paths_are_routes_that_exist() -> None:
    """A typo in INTERACTIVE_PATHS is a silent loss of the priority rule."""
    mounted = set(route_paths(create_app(config=make_config())))
    assert mounted.issuperset(batch_routes.INTERACTIVE_PATHS)

# --- PERF-1: the API layer's own latency (LP-090) ---------------------------------------


def api_overhead_ms(body: dict[str, Any]) -> int:
    """Everything the request spent that was not the model call.

    Ingest, quality scoring, the rules engine — the part of PERF-1 this codebase controls.

    Two things this number is not. It is measured server-side and stamped before the
    response is serialized and before either middleware unwinds, so it runs a few
    milliseconds narrower than a client's wall clock; that is the right choice for
    gating our own work and the wrong number to quote as a round trip. And against the
    fixture provider `extract` is 0, so here the subtraction is a no-op and this is simply
    the total — `test_the_overhead_measurement_excludes_the_model_call` is what proves the
    subtraction does its job when there is something to subtract.
    """
    timings = body["timings_ms"]
    return int(timings["total"]) - int(timings["extract"])


#: Ceiling on our own overhead for a two-image verification, p95 over a warm process.
#:
#: MEASURED IN THREE PLACES, and they are far enough apart that one number cannot serve:
#:
#:     developer laptop, 30 samples   124-134 ms
#:     GitHub Actions runner          330-354 ms
#:     Fly shared-cpu-2x (production) ~570 ms
#:
#: A flat 300 ms was set from the laptop figure alone and turned CI red on every commit
#: for eight commits. Raising it globally to clear CI would have cost the tightness that
#: makes it useful locally: the regression it exists to catch is an accidental re-decode,
#: which is roughly 3x, and 3x of the laptop baseline is ~400 ms — inside a CI-safe
#: ceiling and therefore invisible.
#:
#: So it scales with the machine. Tight where the measurement is stable, loose where the
#: box is shared and the number says more about the runner than about this code.
API_OVERHEAD_CEILING_MS = 900 if os.environ.get("CI") else 300


def test_the_api_layer_stays_under_its_share_of_the_five_second_budget() -> None:
    """LP-090, PERF-1 — with the provider stubbed, so this measures OUR overhead only.

    READ THIS BEFORE QUOTING IT. The provider here is a fixture that answers instantly.
    A green run says the ingest → quality → rules → serialization path is fast; it says
    NOTHING about whether the deployed p95 is under five seconds, because the model call
    is the dominant term and it is not in this measurement. A real Opus 5 extraction was
    measured at 9.6s median and Haiku 4.5 at 5.5s, both of which blow the 5s budget on
    their own. The deployed claim belongs to LP-144, timed against the deployed URL with
    a live model.

    What this test can honestly gate is the only part we control, and it gates it tightly.
    """
    client = make_client()
    post_verify(client, roles=["front", "back"])  # warm the process; PERF-6 is LP-144's

    overheads = [
        api_overhead_ms(post_verify(client, roles=["front", "back"]).json())
        for _ in range(12)
    ]
    p95 = sorted(overheads)[-2]
    assert p95 < API_OVERHEAD_CEILING_MS, (
        f"API-layer overhead p95 is {p95}ms against a {API_OVERHEAD_CEILING_MS}ms ceiling "
        f"(all samples: {overheads})"
    )


def test_the_overhead_measurement_excludes_the_model_call() -> None:
    """Guards the measurement itself: a slow provider must not inflate the API number.

    Without this, the ceiling test could be made to pass by mis-attributing provider time,
    or could start failing because someone swapped in a slower fixture — neither of which
    is a fact about our overhead.
    """
    client = make_client(
        provider=SlowProvider(by_name("tc16_front_back")),
        request_budget_ms=4000,
        provider_timeout_ms=3000,
    )
    body = post_verify(client).json()

    assert body["timings_ms"]["extract"] >= 900, "the slow provider was not actually slow"
    assert api_overhead_ms(body) < API_OVERHEAD_CEILING_MS


#: The pre-gated path does one image's ingest and quality scoring and then stops. Fifteen
#: samples ran 37-42 ms; 150 ms is ~3.5x that. Sharing API_OVERHEAD_CEILING_MS here would
#: have been meaningless — a path that measures 38 ms told to stay under 300 is not being
#: measured at all.
PREGATE_CEILING_MS = 150


def test_a_pre_gated_request_is_faster_still() -> None:
    """The cheap path must stay cheap: no model call, and no wait for one.

    Cheapness is the entire argument for the pre-gate, so it is worth a number rather than
    an adjective. Refusing an unreadable image has to cost visibly less than checking a
    readable one, or the gate is only saving tokens and not time.
    """
    client = make_client(provider=FailingProvider())
    files = [("images", ("dark.png", png_bytes((2, 2, 2)), "image/png"))]
    body = post_verify(client, files=files).json()

    assert body["timings_ms"]["total"] < PREGATE_CEILING_MS
    assert body["timings_ms"]["extract"] == 0, "the pre-gate let a model call through"


# --- ENG-2: the sample-to-verdict smoke (LP-116) ----------------------------------------

#: Deliberately NOT five seconds. Ten runs of the walk below took 0.142-0.167s, so an
#: `assert elapsed < 5.0` was an assertion nothing could trip — and worse, it was PERF-1's
#: headline number sitting green under a comment naming it. The docstring disclaimer is
#: the mitigation; the assertion is the hazard, and the assertion is what survives a grep
#: in six months. This is ~4.5x the worst observed: it catches a real regression on the
#: sample walk and it cannot be mistaken for evidence that PERF-1 holds in production.
E2E_CEILING_SECONDS = 0.75


def test_sample_to_verdict_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LP-116, ENG-2 — the grader's whole first minute, in fixture mode.

    The walk is the real one: load the page, ask for the sample, fetch both images, post
    them to /verify, read a recommendation. Every hop goes over HTTP through the app the
    container serves, with the SPA present, and the only thing swapped out is the model
    (ENG-3 — CI passes offline or it is not CI). The wiring is the point; the timing
    assertion is a floor on this walk and is deliberately not PERF-1's five seconds.

    NOTHING HERE IS THE DEPLOYED p95. The provider is a fixture that answers instantly, so
    the ceiling bounds our own overhead plus four round trips through a test harness, not
    the model. Live extraction was measured at 9.6s median on Opus 5 and 5.5s on Haiku
    4.5; the 5-second budget does not hold against either without the split concurrent
    call, and proving it against the deployed URL is LP-144's job, not this test's. A
    green run here means the app is wired end to end and adds nothing meaningful to the
    model's time. It does not mean PERF-1 is met in production, and it must never be
    quoted as though it did — which is why the number below is not five seconds.
    """
    built_spa(tmp_path, monkeypatch)
    client = make_client(storage_dir=str(tmp_path / "data"))

    started = time.perf_counter()

    page = client.get("/")
    assert page.status_code == 200

    sample = client.get("/sample")
    assert sample.status_code == 200
    offer = sample.json()

    files = [
        ("images", (image["filename"], client.get(image["url"]).content, "image/png"))
        for image in offer["images"]
    ]
    response = post_verify(
        client,
        files=files,
        application=offer["application"],
        roles=[image["role"] for image in offer["images"]],
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    body = response.json()
    assert body["aggregate"]["recommendation"] == Recommendation.READY_TO_APPROVE.value
    assert body["aggregate"]["rationale"]
    assert len(body["fields"]) == 7
    assert body["request_id"] == response.headers["X-Request-ID"]

    assert elapsed < E2E_CEILING_SECONDS, (
        f"sample to verdict took {elapsed:.2f}s in fixture mode, against a "
        f"{E2E_CEILING_SECONDS:g}s ceiling — this is a floor on our own work, not PERF-1"
    )


# --------------------------------------------------------------------------------------
# The sample picker (LP-088, LP-308, UX-1)
# --------------------------------------------------------------------------------------


def test_the_demo_offers_every_verdict_shape_rather_than_one_clean_pass() -> None:
    """A single passing sample demonstrates one verdict of six.

    The demo is the first thing a reviewer touches, and until now it opened on manual
    entry — nine fields of the exact drudgery this tool exists to remove — with one clean
    sample beside it. Four samples put a pass, a value disagreement, a typography defect
    and an absent warning within one click each.
    """
    body = make_client().get("/sample").json()
    slugs = [case["slug"] for case in body["cases"]]

    assert len(slugs) >= 4, slugs
    assert slugs[0] == "clean", "the first click must be a pass, not a rejection"
    assert body["slug"] == "clean", "a bare GET /sample must still run a working demo"


@pytest.mark.parametrize(
    "slug", ["clean", "abv-mismatch", "title-case-warning", "missing-warning"]
)
def test_every_offered_sample_loads_with_its_images(slug: str) -> None:
    client = make_client()
    body = client.get(f"/sample?case={slug}").json()

    assert body["application"]["brand_name"]
    assert body["images"], f"{slug} offers no image"
    for image in body["images"]:
        assert client.get(image["url"]).status_code == 200


def test_the_samples_are_read_from_the_golden_set_not_restated() -> None:
    """A demo that drifted from the set would show behaviour nothing tests.

    Asserted by comparing the served application against `golden/set.json` rather than
    against a copy in the test, so regenerating the fixtures cannot leave the demo behind.
    """
    golden = {
        entry["name"]: entry
        for entry in json.loads((ROOT / "golden" / "set.json").read_text())["fixtures"]
    }
    body = make_client().get("/sample?case=abv-mismatch").json()

    assert body["application"] == golden["tc08_abv_mismatch"]["application"]


def test_an_unknown_sample_is_refused_rather_than_silently_defaulted() -> None:
    """Falling back to the clean sample would show a pass for a case nobody asked for."""
    response = make_client().get("/sample?case=../../etc/passwd")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_sample"


def test_only_images_the_samples_declare_are_servable() -> None:
    """The allowlist is derived from the manifest, never assembled from the request.

    `tc01_old_tom_clean.png` is a real fixture that exists on disk and is NOT one of the
    four the samples declare — which is the case worth testing, because a route that
    joined the name onto a directory would happily serve it.
    """
    client = make_client()
    assert (ROOT / "fixtures" / "labels" / "tc01_old_tom_clean.png").is_file()

    refused = client.get("/sample/images/tc01_old_tom_clean.png")
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "unknown_sample_image"


@pytest.mark.parametrize(
    "encoded",
    ["%2e%2e%2f%2e%2e%2fetc%2fpasswd", "..%2F..%2Fetc%2Fpasswd"],
)
def test_a_traversal_that_survives_url_normalisation_still_gets_nothing(
    encoded: str,
) -> None:
    """Percent-encoded, so it actually arrives at the route.

    The obvious version of this test — `GET /sample/images/../../../etc/passwd` — proves
    nothing: the HTTP client resolves the dots before the request is sent, the path
    becomes `/etc/passwd`, and the SPA catch-all answers 200 with the app shell. It looked
    like a traversal succeeding and was neither a traversal nor a success.
    """
    assert make_client().get(f"/sample/images/{encoded}").status_code == 404


# --------------------------------------------------------------------------------------
# The mixed-quality upload (LP-321, IMG-4) — found by audit, and it was a false finding
# --------------------------------------------------------------------------------------


def _blurred(name: str, sigma: float = 12.0) -> bytes:
    import cv2

    image = cv2.imread(str(LABELS / name))
    ok, buffer = cv2.imencode(".png", cv2.GaussianBlur(image, (0, 0), sigma))
    assert ok
    return bytes(buffer.tobytes())


def test_an_unreadable_second_image_never_makes_a_field_missing() -> None:
    """The defect this closes returned a compliant label for correction.

    Measured before the fix: front sharp, back too poor to read. The pre-gate dropped the
    back — the panel carrying the warning and the producer — sent the front to the model,
    and reported `government_warning: missing` with the rationale "no government warning
    statement was found on **any** of the supplied images". There were two images. One was
    never looked at.

    `pregated` was only true when EVERY image failed, so the mixed case took the silent
    path — and the mixed case is much the more likely one, because agents send a front and
    a back and usually only one of them is bad.

    Missing is a finding against the LABEL and grounds to return an application.
    Unreadable is a statement about the PHOTOGRAPH.
    """
    spec = by_name("tc16_front_back")
    client = make_client(provider=SpecBackedProvider(spec))
    response = client.post(
        "/verify",
        files=[
            ("images", (FRONT, (LABELS / FRONT).read_bytes(), "image/png")),
            ("images", (BACK, _blurred(BACK), "image/png")),
        ],
        data={"application": json.dumps(spec.application()), "roles": ["front", "back"]},
    )
    body = response.json()

    qualities = [image["quality"]["verdict"] for image in body["images"]]
    assert "hopeless" in qualities, "the fixture stopped being unreadable; re-blur it"
    assert "ok" in qualities, "both images failed — that is the all-or-nothing case"

    verdicts = {field["field"]: field["verdict"] for field in body["fields"]}
    assert verdicts["government_warning"] != "missing", (
        "a panel that was never read cannot make the warning Missing"
    )
    assert verdicts["government_warning"] == "unreadable"
    assert body["aggregate"]["recommendation"] != "return_for_correction"


def test_the_agent_is_told_which_picture_went_unread() -> None:
    """"Something was wrong somewhere" is not actionable. The rationale has to name the
    retake reason, or the agent cannot tell which photograph to ask for again."""
    spec = by_name("tc16_front_back")
    client = make_client(provider=SpecBackedProvider(spec))
    body = client.post(
        "/verify",
        files=[
            ("images", (FRONT, (LABELS / FRONT).read_bytes(), "image/png")),
            ("images", (BACK, _blurred(BACK), "image/png")),
        ],
        data={"application": json.dumps(spec.application()), "roles": ["front", "back"]},
    ).json()

    warning = next(f for f in body["fields"] if f["field"] == "government_warning")
    assert "could not be read" in warning["rationale"]


def test_a_defect_on_a_readable_image_survives_an_unreadable_one() -> None:
    """The demotion must not become an amnesty.

    An unread second photograph does not excuse a defect visible on the first. If a bad
    upload could wash out a real Mismatch, this fix would have replaced a false finding
    with a false pass — which is the worse trade by the whole design of this product.
    """
    spec = by_name("tc08_abv_mismatch")
    client = make_client(provider=SpecBackedProvider(spec))
    body = client.post(
        "/verify",
        files=[
            (
                "images",
                (
                    "tc08_abv_mismatch.png",
                    (LABELS / "tc08_abv_mismatch.png").read_bytes(),
                    "image/png",
                ),
            ),
            ("images", (BACK, _blurred(BACK), "image/png")),
        ],
        data={"application": json.dumps(spec.application())},
    ).json()

    verdicts = {field["field"]: field["verdict"] for field in body["fields"]}
    assert verdicts["alcohol_content"] == "mismatch", (
        "the ABV mismatch was visible on an image we DID read and must stand"
    )
