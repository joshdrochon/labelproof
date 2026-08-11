"""Security headers, strict CORS, and the no-content log rule (SEC-4, SEC-6).

LP-082 and LP-086, plus the app-side half of LP-083 and the scan LP-256 runs in CI.

The centre of gravity here is `test_a_traceback_carrying_label_text_never_reaches_stdout`
and the tests around it. `api/logging.py` makes logging label content on purpose
impossible; it does nothing about a traceback, and a traceback is how it would actually
happen. Those tests capture the real file descriptors — `capfd`, not `caplog` — because the
leak being closed is bytes on stdout, and a test that reads the logging framework's own
buffers would pass while the bytes went out anyway.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

from api import logging as applog
from api import security
from api.config import Config
from api.main import create_app
from api.provider.base import ExtractionRequest, ExtractionResponse
from api.security import CONTENT_SECURITY_POLICY, SecurityPolicy, harden

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "fixtures" / "labels"
SAMPLE = ROOT / "assets" / "samples" / "old_tom.json"

#: A string that could only have come off a label. If this appears anywhere in captured
#: output, something bypassed the allowlist.
LABEL_TEXT = "STONE'S THROW KENTUCKY STRAIGHT BOURBON — GOVERNMENT WARNING: (1) According to"


@pytest.fixture(autouse=True)
def _containment_is_never_left_installed() -> Iterator[None]:
    """`harden` changes the process's log record factory. Always put it back.

    Without this, one test in this file would silently scrub tracebacks for every test that
    ran after it, in any file, which is the kind of cross-test coupling that turns a real
    failure into a mystery.
    """
    yield
    security.remove_log_containment()


def make_config(**overrides: Any) -> Config:
    base: dict[str, Any] = {"use_fake_provider": True}
    base.update(overrides)
    return Config(**base)


def hardened_app(*, routes: bool = True, **policy_env: Any) -> FastAPI:
    """A small app carrying the real security stack."""
    app = FastAPI()

    if routes:

        @app.get("/health")
        def health() -> JSONResponse:
            return JSONResponse({"ok": True})

        @app.get("/sample")
        def sample() -> JSONResponse:
            return JSONResponse({"ok": True})

        @app.post("/verify")
        def verify() -> JSONResponse:
            return JSONResponse({"ok": True})

        @app.get("/assets/index-abc123.js")
        def asset() -> PlainTextResponse:
            return PlainTextResponse("console.log(1)", media_type="text/javascript")

        @app.get("/boom")
        def boom() -> JSONResponse:
            raise RuntimeError(LABEL_TEXT)

        @app.get("/export.csv")
        def export() -> PlainTextResponse:
            return PlainTextResponse(
                "row\n1\n",
                media_type="text/csv",
                headers={"Cache-Control": "private, max-age=60"},
            )

    harden(app, make_config(**policy_env))
    return app


# --- security headers (LP-082, LP-256) ---------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("cross-origin-opener-policy", "same-origin"),
        ("cross-origin-resource-policy", "same-origin"),
        ("x-robots-tag", "noindex, nofollow"),
    ],
)
def test_every_response_carries_the_policy_headers(name: str, value: str) -> None:
    client = TestClient(hardened_app())
    assert client.get("/sample").headers[name] == value


def test_permissions_policy_denies_every_capability_the_app_does_not_use() -> None:
    client = TestClient(hardened_app())
    policy = client.get("/sample").headers["permissions-policy"]
    for capability in ("geolocation", "camera", "microphone", "usb", "payment"):
        assert f"{capability}=()" in policy


def test_headers_reach_the_error_paths_too() -> None:
    """A header policy with holes in the errors is a policy attackers read errors for."""
    client = TestClient(hardened_app(), raise_server_exceptions=False)
    for response in (client.get("/nope"), client.get("/boom")):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" in response.headers


def test_the_csp_forbids_inline_and_remote_script() -> None:
    """script-src is the one directive with no relaxation anywhere in this codebase."""
    parts = [part.strip() for part in CONTENT_SECURITY_POLICY.split(";")]
    directives = dict(part.split(" ", 1) for part in parts if " " in part)
    assert directives["script-src"] == "'self'"
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'unsafe-eval'" not in directives["script-src"]
    assert directives["default-src"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["base-uri"] == "'none'"
    assert directives["object-src"] == "'none'"
    assert directives["form-action"] == "'self'"


def test_the_csp_permits_no_external_host() -> None:
    """NET-1: the egress table is the README's, and the browser's is empty."""
    assert "http://" not in CONTENT_SECURITY_POLICY
    assert "https://" not in CONTENT_SECURITY_POLICY.replace("upgrade-insecure-requests", "")
    assert "*" not in CONTENT_SECURITY_POLICY


def test_the_csp_still_allows_what_the_spa_actually_does() -> None:
    """Blob previews and the inline styles the evidence overlay positions itself with."""
    assert "img-src 'self' blob: data:" in CONTENT_SECURITY_POLICY
    assert "worker-src 'self' blob:" in CONTENT_SECURITY_POLICY
    # Documented in the judgment log, not slipped in: EvidenceOverlay.tsx positions each
    # highlight with a React inline style attribute, which CSP3 blocks under a bare
    # style-src 'self' — silently, leaving every box stacked at the top-left corner.
    assert "style-src 'self' 'unsafe-inline'" in CONTENT_SECURITY_POLICY


def test_hsts_is_sent_over_https_and_withheld_over_plaintext() -> None:
    client = TestClient(hardened_app())
    assert "strict-transport-security" not in client.get("/sample").headers

    forwarded = client.get("/sample", headers={"X-Forwarded-Proto": "https"})
    assert forwarded.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_hsts_does_not_claim_a_preload_commitment() -> None:
    """preload is an apex-domain commitment to browser vendors; this app rents a subdomain."""
    client = TestClient(hardened_app())
    value = client.get("/sample", headers={"X-Forwarded-Proto": "https"}).headers[
        "strict-transport-security"
    ]
    assert "preload" not in value


def test_results_are_never_stored_by_a_cache_but_hashed_assets_are() -> None:
    """A verdict body carries extracted label text; an intermediary cache is undocumented
    retention (SEC-2)."""
    client = TestClient(hardened_app())
    assert client.post("/verify").headers["cache-control"] == "no-store"
    assert client.get("/assets/index-abc123.js").headers["cache-control"] == (
        "public, max-age=31536000, immutable"
    )


def test_a_route_that_set_its_own_header_keeps_it() -> None:
    """The CSV export knows something this middleware does not."""
    client = TestClient(hardened_app())
    assert client.get("/export.csv").headers["cache-control"] == "private, max-age=60"


def test_the_real_app_is_hardened_end_to_end() -> None:
    """Not a stand-in: `create_app` plus `harden`, exercising the shipped stack."""
    app = create_app(config=make_config())
    harden(app, make_config())
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


# --- strict CORS (LP-082) ----------------------------------------------------------------


def test_no_cors_header_is_ever_emitted_by_default() -> None:
    client = TestClient(hardened_app())
    response = client.get("/sample", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_a_foreign_origin_cannot_spend_a_model_call() -> None:
    """A browser blocks the read; without this the server has already done the work."""
    client = TestClient(hardened_app())
    response = client.post("/verify", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cross_origin_refused"


def test_the_apps_own_page_is_not_foreign_to_itself() -> None:
    """Browsers send Origin on same-origin POSTs too — getting this wrong breaks the app."""
    client = TestClient(hardened_app())
    response = client.post("/verify", headers={"Origin": "http://testserver"})
    assert response.status_code == 200


def test_same_origin_is_recognised_behind_tls_termination() -> None:
    """On Fly the scheme that reached this process is http; the browser used https."""
    client = TestClient(hardened_app())
    response = client.post(
        "/verify",
        headers={"Origin": "https://testserver", "X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 200


def test_a_request_with_no_origin_is_allowed() -> None:
    """curl, the deploy smoke test, and anything server-to-server send none."""
    client = TestClient(hardened_app())
    assert client.post("/verify").status_code == 200


def test_a_disallowed_preflight_is_refused_in_the_taxonomy() -> None:
    client = TestClient(hardened_app())
    response = client.options(
        "/verify",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["kind"] == "user"


def test_an_allowlisted_origin_gets_an_exact_echo_never_a_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LABELPROOF_ALLOWED_ORIGINS", "http://localhost:5173")
    client = TestClient(hardened_app())

    preflight = client.options(
        "/verify",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"

    allowed = client.post("/verify", headers={"Origin": "http://localhost:5173"})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_a_wildcard_in_the_allowlist_is_not_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowlist that reads `*` is not an allowlist."""
    monkeypatch.setenv("LABELPROOF_ALLOWED_ORIGINS", "*")
    client = TestClient(hardened_app())
    assert client.post("/verify", headers={"Origin": "https://evil.example"}).status_code == 403


