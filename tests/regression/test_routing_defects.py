"""ROUTING DEFECTS: batch is unreachable (open), four statuses were collapsed (fixed).

**Defect one — `create_app` never mounts the batch router.** `api/routes/batch.py` is
629 lines implementing `POST /batch`, `GET /batch/{id}`, retry and CSV export, and
`api/main.py` never calls `app.include_router(batch.router)`. `POST /batch` on the
shipped app answers "that address is not part of this tool". Batch is one of the two
modes the PRD specifies, so half the product is dark.

This is a known, documented integration gap — `JUDGMENT-LOG.md` records it as one of two
points the batch author could not wire because `api/main.py` belongs to another agent,
along with the whole-request size ceiling that would refuse a real 300-item batch. It is
pinned here rather than merely written down because the batch test suite mounts the
router *itself*, which is exactly why 624 tests were green while the endpoint did not
exist. A documented gap with no failing test is a gap that ships.

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

Defect one is still `xfail(strict=True)`: mounting the router turns those red, which is
the signal that the gap closed.
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open, documented in JUDGMENT-LOG.md): create_app() never calls "
        "app.include_router(batch.router), so POST /batch answers 'that address is not "
        "part of this tool' on the shipped app. Batch is one of the two modes the PRD "
        "specifies. Fix: include the batch router BEFORE _install_spa, or the SPA "
        "catch-all answers GET /batch/{id} with a 404. Note the companion gap in the "
        "same log entry: main._too_large_to_read derives a 41 MB whole-request ceiling "
        "from the single-verify caps, which refuses a real 300-item batch before "
        "routing. Owner: api/main.py."
    ),
)
def test_the_batch_endpoint_is_reachable_on_the_shipped_app(client: TestClient) -> None:
    """A bare POST should be a validation error about the manifest, not a missing route."""
    response = client.post("/batch")
    assert response.json()["error"]["code"] != "not_found"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open): the same missing mount, seen from the status page. GET on a "
        "job id answers not_found rather than telling the caller the job is unknown. "
        "Owner: api/main.py."
    ),
)
def test_a_batch_job_status_url_is_reachable_on_the_shipped_app(
    client: TestClient,
) -> None:
    response = client.get("/batch/job_does_not_exist")
    assert response.json()["error"]["code"] != "not_found"


def test_the_shipped_app_exposes_no_batch_route_at_all() -> None:
    """Why 624 tests stayed green while the endpoint did not exist.

    The batch tests mount the router themselves after calling `create_app`, so they
    exercise a differently-assembled app than the one that ships. This asserts the
    difference at its source — the shipped app's routing table — rather than by
    grepping another branch's test file for a literal, which is what it used to do.
    That grep asserted how somebody else's test was *written*, would have broken on a
    rename, and told us nothing about the product.
    """
    app = create_app(config=Config(use_fake_provider=True), provider=None)
    batch_routes = [
        route
        for route in app.router.routes
        if str(getattr(route, "path", "")).startswith("/batch")
    ]
    assert batch_routes == [], (
        "batch is mounted now — remove the xfails above and the self-mounting helper "
        "in tests/e2e/test_batch_flow.py"
    )


# --------------------------------------------------------------------------------------
# Defect 2: four statuses collapse to 400
# --------------------------------------------------------------------------------------


def test_the_error_envelope_itself_is_correct(client: TestClient) -> None:
    """The taxonomy was never the problem, and the fix leaves it alone.

    The body still carries `kind: user` for all four, because all four *are* the
    caller's to fix. Only the status line changed.
    """
    body = client.get("/no-such-address").json()["error"]
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
    assert client.get("/no-such-address").status_code == 404


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
