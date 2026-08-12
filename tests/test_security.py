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
from api.middleware.ratelimit import RateLimitMiddleware
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
    """Blob previews for the upload thumbnails and the off-main-thread encode worker."""
    assert "img-src 'self' blob: data:" in CONTENT_SECURITY_POLICY
    assert "worker-src 'self' blob:" in CONTENT_SECURITY_POLICY


def test_the_policy_carries_no_unsafe_directive_at_all() -> None:
    """There is no relaxation anywhere in this policy, and this is the guard on that.

    An earlier version shipped `style-src 'unsafe-inline'` on the theory that the evidence
    overlay's React inline `style` props needed it. That theory was wrong — react-dom
    applies the `style` prop through `node.style.setProperty`, a CSSOM mutation, which CSP
    does not govern; CSP governs style attributes parsed from markup and `<style>` elements.
    Checked in a browser against the real built SPA under this exact policy: an injected
    `<style>` element and an inline `<script>` were both refused (`style-src-elem`,
    `script-src-elem`, so enforcement was genuinely on) while the evidence box still
    computed to `left: 30.4px` and rendered over the brand name it cites.

    A source check cannot see a rendering bug, so this test does not pretend to. It holds
    the line that was hard-won: if someone adds `'unsafe-inline'` back, they have to delete
    this and explain why in the same commit.
    """
    for unsafe in ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "data: 'self' *"):
        assert unsafe not in CONTENT_SECURITY_POLICY, f"{unsafe} is back in the CSP"
    assert "style-src 'self';" in CONTENT_SECURITY_POLICY + ";"


def test_only_the_evidence_overlay_uses_inline_styles() -> None:
    """The premise of the browser check above, held in place.

    The finding turned on *which* mechanism sets those styles. If a component starts
    emitting a real inline `<style>` element or a CSS-in-JS runtime lands in `web/`, the
    browser evidence stops applying and the CSP needs re-testing rather than re-assuming.
    """
    sources = list((ROOT / "web" / "src").rglob("*.tsx")) + list(
        (ROOT / "web" / "src").rglob("*.ts")
    )
    assert sources, "the SPA sources should be present"

    inline_style_props = {
        path.relative_to(ROOT).as_posix()
        for path in sources
        if "style={{" in path.read_text()
    }
    assert inline_style_props == {"web/src/components/EvidenceOverlay.tsx"}, (
        "a new inline-style user appeared; re-check the CSP in a browser before trusting "
        "test_the_policy_carries_no_unsafe_directive_at_all"
    )

    for path in sources:
        text = path.read_text()
        assert "<style" not in text, f"{path.name} renders a style element — CSP governs those"
        assert "dangerouslySetInnerHTML" not in text, f"{path.name} injects raw HTML"


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


def test_hardening_twice_does_not_halve_the_rate_limit() -> None:
    """Two rate limiters in the stack means every request spends two tokens.

    That is the failure mode of a well-intentioned second wiring line: the 30/min ceiling
    silently becomes 15 and the first thing to break is the demo. `harden` returns the
    policy it already installed instead.
    """
    app = FastAPI()

    @app.post("/verify")
    def verify() -> JSONResponse:
        return JSONResponse({"ok": True})

    # The two calls carry DIFFERENT limits, deliberately. With the same limit twice, two
    # stacked buckets deplete in lockstep and behave identically to one — the previous
    # version measured the same result at one, two and three copies, so it proved nothing.
    # A looser first call and a tighter second one separates them: if the second stacks, the
    # tighter bucket is now outermost and refuses at request three.
    first = harden(app, make_config(rate_limit_per_minute=4))
    second = harden(app, make_config(rate_limit_per_minute=2))
    assert second is first
    assert first.rate_limit_per_minute == 4, "the second call must not replace the policy"

    client = TestClient(app)
    statuses = [client.post("/verify").status_code for _ in range(4)]
    assert statuses == [200] * 4, f"a second limiter is stacked in the middleware: {statuses}"

    installed = [
        middleware
        for middleware in app.user_middleware
        if middleware.cls is RateLimitMiddleware
    ]
    assert len(installed) == 1