def test_cross_origin_responses_vary_so_a_shared_cache_cannot_confuse_them() -> None:
    client = TestClient(hardened_app())
    assert "Origin" in client.get("/sample", headers={"Origin": "https://x.example"}).headers[
        "vary"
    ]


# --- traceback containment (LP-086, SEC-4) -----------------------------------------------


class ExplodingProvider:
    """A provider whose failure message is label text, which is the realistic case.

    Extraction responses are validated on receipt, and a pydantic `ValidationError` renders
    `input_value=...` — on that path, the label the model just read.
    """

    name = "exploding"

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        raise RuntimeError(LABEL_TEXT)


def _verify_payload() -> tuple[dict[str, Any], list[tuple[str, tuple[str, bytes, str]]]]:
    raw = json.loads(SAMPLE.read_text())
    application = {k: v for k, v in raw.items() if not k.startswith("_")}
    name = "tc01_old_tom_clean.png"
    files = [("images", (name, (LABELS / name).read_bytes(), "image/png"))]
    return application, files


def _uvicorn_error_logger() -> logging.Logger:
    """A stand-in for what uvicorn does with an exception that escapes the ASGI app.

    uvicorn's HTTP protocol wraps the call in `try/except` and answers with
    `logger.error("Exception in ASGI application", exc_info=exc)` on `uvicorn.error`, which
    writes the formatted traceback to stdout. TestClient is not a server and never does
    this, so a test that only drove TestClient would pass while the deployed app leaked —
    this reproduces the missing half honestly rather than asserting around it.
    """
    logger = logging.getLogger("uvicorn.error")
    logger.handlers = [logging.StreamHandler(sys.stdout)]
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    return logger


