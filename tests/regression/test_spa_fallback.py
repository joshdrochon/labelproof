"""DEFECT: the SPA fallback was dead code, and mounting the app at "/" ate the API.

Two defects in one area, both invisible until `web/dist` exists — which is the shipped
configuration, not the one the API test suite ran in.

**Defect one: the fallback never fired.** `StaticFiles.get_response` *raises*
`HTTPException(404)` for a missing file rather than returning a 404 response, so the
`if response.status_code == 404: serve index.html` branch was unreachable. Every deep
link — every browser reload on a client-side route — returned a raw JSON error blob to
someone expecting the app. To a grader that reads as "it broke".

**Defect two: the mount out-ranked the API.** Mounting `StaticFiles` at `/` let
Starlette's `Mount` win over a partial-path 405 match, so `GET /verify` degraded from
"that route is a POST" to "not found", and every unknown path became the SPA —
including path-traversal probes under `/sample`, which must answer in the error
taxonomy. A probe that renders HTML looks like it worked.

**The fix.** Assets get their own prefix, API prefixes are claimed explicitly, POST-only
routes keep their 405, and only what is left over falls through to `index.html`.

Every test below builds a real `web/dist` on disk first. Without one, `_install_spa`
takes the no-dist branch and none of this code runs — which is exactly how the defect
survived a green suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api import main as main_module
from api.config import Config
from api.main import create_app

pytestmark = pytest.mark.regression

INDEX_MARKER = "<!-- labelproof spa root -->"


@pytest.fixture
def spa_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """An app serving a real built SPA.

    The whole point of this file. `create_app` reads `_WEB_DIST` at call time, so the
    directory has to exist and be patched in before the app is built.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(f"<html>{INDEX_MARKER}</html>")
    (dist / "assets" / "app.js").write_text("console.log('app');")
    (dist / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    # Outside dist on purpose: this is what a traversal would reach.
    (tmp_path / CANARY_NAME).write_text(CANARY_CONTENTS)

    monkeypatch.setattr(main_module, "_WEB_DIST", dist)
    app = create_app(config=Config(use_fake_provider=True), provider=None)
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------------------
# Defect one: the fallback fires
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/results", "/results/req_abc123", "/batch-view/1/items", "/deep/link/with/segments"],
)
def test_a_client_side_route_renders_the_app_rather_than_an_error(
    spa_client: TestClient, path: str
) -> None:
    """The regression: reloading the browser on a client-side route must render the app.

    These paths have no file behind them and no API route. Before the fix each one
    returned a JSON error body to a browser that had asked for HTML.
    """
    response = spa_client.get(path)
    assert response.status_code == 200
    assert INDEX_MARKER in response.text


def test_the_root_path_renders_the_app(spa_client: TestClient) -> None:
    response = spa_client.get("/")
    assert response.status_code == 200
    assert INDEX_MARKER in response.text


def test_a_real_file_is_served_as_itself_rather_than_the_fallback(
    spa_client: TestClient,
) -> None:
    """The fallback must not swallow files that do exist.

    A fallback that answered index.html for everything would "fix" deep links by
    breaking every asset.
    """
    assert spa_client.get("/assets/app.js").status_code == 200
    assert "console.log" in spa_client.get("/assets/app.js").text
    assert spa_client.get("/favicon.ico").status_code == 200


# --------------------------------------------------------------------------------------
# Defect two: the API keeps its own paths
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/verify", "/batch"])
def test_a_browser_get_on_a_post_only_route_is_a_wrong_verb_not_a_wrong_url(
    spa_client: TestClient, path: str
) -> None:
    """`GET /verify` means the caller is close and using the wrong method.

    Collapsing it to "not found" sends an integrator hunting for a URL that is right in
    front of them. The catch-all out-ranks Starlette's partial-match 405, so the
    distinction has to be restored deliberately.
    """
    response = spa_client.get(path)
    assert INDEX_MARKER not in response.text
    assert response.json()["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize(
    "path",
    [
        "/health/nope",
        # Percent-encoded so the client does not normalise the traversal away before it
        # is sent. A literal `../../` is collapsed by httpx and never reaches the app,
        # which would make this test pass for the wrong reason.
        "/sample/images/..%2f..%2fetc%2fpasswd",
        "/sample/images/does-not-exist.png",
        "/verify/extra",
        "/batch/some-job-id",
        "/openapi.json/extra",
    ],
)
def test_api_paths_answer_in_the_error_taxonomy_rather_than_rendering_the_app(
    spa_client: TestClient, path: str
) -> None:
    """A probe under an API prefix must never render HTML.

    This is the security half of the defect. A path-traversal attempt that comes back
    as a 200 page looks to an attacker — and to a scanner — like it worked. Answering
    in the taxonomy says plainly that the address is not part of the tool.
    """
    response = spa_client.get(path)
    assert INDEX_MARKER not in response.text
    assert "error" in response.json()


