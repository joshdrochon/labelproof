"""OPEN DEFECTS in routing: batch is unreachable, and four status codes are collapsed.

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

**Defect two — the error taxonomy collapses four HTTP statuses into 400.** `_from_status`
maps 404, 405, 413 and 429 onto `UserError`, whose `kind` is `user`, whose status is 400.
The `code` field keeps the distinction; the status code does not. So the SPA fallback
carefully raises `HTTPException(405)` to preserve "wrong verb, not wrong URL" — with a
comment saying exactly that — and the handler flattens it to 400 on the way out.

It matters beyond tidiness. A 429 that answers 400 will not be retried by any client
honouring `Retry-After`; a proxy or WAF cannot distinguish a missing route from a
malformed body; and monitoring that alerts on 4xx rates loses the one split that says
whether callers are lost or the service is shedding load. SEC-9 asks for rate limiting
with a plain-language body — the body is right and the status is not.

This one is a judgment call rather than an obvious bug: the taxonomy's `kind` deliberately
groups by *who can act*, and all four of these are "the caller". The counter-argument is
that `main.py`'s own code goes out of its way to preserve 404-versus-405 and then loses
it. Flagged for the owner rather than asserted as settled.

Both are `xfail(strict=True)`: wiring the router or splitting the statuses turns these
red, which is the signal that the gap closed.
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

    assert "batch" in main_module._API_PREFIXES  # noqa: SLF001
    assert "/batch" in main_module._POST_ONLY_ROUTES  # noqa: SLF001


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


def test_the_batch_test_suite_mounts_the_router_itself() -> None:
    """Why 624 tests stayed green while the endpoint did not exist.

    `tests/test_batch.py` calls `app.include_router(batch_routes.router)` after
    `create_app`. Every batch test therefore exercises a differently-assembled app than
    the one that ships. Pinned so that when the mount lands in `create_app`, the
    now-redundant line in the test file is findable rather than left to drift.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "test_batch.py").read_text()
    assert "include_router(batch_routes.router)" in source


# --------------------------------------------------------------------------------------
# Defect 2: four statuses collapse to 400
# --------------------------------------------------------------------------------------


def test_the_error_envelope_itself_is_correct(client: TestClient) -> None:
    """Established first: only the status code is in question.

    The body carries `kind`, `code`, `message` and `next_step`, all correct and all
    written for an agent. Nothing below is a complaint about the taxonomy.
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
    assert main_module._from_status(status).code == code  # noqa: SLF001


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open, judgment call — flagged for the owner): _from_status maps 404, "
        "405, 413 and 429 onto UserError, whose kind 'user' maps to HTTP 400. The "
        "distinction survives in `code` but not in the status line, so a 429 is not "
        "retried by clients honouring Retry-After, a proxy cannot tell a missing route "
        "from a malformed body, and 4xx dashboards lose the split. main.py's SPA "
        "fallback raises 405 specifically to preserve 'wrong verb, not wrong URL' and "
        "this undoes it. Fix: let LabelProofError carry an explicit status_code that "
        "overrides the kind mapping, keeping `kind` as the who-can-act grouping. "
        "Owner: api/errors.py, api/main.py."
    ),
)
@pytest.mark.parametrize("status", [404, 405, 413, 429])
def test_the_http_status_matches_the_status_it_was_built_from(status: int) -> None:
    assert main_module._from_status(status).status_code == status  # noqa: SLF001


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open): the same collapse, seen over the wire. An unknown address "
        "answers 400 rather than 404. Owner: api/errors.py, api/main.py."
    ),
)
def test_an_unknown_address_answers_404_over_the_wire(client: TestClient) -> None:
    assert client.get("/no-such-address").status_code == 404


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (open): the same collapse. A GET on the POST-only /verify answers 400 "
        "rather than 405, so the deliberate wrong-verb signal never reaches the client. "
        "Owner: api/errors.py, api/main.py."
    ),
)
def test_a_wrong_verb_answers_405_over_the_wire(client: TestClient) -> None:
    assert client.get("/verify").status_code == 405


def test_provider_and_internal_failures_do_carry_their_own_statuses() -> None:
    """The half of the mapping that is right, so the fix is a narrowing rather than a rewrite.

    Provider trouble is 503 and not 500 — it is not our bug, and anyone reading a
    status page needs that distinction. The same argument applies to the four above.
    """
    assert errors.ProviderUnavailable().status_code == 503
    assert errors.InternalError().status_code == 500
    assert errors.ImageError("unreadable").status_code == 422