def _drive_a_failing_verification(*, hardened: bool) -> tuple[int | None, bool]:
    """Run a verification whose provider raises label text. Returns (status, escaped).

    `raise_server_exceptions=True` is deliberate: it is how Starlette surfaces the
    re-raise that `ServerErrorMiddleware` performs, which is the exact object uvicorn would
    receive in production. Whatever escapes is handed to the uvicorn stand-in.
    """
    applog.configure()
    app = create_app(config=make_config(), provider=ExplodingProvider())
    if hardened:
        harden(app, make_config())
    client = TestClient(app, raise_server_exceptions=True)

    application, files = _verify_payload()
    try:
        response = client.post(
            "/verify", data={"application": json.dumps(application)}, files=files
        )
    except Exception:
        _uvicorn_error_logger().error("Exception in ASGI application", exc_info=True)
        return None, True
    return response.status_code, False


def test_the_http_traceback_leak_is_real_without_the_fix(capfd: Any) -> None:
    """Evidence that the finding is a finding, in the shape it actually occurs.

    `api/logging.py` allowlists field names and raises on anything else. That is deliberate
    and correct and nothing here weakens it — but it governs `applog.log` and nothing else.
    A provider exception whose message carries label text escapes the app entirely,
    `ServerErrorMiddleware` re-raises it after the registered handler runs, and uvicorn
    formats the whole traceback to stdout. The allowlist never sees that path.
    """
    security.remove_log_containment()
    _uvicorn_error_logger()

    status, escaped = _drive_a_failing_verification(hardened=False)
    leaked = capfd.readouterr().out

    assert escaped, "the exception should reach the server, which is the leak"
    assert status is None
    assert "Traceback" in leaked
    assert LABEL_TEXT in leaked, "label text on stdout — this is the SEC-4 hole"


def test_a_traceback_carrying_label_text_never_reaches_stdout(capfd: Any) -> None:
    """LP-086, the whole point.

    Same request, same exception, with the containment layer installed. Nothing escapes to
    the server, so uvicorn never formats anything; the agent gets the taxonomy 500 instead.
    Captured at the file descriptors — `capfd`, not `caplog` — because bytes on stdout is
    precisely what is being denied, and a test that read the logging framework's own
    buffers would pass while the bytes went out anyway.
    """
    _uvicorn_error_logger()

    status, escaped = _drive_a_failing_verification(hardened=True)
    captured = capfd.readouterr()
    combined = captured.out + captured.err

    assert not escaped, "containment must stop the exception before the server sees it"
    assert status == 500
    assert "Traceback" not in combined
    assert LABEL_TEXT not in combined
    assert "STONE'S THROW" not in combined