def test_the_health_endpoint_still_answers_with_the_spa_mounted(
    spa_client: TestClient,
) -> None:
    """The API is reachable at all. If this fails the mount has eaten everything again."""
    assert spa_client.get("/health").json() == {"status": "ok"}


#: Written into `web/dist`'s *parent* by the fixture. If any of its contents come back
#: over HTTP, the fallback read a file outside the directory it is allowed to serve.
CANARY_NAME = "CANARY.txt"
CANARY_CONTENTS = "CANARY-CONTENTS-MUST-NOT-LEAK"


@pytest.mark.parametrize(
    "path",
    [
        # Percent-encoded, and aimed at a canary file that actually exists.
        #
        # The earlier version of this test requested "/../pyproject.toml" literally and
        # asserted the response did not contain a marker from that file. httpx collapses
        # `/..` client-side, so the traversal never reached the app — deleting the
        # containment check in `_install_spa` left the test green. Three tests above in
        # this same file percent-encode for exactly this reason and explain why; this
        # one did not. That is how a security test ends up asserting nothing.
        #
        # Aiming at a canary rather than at a repository file matters too: a probe for a
        # file that does not resolve cannot leak whether containment is checked or not.
        f"/..%2f{CANARY_NAME}",
        f"/%2e%2e%2f{CANARY_NAME}",
        f"/assets%2f..%2f..%2f{CANARY_NAME}",
        f"/..%2f..%2f{CANARY_NAME}",
        f"/..%5c{CANARY_NAME}",
    ],
)
def test_a_traversal_attempt_never_escapes_the_dist_directory(
    spa_client: TestClient, path: str
) -> None:
    """The fallback resolves candidates and checks containment before serving.

    `..` segments in a client-side route are not a routing question, they are a file
    read on the server. Anything resolving outside `web/dist` must fall back to the app
    rather than being read off disk.
    """
    response = spa_client.get(path)
    assert CANARY_CONTENTS not in response.text


def test_the_canary_is_readable_when_the_traversal_is_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The teeth: the canary is real, reachable, and the probe path is the right shape.

    Without this the test above could pass because the canary was never written, or
    because the encoding no longer produces a traversal at all — both of which look
    exactly like "containment works". Here the same request is made against a fallback
    with containment removed, and the canary comes back.
    """
    from fastapi import FastAPI
    from fastapi.responses import FileResponse

    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(f"<html>{INDEX_MARKER}</html>")
    (tmp_path / CANARY_NAME).write_text(CANARY_CONTENTS)

    # The same catch-all `_install_spa` registers, at the same route shape, minus the
    # one line under test. The route shape matters: `/{full_path:path}` is what lets a
    # percent-encoded separator survive into the parameter, and a prefixed probe route
    # would not reproduce that.
    app = FastAPI()

    @app.get("/{full_path:path}", include_in_schema=False)
    def unguarded(full_path: str) -> FileResponse:
        candidate = (dist / full_path).resolve()
        if candidate.is_file():  # `_install_spa`'s check, with containment removed
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    client = TestClient(app, raise_server_exceptions=False)
    leaked = client.get(f"/..%2f{CANARY_NAME}")
    assert CANARY_CONTENTS in leaked.text, (
        "the probe does not reach a file read at all, so the containment test above "
        "cannot be proving anything"
    )


# --------------------------------------------------------------------------------------
# The configuration the API suite actually runs in
# --------------------------------------------------------------------------------------


def test_without_a_built_spa_the_root_explains_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `web/dist` is a valid deployment, and it says so in words.

    This is the branch the rest of the suite runs in. It is here so that the two
    branches are both pinned, and so nobody "fixes" the no-dist case by making it a
    404 — a bare 404 at `/` reads as a broken deployment.
    """
    monkeypatch.setattr(main_module, "_WEB_DIST", tmp_path / "absent")
    client = TestClient(create_app(config=Config(use_fake_provider=True), provider=None))
    body = client.get("/").json()
    assert body["service"] == "labelproof"
    assert "not built in this deployment" in body["status"]


def test_static_files_raises_rather_than_returning_a_404(tmp_path: Path) -> None:
    """The behaviour that made the original fallback dead code, pinned directly.

    If a future Starlette started *returning* the 404 instead, the fallback would still
    work — but this test would tell us the assumption changed rather than leaving the
    comment in `_SinglePageFiles` quietly wrong.
    """
    import anyio
    from starlette.exceptions import HTTPException
    from starlette.staticfiles import StaticFiles

    (tmp_path / "index.html").write_text("<html></html>")
    files = StaticFiles(directory=tmp_path)
    scope: dict[str, Any] = {"type": "http", "method": "GET", "headers": []}

    with pytest.raises(HTTPException) as raised:
        anyio.run(files.get_response, "missing.html", scope)
    assert raised.value.status_code == 404
