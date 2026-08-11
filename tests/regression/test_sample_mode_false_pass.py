"""DEFECT: sample mode returned a clean pass for any file you uploaded.

**What happened.** `provider_for` resolves an offline fixture from the uploaded
filename when `LABELPROOF_FAKE_PROVIDER=1`. When it did not recognise the name it fell
back to the Old Tom fixture. Uploading arbitrary bytes under any filename therefore
returned `ready_to_approve`, a full checklist of Match rows, and a verbatim government
warning statement that was never on the image.

**Why it is the worst bug this product can have.** It is not a wrong answer, it is a
*confident* wrong answer with fabricated evidence: the report quoted 27 CFR 16.21 text
the pixels did not contain. An agent reading it would have had no way to tell. The PRD
names the false pass on the warning statement as the failure the product exists to
prevent, and this produced one for every upload.

**The fix.** Sample mode replays *recorded* labels. It cannot read pixels, so when it
does not recognise a filename the only honest answer is that nothing was checked — a
provider outage in the error taxonomy, with a message that tells the agent what to do
instead.

The tests below pin the fix from the direction the bug came from: they assert the
*absence* of a pass, not the presence of the error, because a future fallback that
returned some other cheerful verdict would still be the same defect.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api import errors
from api.config import Config
from api.main import create_app
from api.models import Recommendation
from api.provider.fake import spec_name_for_image

pytestmark = pytest.mark.regression


@pytest.fixture
def sample_mode_client() -> TestClient:
    """The shipped sample-mode configuration: no key, fixtures only, no provider injected."""
    config = Config(use_fake_provider=True, anthropic_api_key="")
    app = create_app(config=config, provider=None)
    return TestClient(app, raise_server_exceptions=False)


APPLICATION = {
    "commodity": "spirits",
    "brand_name": "OLD TOM DISTILLERY",
    "class_type": "Kentucky Straight Bourbon Whiskey",
    "alcohol_content": 45.0,
    "net_contents": "750 mL",
    "producer_name": "Old Tom Distillery",
    "producer_address": "Bardstown, Kentucky",
    "country_of_origin": None,
    "is_import": False,
}

#: Filenames that are not fixture keys. The first is what an agent's camera actually
#: produces; the last three are near-misses — the shapes that made the old fallback so
#: easy to reach without anyone noticing.
#:
#: An empty filename is deliberately absent: multipart drops a part with no filename,
#: so the request never reaches provider resolution and is a malformed request at a
#: different layer. That path is pinned at the resolver instead, below.
UNRECOGNISED = [
    "IMG_4471.jpg",
    "photo.png",
    "label.png",
    "tc99_does_not_exist.png",
    "tc01_old_tom_clean.png.bak",
    "copy of tc01_old_tom_clean.png",
]


@pytest.fixture
def renamed_label(fixture_uploads: Any) -> Any:
    """Real, readable label pixels behind whatever filename the caller supplies.

    This is the bug's actual shape. The bytes are a genuine generated label — sharp,
    correctly sized, straight through ingest and past the quality pre-gate — so the
    request reaches provider resolution, which is where the fallback lived. Using
    unreadable bytes instead would stop at the pre-gate and never exercise it.
    """
    real = dict(fixture_uploads("tc01_old_tom_clean.png"))["images"][1]

    def rename(filename: str) -> list[tuple[str, tuple[str, bytes, str]]]:
        return [("images", (filename, real, "image/png"))]

    return rename


@pytest.mark.parametrize("filename", UNRECOGNISED, ids=lambda n: n or "<empty>")
def test_an_unrecognised_upload_never_returns_a_recommendation(
    sample_mode_client: TestClient, renamed_label: Any, filename: str
) -> None:
    """The regression itself: rename a label and sample mode must refuse to verify it.

    Asserted as "no recommendation of any kind", not "not ready_to_approve". A fallback
    that returned `needs_review` on evidence it invented would be the same defect
    wearing a safer-looking chip.
    """
    response = sample_mode_client.post(
        "/verify",
        files=renamed_label(filename),
        data={"application": json.dumps(APPLICATION)},
    )
    assert response.status_code != 200
    assert "aggregate" not in response.json()
    assert Recommendation.READY_TO_APPROVE.value not in response.text


@pytest.mark.parametrize("filename", UNRECOGNISED, ids=lambda n: n or "<empty>")
def test_an_unrecognised_upload_degrades_in_the_error_taxonomy(
    sample_mode_client: TestClient, renamed_label: Any, filename: str
) -> None:
    """The failure is a provider outage, phrased for a compliance agent (OPS-5, UX-6).

    503 rather than 500: nothing is broken, the server simply cannot read photographs
    in this mode. The message has to say that and say what to do instead, because
    "provider_unavailable" on its own tells an agent nothing.
    """
    response = sample_mode_client.post(
        "/verify",
        files=renamed_label(filename),
        data={"application": json.dumps(APPLICATION)},
    )
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["kind"] == "provider"
    assert error["next_step"] == "try_sample"
    assert "Nothing has been checked" in error["message"]
    assert "sample mode" in error["message"]


def test_the_resolver_itself_raises_rather_than_substituting_a_fixture() -> None:
    """Pinned one level below HTTP, where the fallback actually lived.

    A future refactor could move the guard out of `_fixture_provider` and into the
    route while leaving this function happy to hand back Old Tom. Testing the resolver
    directly keeps the honesty where the resolution happens.
    """
    from api.routes import _fixture_provider

    with pytest.raises(errors.ProviderUnavailable):
        _fixture_provider(["IMG_4471.jpg", "another_unknown.png"])


def test_no_upload_at_all_still_fails_closed() -> None:
    """The empty-filename path, which is what an upload with no name reaches."""
    from api.routes import _fixture_provider

    with pytest.raises(errors.ProviderUnavailable):
        _fixture_provider([])


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("tc01_old_tom_clean.png", "tc01_old_tom_clean"),
        ("tc16_front_back_front.png", "tc16_front_back"),
        ("tc16_front_back_back.png", "tc16_front_back"),
        ("IMG_4471.jpg", None),
        ("cat.png", None),
        ("TC01_OLD_TOM_CLEAN.png", None),
    ],
)
def test_filename_to_fixture_mapping_is_an_allowlist_not_a_guess(
    filename: str, expected: str | None
) -> None:
    """The mapping recognises fixture names and nothing else.

    Returning `None` is what makes the caller fail closed. A looser pattern — matching
    case-insensitively, say, or stripping unknown suffixes — would reopen the fallback
    without anyone editing the resolver.
    """
    assert spec_name_for_image(filename) == expected


# --------------------------------------------------------------------------------------
# The other branch of the same resolver: a server with no key
# --------------------------------------------------------------------------------------
#
# `api/routes/__init__.py` was hidden from coverage by an undocumented `*/__init__.py`
# omit — 118 lines including this resolver, the sample-mode fallback above, and the
# live-adapter path below. Removing the glob surfaced the live path as the one part of
# provider resolution nothing exercised, which is the reason these three exist.


class _Request:
    """The two attributes `provider_for` reads off a request, and nothing else."""

    def __init__(self, app: Any) -> None:
        self.app = app


class _App:
    def __init__(self, config: Config) -> None:
        self.state = type("_State", (), {"config": config, "provider": None})()


def _request(app: _App) -> Any:
    """A stand-in `Request`, cast at one boundary.

    `provider_for` reads `request.app.state`, and a real `Request` cannot be built
    outside a running ASGI call. Casting once here beats an ignore comment on every
    call site.
    """
    return _Request(app)


def test_a_server_with_no_key_and_no_fake_mode_refuses_rather_than_crashing() -> None:
    """A misconfigured deployment answers in the taxonomy, not with an ImportError.

    From the agent's seat this is what it is: the label reading service is not
    available, and no application data changed. Telling them to ask whoever runs the
    service — or to switch to sample mode — is the whole difference between an outage
    they can act on and a 500 they cannot.
    """
    from api.routes import provider_for

    app = _App(Config(anthropic_api_key="", use_fake_provider=False))
    with pytest.raises(errors.ProviderUnavailable) as raised:
        provider_for(_request(app), ["tc01_old_tom_clean.png"])

    message = str(raised.value)
    assert "not set up on this server" in message
    assert "sample mode" in message


def test_an_injected_provider_wins_over_every_other_resolution() -> None:
    """The hook the whole offline suite depends on (ENG-3).

    If injection ever stopped taking precedence, every test in this repository would
    silently start resolving a real adapter — and the socket guard would be the only
    thing standing between the suite and a live call.
    """
    from api.provider.fake import FailingProvider
    from api.routes import provider_for

    app = _App(Config(anthropic_api_key="a-key", use_fake_provider=False))
    injected = FailingProvider()
    app.state.provider = injected

    assert provider_for(_request(app), ["anything.png"]) is injected


def test_the_live_adapter_is_built_once_and_kept_on_the_app() -> None:
    """A breaker that resets every request has learned nothing.

    Per-request construction throws away the connection pool and the circuit breaker,
    which is what keeps a provider outage from becoming a queue of hanging requests. So
    resolution has to cache — asserted by resolving twice and requiring the same object.

    Constructing the adapter does not open a socket; the guard in conftest would refuse
    it if it did.
    """
    from api.routes import provider_for

    app = _App(Config(anthropic_api_key="placeholder", use_fake_provider=False))
    first = provider_for(_request(app), [])
    second = provider_for(_request(app), [])

    assert first is second
    assert app.state.provider is first
    assert first.name == "anthropic"


def test_an_adapter_that_cannot_be_built_degrades_rather_than_raising_a_config_error() -> None:
    """A bad `LABELPROOF_EFFORT` must not reach the agent as a ConfigError.

    The adapter validates effort at construction (PERF-6), and that is right — but the
    resolver is a request path, so the failure has to leave as a provider outage in the
    taxonomy rather than as whatever exception the constructor chose.
    """
    from api.routes import provider_for

    app = _App(
        Config(anthropic_api_key="placeholder", use_fake_provider=False, effort="turbo")
    )
    with pytest.raises(errors.ProviderUnavailable) as raised:
        provider_for(_request(app), [])
    assert "not available on this server" in str(raised.value)


def test_a_recognised_fixture_still_resolves(sample_mode_client: TestClient) -> None:
    """Failing closed must not mean failing always.

    The one-click demo is the product's front door (UX-1, DEL-5). If this test ever
    goes red, sample mode has been hardened into uselessness — which is its own kind of
    broken.
    """
    from api.routes import _fixture_provider

    provider = _fixture_provider(["tc01_old_tom_clean.png"])
    assert provider.name == "fake:spec"
