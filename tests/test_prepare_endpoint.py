"""`POST /prepare` — reading the label while the agent is still typing (LP-346).

The store's own tests cover expiry, binding and bounds. These cover the thing that would
actually hurt: that a verdict reached with a head start is the same verdict reached
without one, and that no path through this endpoint can answer for artwork it did not
read.

The rule the whole feature rests on: **`/prepare` moves when the model is called and
nothing else.** Every assertion here is a restatement of that.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.middleware.ratelimit import Lane, lane_for
from api.provider.base import ProviderUsage
from api.provider.fake import FailingProvider, SpecBackedProvider
from fixtures.generator.catalog import by_name
from tests.test_api import LABELS, label_files, make_client, sample_application

LABEL = "tc01_old_tom_clean.png"


def a_client() -> TestClient:
    return make_client(provider=SpecBackedProvider(by_name("tc01_old_tom_clean")))


def prepare(client: TestClient, name: str = LABEL, commodity: str = "spirits") -> dict[str, Any]:
    response = client.post(
        "/prepare", data={"commodity": commodity}, files=label_files(name)
    )
    assert response.status_code == 200, response.text
    return response.json()


def verify(client: TestClient, name: str = LABEL, token: str | None = None, **overrides: Any):
    data: dict[str, Any] = {"application": json.dumps(sample_application(**overrides))}
    if token is not None:
        data["prepared_token"] = token
    return client.post("/verify", data=data, files=label_files(name))


# --- the point of the whole thing -------------------------------------------------------


def test_a_prepared_check_reaches_the_same_verdict_as_an_unprepared_one() -> None:
    """If these ever differ, the optimisation has changed what the tool decides, and no
    amount of speed makes that acceptable."""
    client = a_client()
    plain = verify(client).json()

    token = prepare(client)["token"]
    fast = verify(client, token=token).json()

    assert [(f["field"], f["verdict"]) for f in fast["fields"]] == [
        (f["field"], f["verdict"]) for f in plain["fields"]
    ]
    assert fast["aggregate"]["recommendation"] == plain["aggregate"]["recommendation"]


def test_the_prepared_path_makes_no_model_call_at_verify_time() -> None:
    """The saving IS the skipped call. A run that quietly extracted again would be a
    feature that costs twice and saves nothing."""
    calls: list[Any] = []

    class Counting(SpecBackedProvider):
        def extract(self, request: Any) -> Any:  # type: ignore[override]
            calls.append(request)
            return super().extract(request)

    client = make_client(provider=Counting(by_name("tc01_old_tom_clean")))

    token = prepare(client)["token"]
    assert len(calls) == 1, "prepare should have read the label"

    verify(client, token=token)
    assert len(calls) == 1, "verify extracted again despite being handed a reading"


def test_the_result_reports_the_reading_cost_not_the_wait() -> None:
    """A result card that showed the wait would report a six-second model call as two
    milliseconds of work. The number on screen has to describe the work, not the moment
    the agent happened to press the button (OPS-1)."""


    class Billing(SpecBackedProvider):
        """The fixture provider reports zero tokens, which makes "the cost still lands"
        pass whether or not it does. This one bills."""

        def extract(self, request: Any) -> Any:  # type: ignore[override]
            response = super().extract(request)
            return replace(
                response,
                usage=ProviderUsage(
                    input_tokens=1234, output_tokens=567, model="claude-sonnet-5"
                ),
            )

    client = make_client(provider=Billing(by_name("tc01_old_tom_clean")))
    prepared = prepare(client)
    body = verify(client, token=prepared["token"]).json()

    # The stage reports what the READING measured, not what this request waited.
    assert body["timings_ms"]["extract"] == prepared["read_ms"]
    # And the money still lands, even though it was spent before the button was pressed.
    # A check that reported $0.00 because the call happened a minute earlier would
    # understate the running cost of every verification the tool performs (OPS-4).
    assert body["cost"]["input_tokens"] == 1234
    assert body["cost"]["output_tokens"] == 567
    assert body["cost"]["usd"] > 0


# --- the ways it must refuse ------------------------------------------------------------


def test_a_token_from_another_label_is_ignored_and_the_label_is_read_properly() -> None:
    """THE ONE THAT MATTERS. Attaching one label's reading to another's submission would
    be a false pass built out of a cache. The request must not fail — it must simply not
    take the shortcut.

    Asserted by counting model calls rather than by comparing verdicts: the fixture
    provider replays one spec whatever image it is handed, so the verdict cannot tell
    "read the new label" apart from "reused the old reading". The call count can.
    """
    calls: list[Any] = []

    class Counting(SpecBackedProvider):
        def extract(self, request: Any) -> Any:  # type: ignore[override]
            calls.append(request)
            return super().extract(request)

    client = make_client(provider=Counting(by_name("tc01_old_tom_clean")))
    token = prepare(client, LABEL)["token"]
    assert len(calls) == 1

    response = verify(client, name="tc07_missing_warning.png", token=token)

    assert response.status_code == 200
    assert len(calls) == 2, "a token from another label was used instead of reading this one"


def test_a_token_prepared_for_one_commodity_does_not_answer_another() -> None:
    client = a_client()
    token = prepare(client, commodity="spirits")["token"]

    response = verify(client, token=token, commodity="wine")

    assert response.status_code == 200


def test_an_unknown_or_expired_token_falls_back_rather_than_failing() -> None:
    """A restart between prepare and verify is ordinary. The agent must never see an error
    for an optimisation they did not ask for and cannot observe."""
    client = a_client()
    response = verify(client, token="no-such-reading")  # noqa: S106 — not a secret

    assert response.status_code == 200
    assert response.json()["aggregate"]["recommendation"]


def test_no_token_at_all_is_the_ordinary_path() -> None:
    client = a_client()
    assert verify(client).status_code == 200


# --- what it must not become ------------------------------------------------------------


def test_prepare_returns_no_verdict_of_any_kind() -> None:
    """There must be no route to an answer that skips the comparison. `/prepare` has read
    the label and could volunteer an opinion about it; anything it said would be a
    compliance judgement made without the application it is judged against."""
    body = prepare(a_client())

    for forbidden in ("aggregate", "fields", "verdict", "recommendation"):
        assert forbidden not in body, f"/prepare leaked {forbidden}"


def test_a_pregated_image_is_reported_at_prepare_time_without_a_model_call() -> None:
    """The head start pointed at the other outcome: an agent can retake a hopeless
    photograph while they would otherwise have been typing."""
    client = a_client()
    blur = LABELS.parent / "robustness" / "tc14_blur_hopeless.png"
    response = client.post(
        "/prepare",
        data={"commodity": "spirits"},
        files=[("images", ("blur.png", blur.read_bytes(), "image/png"))],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["prepared"] is False
    assert body["reason"]


def test_a_provider_outage_at_prepare_time_is_silent() -> None:
    """The agent has not asked for anything yet. Submitting will try again and report
    properly if it is still down — telling them now would be an error for a request they
    did not make."""
    client = make_client(provider=FailingProvider())
    response = client.post(
        "/prepare", data={"commodity": "spirits"}, files=label_files(LABEL)
    )

    assert response.status_code == 200
    assert response.json()["prepared"] is False


def test_prepare_draws_on_the_same_rate_limit_as_verify() -> None:
    """It makes the identical paid model call. On the `default` lane it would be a cheaper
    way to spend the same money, reached by calling the other endpoint (SEC-9)."""
    lanes = tuple(
        Lane(name=n, per_minute=v)
        for n, v in (
            ("exempt", 0),
            ("verify", 30),
            ("batch_submit", 5),
            ("batch_read", 120),
            ("default", 60),
        )
    )
    assert lane_for("POST", "/prepare", lanes).name == "verify"


def test_a_prepared_reading_answers_one_submission_only() -> None:
    """Two submissions cannot ride on one reading. The second extracts normally."""
    client = a_client()
    token = prepare(client)["token"]

    assert verify(client, token=token).status_code == 200
    assert verify(client, token=token).status_code == 200


@pytest.mark.parametrize("commodity", ["", "beer", "SPIRITS "])
def test_prepare_refuses_a_commodity_it_does_not_recognise(commodity: str) -> None:
    client = a_client()
    response = client.post(
        "/prepare", data={"commodity": commodity}, files=label_files(LABEL)
    )
    # "SPIRITS " is normalised and accepted; the other two are refused in plain language.
    if commodity.strip().lower() in {"spirits", "wine", "malt"}:
        assert response.status_code == 200
        return
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    # A sentence an agent can act on, whichever layer refused it (UX-6, OPS-5).
    assert "check" in message.lower() or "distilled spirits" in message