def test_the_real_app_is_hardened_end_to_end() -> None:
    """`create_app` plus `harden`, exercising the shipped stack.

    This proves the two pieces fit together. It does **not** prove the shipped app is
    hardened, because it installs the posture itself — see the block below, which is the
    test that actually holds `api/main.py` to account.
    """
    app = create_app(config=make_config())
    harden(app, make_config())
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


# --- is the posture actually switched on? -------------------------------------------------
#
# Every other test in this file builds its own app and installs the posture itself, so all
# of them pass whether or not `api/main.py` calls `harden`. That is how the entire security
# posture — CSP, rate limiting, CORS, containment, retention — came to be a fully tested,
# fully documented no-op in the shipped app with nothing going red.
#
# These four take the app exactly as the process serves it and assert the controls are live.
# They are the only tests here that can see the wiring, so if `api/main.py` ever stops
# calling `harden`, this is what fails. Do not give any of them their own `harden()` call to
# make them pass; that is precisely the mistake they exist to catch.


def as_shipped(**config_overrides: Any) -> FastAPI:
    """The app the way `api/main.py` builds it, with nothing added by the test."""
    return create_app(config=make_config(**config_overrides))


def test_the_shipped_app_records_a_security_policy() -> None:
    assert getattr(as_shipped().state, "security_policy", None) is not None


def test_the_shipped_app_sends_a_content_security_policy() -> None:
    response = TestClient(as_shipped()).get("/health")
    assert response.headers.get("content-security-policy") == CONTENT_SECURITY_POLICY
    assert response.headers.get("x-frame-options") == "DENY"


