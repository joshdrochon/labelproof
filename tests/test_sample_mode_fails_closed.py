"""Sample mode must never produce a verdict on content it cannot read.

Regression guard for the worst bug found in this codebase: fixture replay keyed off the
uploaded FILENAME, falling back to the clean compliant label when the name was not
recognized. That returned `ready_to_approve` with a verbatim government warning for
arbitrary bytes — a false pass carrying fabricated evidence, which is precisely what the
PRD names as the worst failure this product can produce.

The rule these tests pin: sample mode replays recorded labels. It cannot read pixels. When
it does not recognize a filename, the only honest answer is that nothing was checked.
"""

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api.config import Config
from api.main import create_app

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


def noise_png() -> bytes:
    """Bytes that are demonstrably not a label."""
    import numpy as np

    rng = np.random.default_rng(0)
    array = rng.integers(0, 255, size=(1400, 1000, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def client() -> TestClient:
    app = create_app(Config(use_fake_provider=True, anthropic_api_key=""))
    return TestClient(app, raise_server_exceptions=False)


def post(client: TestClient, filename: str, data: bytes):
    return client.post(
        "/verify",
        files=[("images", (filename, data, "image/png"))],
        data={"application": json.dumps(APPLICATION)},
    )


def test_unrecognized_filename_never_returns_a_verdict(client: TestClient) -> None:
    response = post(client, "my_bottle_photo.jpg", noise_png())
    assert response.status_code == 503
    assert "aggregate" not in response.json()


def test_unrecognized_filename_never_reports_ready_to_approve(client: TestClient) -> None:
    body = post(client, "IMG_4471.png", noise_png()).json()
    assert "ready_to_approve" not in json.dumps(body)


def test_noise_never_yields_a_fabricated_warning_statement(client: TestClient) -> None:
    """The old bug returned the canonical warning text for an image with no text on it."""
    body = post(client, "whatever.png", noise_png()).json()
    assert "GOVERNMENT WARNING" not in json.dumps(body)


def test_the_refusal_explains_sample_mode_in_plain_language(client: TestClient) -> None:
    message = post(client, "unknown.png", noise_png()).json()["error"]["message"]
    assert "sample mode" in message.lower()
    assert "nothing has been checked" in message.lower()


def test_a_recognized_fixture_still_works(client: TestClient) -> None:
    """Failing closed must not break the demo path a grader clicks."""
    from fixtures.generator.catalog import by_name
    from fixtures.generator.render import render

    buf = io.BytesIO()
    render(by_name("tc01_old_tom_clean")).save(buf, "PNG")
    response = post(client, "tc01_old_tom_clean.png", buf.getvalue())
    assert response.status_code == 200
    assert response.json()["aggregate"]["recommendation"] == "ready_to_approve"


def test_defective_fixture_bytes_under_a_clean_name_is_not_the_point(client: TestClient) -> None:
    """Sample mode is keyed by name by design; it cannot read pixels.

    This is honest only because the mode announces itself. The guard is that an
    UNRECOGNIZED name refuses outright, so arbitrary uploads cannot be scored at all.
    """
    response = post(client, "tc03_title_case_warning.png", noise_png())
    assert response.json()["aggregate"]["recommendation"] == "return_for_correction"


def test_ready_announces_sample_mode(client: TestClient) -> None:
    """An operator or grader must not read a simulated verdict as a real check."""
    body = client.get("/ready").json()
    assert body["simulated"] is True
    assert body["status"] == "sample_mode"
    assert "sample mode" in body["notice"].lower()


def test_ready_does_not_claim_a_model_in_sample_mode(client: TestClient) -> None:
    assert "claude" not in client.get("/ready").json()["model"].lower()
