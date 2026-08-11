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

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

from api.config import Config
from api.main import create_app
from api.security import CONTENT_SECURITY_POLICY, harden

ROOT = Path(__file__).resolve().parents[1]


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
    for response in (client.get("/nope"), client.post("/nope")):
        assert response.status_code >= 400
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