def test_the_shipped_app_refuses_a_foreign_origin_write() -> None:
    response = TestClient(as_shipped()).post(
        "/verify", headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_the_shipped_app_rate_limits() -> None:
    """The limit has to be passed to `create_app`, not set on `app.state` afterwards.

    `RateLimitMiddleware` is constructed inside `harden` from the config it is handed, so
    assigning `app.state.config` after the factory returned changed nothing — the middleware
    already held the default 30/min and six requests never reached it. This canary could not
    pass even with the wiring in place, which would have sent whoever deleted the xfail
    markers hunting for a fault in the limiter that was not there.
    """
    client = TestClient(as_shipped(rate_limit_per_minute=2))
    statuses = [client.post("/verify").status_code for _ in range(6)]
    assert 429 in statuses, f"the shipped app is not rate limiting: {statuses}"


def test_the_shipped_app_installs_the_retention_sweeper() -> None:
    """Split out of the rate-limit canary, where it sat below a failing assertion and was
    therefore unreachable — an assertion that cannot run is an assertion that is not made."""
    app = as_shipped()
    sweeper = getattr(app.state, "retention_sweeper", None)
    assert sweeper is not None
    with TestClient(app):
        assert sweeper.running, "the sweeper is installed but never started"


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


def _drive(app: FastAPI, request: Any) -> tuple[int | None, bool]:
    """Send `request`, and hand whatever escapes to the uvicorn stand-in.

    `raise_server_exceptions=True` is deliberate: it is how Starlette surfaces the re-raise
    that `ServerErrorMiddleware` performs, which is the exact object uvicorn would receive
    in production. Returns `(status, escaped)`.
    """
    client = TestClient(app, raise_server_exceptions=True)
    try:
        response = request(client)
    except Exception:  # noqa: BLE001 - reproducing what uvicorn does with any exception
        _uvicorn_error_logger().error(
            "Exception in ASGI application", exc_info=True
        )
        return None, True
    return response.status_code, False


def _drive_a_failing_verification(*, hardened: bool) -> tuple[int | None, bool]:
    """Run a real verification whose provider raises label text."""
    applog.configure()
    app = create_app(config=make_config(), provider=ExplodingProvider())
    if hardened:
        harden(app, make_config())

    application, files = _verify_payload()

    def post(client: TestClient) -> Any:
        return client.post(
            "/verify", data={"application": json.dumps(application)}, files=files
        )

    return _drive(app, post)


def test_the_http_traceback_leak_is_real_without_the_fix(capfd: Any) -> None:
    """Evidence that the finding is a finding, in the shape it actually occurs.

    `api/logging.py` allowlists field names and raises on anything else. That is deliberate
    and correct and nothing here weakens it — but it governs `applog.log` and nothing else.
    An exception whose message carries label text escapes the app entirely,
    `ServerErrorMiddleware` re-raises it after the registered handler runs, and uvicorn
    formats the whole traceback to stdout. The allowlist never sees that path.

    The unhardened app is built here rather than borrowed from `create_app`, on purpose:
    once the app factory installs the posture by default, `create_app` will have no
    unhardened form and this test would quietly stop demonstrating anything. A bare app
    that raises exercises the identical mechanism — route raises, `ServerErrorMiddleware`
    re-raises, the server formats it — and keeps demonstrating it forever.
    """
    security.remove_log_containment()
    _uvicorn_error_logger()

    bare = FastAPI()

    @bare.get("/boom")
    def boom() -> JSONResponse:
        raise RuntimeError(LABEL_TEXT)

    status, escaped = _drive(bare, lambda client: client.get("/boom"))
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
        logger.error(  # noqa: G201 - uvicorn's exact spelling is the thing under test
            "Exception in ASGI application", exc_info=True
        )

    captured = capfd.readouterr()
    combined = captured.out + captured.err
    assert LABEL_TEXT not in combined
    assert "Traceback" not in combined
    assert "RuntimeError suppressed" in combined


def test_a_contained_500_still_carries_a_request_id() -> None:
    """The response that most needs correlation was the one without it.

    Containment sits outside the app factory's request-context middleware, so it swallowed
    the exception before an ID was ever attached: a 500 came back with a CSP and no
    `X-Request-ID`, while the README promised "the ID an agent reads off the screen is the
    ID in the logs". The 429 path was given an ID deliberately; this one was missed.
    """
    app = hardened_app()

    @app.get("/explode")
    def explode() -> JSONResponse:
        raise RuntimeError("something a compliance agent will phone someone about")

    response = TestClient(app, raise_server_exceptions=False).get("/explode")
    assert response.status_code == 500
    assert response.headers["x-request-id"].startswith("req_")
    assert response.headers["cache-control"] == "no-store"
    assert "content-security-policy" in response.headers


def test_the_id_on_a_contained_500_is_the_id_in_the_log(capfd: Any) -> None:
    applog.configure()
    app = hardened_app()

    @app.get("/explode")
    def explode() -> JSONResponse:
        raise RuntimeError("boom")

    response = TestClient(app, raise_server_exceptions=False).get("/explode")
    lines = [
        json.loads(line)
        for line in capfd.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    unhandled = [line for line in lines if line.get("event") == "unhandled_exception"]
    assert unhandled, "the failure should be in the log"
    assert unhandled[-1]["request_id"] == response.headers["x-request-id"]


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


def test_an_exception_passed_as_a_log_argument_is_scrubbed(capfd: Any) -> None:
    """The gap the traceback scrubbing alone did not close.

        logger.error("extraction failed: %s", exc)

    No `exc_info`, so nothing about that record looks like a traceback — and `str(exc)` on
    the extraction path is the label the model just read. It is also the most ordinary way
    in the world to write a log line.
    """
    security.install_log_containment()
    logger = _uvicorn_error_logger()

    logger.error("extraction failed: %s", RuntimeError(LABEL_TEXT))
    logger.error(RuntimeError(LABEL_TEXT))
    logger.error("failed on %(what)s", {"what": ValueError(LABEL_TEXT)})

    captured = capfd.readouterr()
    combined = captured.out + captured.err
    assert LABEL_TEXT not in combined
    assert "STONE'S THROW" not in combined
    # The developer still learns what class of thing went wrong.
    assert "<RuntimeError>" in combined
    assert "<ValueError>" in combined
    # And the human half of the message survives, because it carries no content.
    assert "extraction failed" in combined


@pytest.mark.parametrize(
    "container",
    [
        [RuntimeError(LABEL_TEXT)],
        (RuntimeError(LABEL_TEXT),),
        {RuntimeError(LABEL_TEXT)},
        [[RuntimeError(LABEL_TEXT)]],
        {"failures": [RuntimeError(LABEL_TEXT)]},
        [{"why": RuntimeError(LABEL_TEXT)}],
    ],
)
def test_exceptions_inside_containers_are_scrubbed_too(capfd: Any, container: Any) -> None:
    """I claimed this was unconditional after handling `dict`. It was not.

        logger.error("failures: %s", errors)

    with a list of per-item exceptions is an ordinary batch-worker line — `WorkerPool`
    collects exactly that, one per failed item, each carrying a brand name — and a list went
    through untouched.
    """
    security.install_log_containment()
    logger = _uvicorn_error_logger()

    logger.error("failures: %s", container)

    combined = capfd.readouterr().out
    assert LABEL_TEXT not in combined, f"leaked out of {type(container).__name__}"
    assert "<RuntimeError>" in combined
    assert "failures:" in combined


def test_scrubbing_a_self_referencing_argument_terminates(capfd: Any) -> None:
    """A cyclic structure must not hang the log call, and must not leak on the way out.

    My first bound was a four-level depth cap that returned the value untouched below it,
    so `[exc, [exc, [exc, …]]]` printed in full past the bound — a leak wearing a safety
    belt. A depth cap has to fail open at the bottom to be useful. Cycle detection does not.
    """
    security.install_log_containment()
    logger = _uvicorn_error_logger()

    cycle: list[Any] = [RuntimeError(LABEL_TEXT)]
    cycle.append(cycle)

    logger.error("failures: %s", cycle)

    combined = capfd.readouterr().out
    assert LABEL_TEXT not in combined
    assert "<RuntimeError>" in combined


def test_a_deeply_nested_exception_is_still_found(capfd: Any) -> None:
    """Ten levels down, which is past any depth cap I would have picked."""
    security.install_log_containment()
    logger = _uvicorn_error_logger()

    nested: Any = RuntimeError(LABEL_TEXT)
    for _ in range(10):
        nested = [nested]

    logger.error("failures: %s", nested)

    combined = capfd.readouterr().out
    assert LABEL_TEXT not in combined
    assert "<RuntimeError>" in combined


def test_a_stack_info_record_keeps_its_message(capfd: Any) -> None:
    """`logger.warning("slow request", stack_info=True)` is not an exception report.

    The first version fired on `exc_info or sinfo`, so it destroyed the message and
    announced "Exception suppressed" for an exception that never happened. Drop the stack,
    keep the line.
    """
    security.install_log_containment()
    logger = _uvicorn_error_logger()
    logger.setLevel(logging.WARNING)

    logger.warning("slow request", stack_info=True)

    combined = capfd.readouterr().out
    assert "slow request" in combined
    assert "suppressed" not in combined
    assert "Stack (most recent call last)" not in combined


def test_containment_notices_when_something_else_takes_the_factory() -> None:
    """`containment_active()` read a module flag, so it answered True after a hijack.

    That is the worst possible answer from a function whose only job is to say whether a
    security control is on. It now reads the live factory, and re-installing re-wraps
    whatever is in place instead of returning early on a stale flag.
    """
    security.install_log_containment()
    assert security.containment_active()

    ours = logging.getLogRecordFactory()
    hijacker = logging.LogRecord
    logging.setLogRecordFactory(hijacker)
    try:
        assert not security.containment_active(), "must not claim to be active after a hijack"
        security.install_log_containment()
        assert security.containment_active(), "re-install must re-wrap, not no-op"
        assert logging.getLogRecordFactory() is not ours
    finally:
        security.remove_log_containment()


def test_containment_still_scrubs_after_being_reasserted(capfd: Any) -> None:
    """The self-heal has to actually work, not just flip the flag."""
    security.install_log_containment()
    logging.setLogRecordFactory(logging.LogRecord)
    security.install_log_containment()

    logger = _uvicorn_error_logger()
    try:
        raise RuntimeError(LABEL_TEXT)
    except RuntimeError:
        logger.error(  # noqa: G201 - uvicorn's exact spelling is the thing under test
            "Exception in ASGI application", exc_info=True
        )

    combined = capfd.readouterr().out + capfd.readouterr().err
    assert LABEL_TEXT not in combined


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
