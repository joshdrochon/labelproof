"""ROUTING DEFECTS: batch was unreachable (fixed), four statuses were collapsed (fixed).

**Defect one — `create_app` never mounted the batch router. FIXED.**
`api/routes/batch.py` is 629 lines implementing `POST /batch`, `GET /batch/{id}`, retry
and CSV export, and `api/main.py` did not call `app.include_router(batch.router)`.
`POST /batch` on the shipped app answered "that address is not part of this tool". Batch
is one of the two modes the PRD specifies, so half the product was dark.

It was pinned here as three `xfail(strict=True)` tests rather than merely written down,
because the batch E2E suite mounted the router *itself* — which is exactly why 624 tests
were green while the endpoint did not exist. A documented gap with no failing test is a
gap that ships. `create_app` now mounts batch before `_install_spa` and calls
`install_verify_priority`, so those three turned red on the merge, as designed, and are
gone along with the self-mounting helper in `tests/e2e/test_batch_flow.py`. That file now
drives the shipped app.

One of the three deserves a note, because it was the weakest of them and it did not fail
— it passed while being wrong. `test_the_shipped_app_exposes_no_batch_route_at_all`
scanned `app.router.routes` for a `path` starting with `/batch`. Under this FastAPI
version an included router appears in that list as an `_IncludedRouter` with no `path`
attribute at all, so the scan found nothing and reported "batch is not mounted" whether
it was or not. The two xfails, which asked the *app over HTTP*, are what actually caught
the change. Introspecting a routing table is not the same as making a request, and only
one of the two knew the difference.

**Defect two — the error taxonomy collapsed four HTTP statuses into 400. FIXED.**
`_from_status` mapped 404, 405, 413 and 429 onto `UserError`, whose `kind` is `user`,
whose status is 400. The `code` field kept the distinction; the status code did not. So
the SPA fallback carefully raised `HTTPException(405)` to preserve "wrong verb, not wrong
URL" — with a comment saying exactly that — and the handler flattened it to 400 three
lines later.

It matters beyond tidiness. A 429 that answers 400 is not retried by any client honouring
`Retry-After`; a proxy or WAF cannot distinguish a missing route from a malformed body;
and monitoring that alerts on 4xx rates loses the one split that says whether callers are
lost or the service is shedding load. SEC-9 asks for rate limiting with a plain-language
body — the body was right and the status was not.

I first pinned this as a judgment call, on the grounds that `kind` deliberately groups by
*who can act* and all four are "the caller". A reviewer pushed back and was right: that
argument justifies the `kind`, not the discarded status, and the codebase had already
made the decision in `_install_spa` before the handler undid it. `LabelProofError` now
takes an explicit `status_code` that overrides the kind default, `kind` keeps grouping by
who can act, and the tests below are live assertions rather than pins.

Both defects are closed, and both are asserted live below rather than pinned.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import errors
from api import main as main_module
from api.config import Config
from api.main import create_app

pytestmark = pytest.mark.regression


@pytest.fixture
def client() -> TestClient:
    app = create_app(config=Config(use_fake_provider=True), provider=None)
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------------------
# Defect 1: batch mode is not wired into the shipped app
# --------------------------------------------------------------------------------------


def test_the_batch_router_module_exists_and_declares_its_routes() -> None:
    """Established first: the endpoints are implemented, they are simply not mounted.

    Without this the next test could be read as "batch was never built", which is a
    different and much larger problem than "one `include_router` call is missing".
    """
    from api.routes import batch as batch_routes

    paths = {route.path for route in batch_routes.router.routes}  # type: ignore[attr-defined]
    assert "/batch" in paths
    assert any(path.startswith("/batch/") for path in paths)


def test_main_claims_the_batch_prefix_for_the_api() -> None:
    """`main.py` already believes batch exists — it reserves the prefix from the SPA.

    Which is what makes the missing mount a wiring slip rather than a design decision:
    two of the three places that need to know about batch already do.
    """
    from api import main as main_module

    assert "batch" in main_module._API_PREFIXES
    assert "/batch" in main_module._POST_ONLY_ROUTES


def test_the_batch_endpoint_is_reachable_on_the_shipped_app(client: TestClient) -> None:
    """FIXED. A bare POST is a validation error about the manifest, not a missing route.

    Asked over HTTP, of the app `create_app` actually builds, with nothing mounted by
    the test. That is the only form of this question worth asking — see the module
    docstring on the routing-table version, which could not tell the two apart.
    """
    response = client.post("/batch")
    assert response.json()["error"]["code"] != "not_found"


def test_a_batch_job_status_url_is_reachable_on_the_shipped_app(
    client: TestClient,
) -> None:
    """FIXED. The same mount, seen from the status page.

    This is also what pins the *order* of the mount. `_install_spa` registers a
    `GET /{path:path}` catch-all, and a router included after it is shadowed, so a batch
    router mounted in the wrong place would answer `not_found` here while `POST /batch`
    above still worked.
    """
    response = client.get("/batch/job_does_not_exist")
    assert response.json()["error"]["code"] != "not_found"


# --------------------------------------------------------------------------------------
# Defect 2: four statuses collapse to 400
# --------------------------------------------------------------------------------------

#: An address that does not exist, under a prefix the API owns.
#:
#: It used to be `/no-such-address`, and that made this file's result depend on whether
#: anybody had run `npm run build`. `web/dist` is gitignored, so on a bare checkout
#: `_install_spa` takes the no-dist branch, nothing claims `/no-such-address`, and the
#: request 404s. Build the SPA — the shipped configuration — and the client-side router
#: owns that path by design: it comes back 200 with `index.html`, which is what makes a
#: browser reload on a deep link render the app, and is pinned as required behaviour in
#: `tests/regression/test_spa_fallback.py`. Two files in this suite were asserting
#: opposite things about the same URL and only the build state decided which one was red.
#:
#: An unknown address *under an API prefix* is unambiguous in both deployments: the API
#: owns it, and it answers in the taxonomy either way. That is also the case the status
#: line actually matters for — a scanner or a proxy probing the API.
UNKNOWN_API_ADDRESS = "/health/nope"


def test_the_error_envelope_itself_is_correct(client: TestClient) -> None:
    """The taxonomy was never the problem, and the fix leaves it alone.

    The body still carries `kind: user` for all four, because all four *are* the
    caller's to fix. Only the status line changed.
    """
    body = client.get(UNKNOWN_API_ADDRESS).json()["error"]
    assert body["kind"] == "user"
    assert body["code"] == "not_found"
    assert body["next_step"] == "navigate"
    assert "not part of this tool" in body["message"]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_found"),
        (405, "method_not_allowed"),
        (413, "file_too_large"),
        (429, "too_many_requests"),
    ],
)
def test_the_distinction_survives_in_the_code_field(status: int, code: str) -> None:
    """The information is not lost — it is just not where HTTP clients look."""
    assert main_module._from_status(status).code == code


@pytest.mark.parametrize("status", [404, 405, 413, 429])
def test_the_http_status_matches_the_status_it_was_built_from(status: int) -> None:
    """FIXED. These four used to collapse to 400 because `kind` chose the status.

    `LabelProofError` now accepts an explicit `status_code` that overrides the kind
    default, and `_from_status` passes each one through. `kind` keeps doing the job it
    is good at — grouping by who can act, which is what selects the message — and the
    status line keeps doing its own.
    """
    assert main_module._from_status(status).status_code == status


def test_an_unknown_address_answers_404_over_the_wire(client: TestClient) -> None:
    """FIXED. Over the wire, not just in the constructor.

    A scanner, a proxy and a browser all read the status line and none of them read
    `code`, so this is the assertion that matters.
    """
    assert client.get(UNKNOWN_API_ADDRESS).status_code == 404


def test_a_wrong_verb_answers_405_over_the_wire(client: TestClient) -> None:
    """FIXED. `_install_spa` raises 405 on purpose; it now survives the handler.

    The comment there says the distinction matters because "one means the URL is wrong,
    the other means the caller is close and using the wrong verb". It was true of the
    raise and false of the response.
    """
    assert client.get("/verify").status_code == 405


def test_provider_and_internal_failures_do_carry_their_own_statuses() -> None:
    """The half of the mapping that is right, so the fix is a narrowing rather than a rewrite.

    Provider trouble is 503 and not 500 — it is not our bug, and anyone reading a
    status page needs that distinction. The same argument applies to the four above.
    """
    assert errors.ProviderUnavailable().status_code == 503
    assert errors.InternalError().status_code == 500
    assert errors.ImageError("unreadable").status_code == 422