def test_containment_holds_even_if_the_exception_reaches_the_server(capfd: Any) -> None:
    """Belt and braces: process-wide containment covers what the middleware cannot see.

    The middleware only sees exceptions raised inside the ASGI app. Anything raised in the
    server itself, in a protocol callback, or in a library that logs with `exc_info` goes
    through `logging` — which is why containment also replaces the log record factory.
    """
    security.install_log_containment()
    logger = _uvicorn_error_logger()

    try:
        raise RuntimeError(LABEL_TEXT)
    except RuntimeError:
        logger.error("Exception in ASGI application", exc_info=True)

    captured = capfd.readouterr()
    combined = captured.out + captured.err
    assert LABEL_TEXT not in combined
    assert "Traceback" not in combined
    assert "RuntimeError suppressed" in combined


def test_the_scrubbed_line_still_names_what_broke(capfd: Any) -> None:
    """Containment must not cost a developer the ability to find the bug."""
    _drive_a_failing_verification(hardened=True)

    lines = [
        json.loads(line)
        for line in capfd.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    unhandled = [line for line in lines if line.get("event") == "unhandled_exception"]
    assert unhandled, "the failure should still be observable"
    assert unhandled[-1]["reason_code"] == "RuntimeError"
    assert unhandled[-1]["request_id"].startswith("req_")
    assert set(unhandled[-1]) <= applog.ALLOWED_FIELDS | {"ts"}


def test_containment_leaves_ordinary_log_lines_alone(capfd: Any) -> None:
    """Scrubbing everything would cost uvicorn's bind line, which is the ops signal that
    the container came up."""
    security.install_log_containment()
    logger = logging.getLogger("uvicorn.error")
    logger.handlers = [logging.StreamHandler(sys.stdout)]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info("Uvicorn running on http://0.0.0.0:8000")

    assert "Uvicorn running on http://0.0.0.0:8000" in capfd.readouterr().out


def test_a_worker_thread_that_dies_leaks_nothing(capfd: Any) -> None:
    """`BatchStore.claim()` revalidates the stored application; a ValidationError there
    carries the brand name and producer address, on a thread, straight to stderr."""
    applog.configure()
    security.install_log_containment()

    def explode() -> None:
        raise ValueError(LABEL_TEXT)

    thread = threading.Thread(target=explode, name="labelproof-batch")
    thread.start()
    thread.join()

    captured = capfd.readouterr()
    combined = captured.out + captured.err
    assert LABEL_TEXT not in combined
    assert "Traceback" not in combined
    assert "unhandled_thread_exception" in combined


def test_containment_is_reversible_and_idempotent() -> None:
    original = logging.getLogRecordFactory()
    security.install_log_containment()
    security.install_log_containment()
    assert security.containment_active()
    security.remove_log_containment()
    assert logging.getLogRecordFactory() is original
    assert not security.containment_active()


def test_containment_can_be_turned_off_for_local_debugging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LABELPROOF_DEBUG_TRACEBACKS", "1")
    policy = SecurityPolicy.from_config(make_config())
    assert policy.contain_tracebacks is False
    app = FastAPI()
    harden(app, make_config())
    assert not security.containment_active()


# --- the no-content log rule end to end (LP-086, LP-251) ----------------------------------


def test_a_real_verification_writes_no_label_string_to_the_logs(capfd: Any) -> None:
    """SEC-4 as a property of a full run, not of the logger in isolation.

    Every line emitted by a successful verification is parsed and checked twice: it must be
    JSON on the allowlist, and no value on it may contain any string from the application
    or from the label the fixture carries.
    """
    applog.configure()
    app = create_app(config=make_config())
    harden(app, make_config())
    client = TestClient(app)

    application, files = _verify_payload()
    response = client.post(
        "/verify", data={"application": json.dumps(application)}, files=files
    )
    assert response.status_code == 200

    captured = capfd.readouterr()
    combined = captured.out + captured.err

    label_strings = [
        str(value) for value in application.values() if isinstance(value, str) and len(value) > 3
    ]
    label_strings += [
        field["extracted"]
        for field in response.json()["fields"]
        if isinstance(field.get("extracted"), str) and len(field["extracted"]) > 3
    ]
    assert label_strings, "the fixture should produce extracted values to look for"

    for text in label_strings:
        assert text not in combined, f"label content reached the logs: {text!r}"

    lines = [json.loads(line) for line in combined.splitlines() if line.startswith("{")]
    assert lines, "the run should have logged something"
    for line in lines:
        assert set(line) <= applog.ALLOWED_FIELDS | {"ts"}


def test_the_allowlist_still_refuses_an_unlisted_field() -> None:
    """Nothing in this wave weakened `api/logging.py`, and this asserts it."""
    with pytest.raises(applog.ContentInLogError):
        applog.log("verify_complete", brand_name="Old Tom Distillery")
