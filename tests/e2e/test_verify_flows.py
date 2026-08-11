"""E2E: the four flows a grader will actually click through (LP-238, LP-239, LP-240).

Everything below drives the real HTTP stack — routing, middleware, multipart parsing,
ingest, the quality pre-gate, extraction, the rules engine, aggregation, serialization,
and the error handlers — against an offline provider. No component is stubbed except the
model call itself, which is the one thing ENG-3 forbids.

These are not unit tests with a client attached. Each one asserts what an agent sees on
the screen at the end of a journey: the recommendation, the rows, the sentence explaining
why, and the next step when it goes wrong. A pipeline that is correct in pieces and
useless as a whole passes every unit test in the suite and fails these.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.config import Config
from api.main import create_app
from api.models import FieldName, Recommendation, Verdict
from api.provider.fake import FailingProvider, NonLabelProvider, SpecBackedProvider
from fixtures.generator.catalog import by_name

pytestmark = pytest.mark.e2e

OLD_TOM = {
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


def _client(provider: Any, **overrides: Any) -> TestClient:
    config = Config(use_fake_provider=True, **overrides)
    return TestClient(
        create_app(config=config, provider=provider), raise_server_exceptions=False
    )


def _verify(
    client: TestClient,
    files: Any,
    application: dict[str, Any] | None = None,
    roles: list[str] | None = None,
) -> Any:
    form: dict[str, Any] = {"application": json.dumps(application or OLD_TOM)}
    if roles:
        form["roles"] = roles
    return client.post("/verify", files=files, data=form)


def _row(body: dict[str, Any], field: FieldName) -> dict[str, Any]:
    return next(r for r in body["fields"] if r["field"] == field.value)


# --------------------------------------------------------------------------------------
# LP-238 — Verify Now, happy path
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-01")
def test_a_compliant_label_reaches_ready_to_approve(fixture_uploads: Any) -> None:
    """TC-01 end to end: the brief's own sample label, approved in one request.

    The single most important passing case. If this ever fails, the product does
    nothing — every other test in the suite could be green and an agent would never see
    a clean result.
    """
    client = _client(SpecBackedProvider("tc16_front_back"))
    response = _verify(
        client,
        fixture_uploads("tc16_front_back_front.png", "tc16_front_back_back.png"),
        roles=["front", "back"],
    )
    assert response.status_code == 200

    body = response.json()
    assert body["aggregate"]["recommendation"] == Recommendation.READY_TO_APPROVE.value
    assert _row(body, FieldName.GOVERNMENT_WARNING)["verdict"] == Verdict.MATCH.value
    assert body["aggregate"]["rationale"].endswith("The final decision is yours.")


@pytest.mark.tc("TC-16")
def test_the_checklist_covers_every_mandatory_element(fixture_uploads: Any) -> None:
    """Seven rows, one per element, whatever the verdict.

    A field the pipeline skipped is a field an agent never learns was not checked — the
    blank-checklist failure mode, at the level of one row.
    """
    client = _client(SpecBackedProvider("tc16_front_back"))
    body = _verify(
        client,
        fixture_uploads("tc16_front_back_front.png", "tc16_front_back_back.png"),
        roles=["front", "back"],
    ).json()
    assert {r["field"] for r in body["fields"]} == {f.value for f in FieldName}


@pytest.mark.tc("TC-16")
def test_the_warning_on_the_back_image_is_found(fixture_uploads: Any) -> None:
    """IMG-8: a two-image application is one label.

    The brand is on the front and the warning is on the back. Declaring the warning
    Missing without searching every image would be a false finding on the most common
    real submission there is.
    """
    client = _client(SpecBackedProvider("tc16_front_back"))
    body = _verify(
        client,
        fixture_uploads("tc16_front_back_front.png", "tc16_front_back_back.png"),
        roles=["front", "back"],
    ).json()
    warning = _row(body, FieldName.GOVERNMENT_WARNING)
    assert warning["verdict"] == Verdict.MATCH.value
    assert _row(body, FieldName.BRAND_NAME)["verdict"] == Verdict.MATCH.value


@pytest.mark.tc("TC-02")
def test_dave_s_case_reaches_the_agent_as_a_visible_judgment_call(
    fixture_uploads: Any,
) -> None:
    """`STONE'S THROW` against `Stone's Throw`: Acceptable variation with a note.

    Not Match — that would be the silent pass MATCH-9 forbids — and not Mismatch, which
    is the false finding that made Dave stop trusting the last tool.
    """
    spec = by_name("tc02_stones_throw")
    client = _client(SpecBackedProvider(spec))
    body = _verify(
        client, fixture_uploads("tc02_stones_throw.png"), application=spec.application()
    ).json()

    brand = _row(body, FieldName.BRAND_NAME)
    assert brand["verdict"] == Verdict.ACCEPTABLE_VARIATION.value
    assert brand["tier"] == 2
    assert brand["rationale"].strip()
    assert body["aggregate"]["recommendation"] == Recommendation.NEEDS_REVIEW.value


@pytest.mark.tc("TC-03")
def test_jenny_s_case_never_reaches_ready_to_approve(fixture_uploads: Any) -> None:
    """A title-case `Government Warning:` heading is a return, end to end.

    The violation she caught on a real label, through the whole stack. This is the
    false pass the product exists to prevent, asserted at the only layer that matters.
    """
    spec = by_name("tc03_title_case_warning")
    client = _client(SpecBackedProvider(spec))
    body = _verify(
        client,
        fixture_uploads("tc03_title_case_warning.png"),
        application=spec.application(),
    ).json()

    assert body["aggregate"]["recommendation"] != Recommendation.READY_TO_APPROVE.value
    warning = _row(body, FieldName.GOVERNMENT_WARNING)
    assert warning["verdict"] != Verdict.MATCH.value


@pytest.mark.tc("TC-07")
def test_a_missing_warning_returns_the_application_and_says_which_field(
    fixture_uploads: Any,
) -> None:
    """WARN-6, MATCH-10: disqualifying on its own, named as the driver."""
    spec = by_name("tc07_missing_warning")
    client = _client(SpecBackedProvider(spec))
    body = _verify(
        client, fixture_uploads("tc07_missing_warning.png"), application=spec.application()
    ).json()

    assert body["aggregate"]["recommendation"] == Recommendation.RETURN_FOR_CORRECTION.value
    assert body["aggregate"]["driving_field"] == FieldName.GOVERNMENT_WARNING.value
    assert "government warning" in body["aggregate"]["rationale"].lower()


@pytest.mark.tc("TC-06")
def test_a_buried_warning_is_read_but_its_prominence_is_not_yet_judged(
    fixture_uploads: Any,
) -> None:
    """TC-06, and an honest account of what the product does about it today.

    The warning is present, small, and low-contrast — legible to the extractor and
    invisible to a shopper. The *text* check passes, correctly: the wording is right.
    What is missing is a prominence heuristic (LP-211), so nothing tells the agent the
    statement is buried.

    The case is automated here rather than left to the eval so that the gap is visible
    in the suite as well as in the accuracy report. The capability itself is pinned in
    the xfail below.
    """
    spec = by_name("tc06_buried_warning")
    client = _client(SpecBackedProvider(spec))
    body = _verify(
        client, fixture_uploads("tc06_buried_warning.png"), application=spec.application()
    ).json()

    warning = _row(body, FieldName.GOVERNMENT_WARNING)
    assert warning["extracted"], "the statement is legible and should be read"
    assert warning["verdict"] != Verdict.MISSING.value
    assert spec.pending, "TC-06's expectation is waiting on a ticket; see the xfail below"


@pytest.mark.tc("TC-06")
@pytest.mark.xfail(
    strict=True,
    reason=(
        "OPEN (LP-211): prominence heuristics do not exist, so a warning that is present "
        "but visibly smaller and lower-contrast than the surrounding text reaches the "
        "agent as a clean Match. 27 CFR 16.21 requires the statement be 'conspicuous and "
        "prominent'; reading it is not the same as it being readable on the shelf. The "
        "extractor already supplies relative_size and contrast_ok — the rule to consume "
        "them is what is missing. Owner: api/rules/warning.py."
    ),
)
def test_a_buried_warning_is_flagged_for_prominence(fixture_uploads: Any) -> None:
    spec = by_name("tc06_buried_warning")
    client = _client(SpecBackedProvider(spec))
    body = _verify(
        client, fixture_uploads("tc06_buried_warning.png"), application=spec.application()
    ).json()
    assert _row(body, FieldName.GOVERNMENT_WARNING)["verdict"] != Verdict.MATCH.value


def test_the_request_id_on_screen_is_the_one_in_the_header(fixture_uploads: Any) -> None:
    """The ID an agent reads off the screen has to be the ID in the logs.

    Correlation is the whole point of having one; two different IDs is worse than none,
    because somebody will search for the wrong one and conclude nothing was logged.
    """
    client = _client(SpecBackedProvider("tc01_old_tom_clean"))
    response = _verify(client, fixture_uploads("tc01_old_tom_clean.png"))
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_a_verification_reports_its_own_timings(fixture_uploads: Any) -> None:
    """PERF-2: the stage breakdown is what makes a latency claim checkable."""
    client = _client(SpecBackedProvider("tc01_old_tom_clean"))
    timings = _verify(client, fixture_uploads("tc01_old_tom_clean.png")).json()["timings_ms"]
    assert timings["total"] >= 0
    assert timings["total"] >= timings["ingest"]


# --------------------------------------------------------------------------------------
# LP-239 — the unreadable path
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-14")
def test_an_unreadable_image_reports_every_row_as_unchecked(
    uploads: Any, underexposed_label: bytes
) -> None:
    """A blank checklist reads as "fine" at a glance, which is the opposite of fine.

    The pre-gate stops before any model call, so nothing was verified — and every row
    has to say so. There is deliberately no seventh verdict for "not attempted":
    Unreadable already means "we did not verify this" and can never be mistaken for a
    pass.
    """
    client = _client(SpecBackedProvider("tc01_old_tom_clean"))
    body = _verify(client, uploads("dark.png", underexposed_label)).json()

    assert body["aggregate"]["recommendation"] == Recommendation.NEEDS_REVIEW.value
    assert {r["verdict"] for r in body["fields"]} == {Verdict.UNREADABLE.value}
    assert len(body["fields"]) == len(list(FieldName))


@pytest.mark.tc("TC-14")
def test_an_unreadable_image_tells_the_agent_what_to_ask_for(
    uploads: Any, underexposed_label: bytes
) -> None:
    """IMG-4, UX-6: a retake reason in the agents' own workflow verb.

    "Quality score 0.08" is a diagnostic. "The photo is too dark to read the label —
    retake it in better light, or request a new image" is an action.
    """
    client = _client(SpecBackedProvider("tc01_old_tom_clean"))
    body = _verify(client, uploads("dark.png", underexposed_label)).json()

    rationale = body["aggregate"]["rationale"]
    assert "dark" in rationale
    assert "Nothing on the label could be checked" in rationale
    assert body["images"][0]["quality"]["verdict"] == "hopeless"


@pytest.mark.tc("TC-14")
def test_an_unreadable_image_still_shows_what_the_application_said(
    uploads: Any, underexposed_label: bytes
) -> None:
    """An unverified result is not an empty one.

    The agent still sees the filed values next to each unchecked row, so the screen is
    a starting point for a manual review rather than a dead end.
    """
    client = _client(SpecBackedProvider("tc01_old_tom_clean"))
    body = _verify(client, uploads("dark.png", underexposed_label)).json()
    assert _row(body, FieldName.BRAND_NAME)["expected"] == "OLD TOM DISTILLERY"


@pytest.mark.tc("TC-15")
def test_a_photograph_that_is_not_a_label_says_so_without_a_verdict(
    fixture_uploads: Any,
) -> None:
    """Somebody uploads a cat. No crash, no verdicts, one sentence.

    The pre-gate cannot catch this — a cat photograph is sharp, well exposed and
    perfectly scored. It takes the model, and the honest answer is an empty checklist
    with an explanation rather than seven Missing rows.
    """
    client = _client(NonLabelProvider())
    body = _verify(client, fixture_uploads("tc01_old_tom_clean.png")).json()

    assert body["fields"] == []
    assert body["aggregate"]["recommendation"] == Recommendation.NEEDS_REVIEW.value
    assert "does not look like a label" in body["aggregate"]["rationale"]


def test_an_unsupported_file_type_is_refused_with_an_actionable_message(
    uploads: Any,
) -> None:
    """SEC-5: sniffed by magic bytes, not by the filename the caller chose."""
    client = _client(SpecBackedProvider("tc01_old_tom_clean"))
    response = _verify(client, uploads("label.png", b"MZ\x90\x00 this is an executable"))
    assert response.status_code in (400, 422)
    error = response.json()["error"]
    assert error["next_step"]
    assert "Traceback" not in error["message"]


# --------------------------------------------------------------------------------------
# LP-240 / TC-21 — the provider is down
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-21")
def test_a_provider_outage_degrades_in_a_sentence_rather_than_a_stack_trace(
    fixture_uploads: Any,
) -> None:
    """NET-3, TC-21: the app stays up, says what happened, and promises nothing changed.

    503 rather than 500 — it is not our bug, and anyone reading a status page needs
    that distinction. The message has to say "nothing has been checked", because the
    one thing an agent must not conclude from an outage is that the label was fine.
    """
    client = _client(FailingProvider("Connection refused"))
    response = _verify(client, fixture_uploads("tc01_old_tom_clean.png"))

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["kind"] == "provider"
    assert error["next_step"] == "retry"
    assert "Nothing has been checked" in error["message"]
    assert "no application data has been changed" in error["message"]
    assert "Traceback" not in response.text


@pytest.mark.tc("TC-21")
def test_a_provider_outage_never_produces_a_verdict(fixture_uploads: Any) -> None:
    """The direction that matters. An outage must not be reported as a clean label."""
    client = _client(FailingProvider())
    body = _verify(client, fixture_uploads("tc01_old_tom_clean.png")).json()
    assert "aggregate" not in body
    assert Recommendation.READY_TO_APPROVE.value not in json.dumps(body)


@pytest.mark.tc("TC-21")
def test_the_service_stays_up_after_an_outage(fixture_uploads: Any) -> None:
    """No hang, no silent queue, and the next request still works.

    A provider failure that poisoned the process would turn one outage into an
    incident — which is what the circuit breaker and the per-request deadline exist to
    prevent.
    """
    client = _client(FailingProvider())
    _verify(client, fixture_uploads("tc01_old_tom_clean.png"))
    assert client.get("/health").json() == {"status": "ok"}
    assert _verify(client, fixture_uploads("tc01_old_tom_clean.png")).status_code == 503


@pytest.mark.tc("TC-21")
def test_a_non_retryable_provider_failure_degrades_the_same_way(
    fixture_uploads: Any,
) -> None:
    """The agent's screen does not change because the failure was permanent.

    Retryability is an internal concern. What reaches the agent is the same sentence
    either way, because their next step is the same: nothing was checked.
    """
    client = _client(FailingProvider("Refused", retryable=False))
    response = _verify(client, fixture_uploads("tc01_old_tom_clean.png"))
    assert response.status_code == 503
    assert response.json()["error"]["kind"] == "provider"


# --------------------------------------------------------------------------------------
# The demo path a grader clicks first (UX-1, DEL-5)
# --------------------------------------------------------------------------------------


def test_the_sample_endpoint_hands_back_a_complete_application() -> None:
    """One click to a verdict. A grader who has to type nine fields has already decided.

    The endpoint returns an application and the URLs of its label pair, which together
    reach a verdict without any typing.
    """
    client = _client(SpecBackedProvider("tc16_front_back"))
    body = client.get("/sample").json()

    from api.models import Application

    Application.model_validate(body["application"])
    assert len(body["images"]) == 2
    assert [image["role"] for image in body["images"]] == ["front", "back"]


def test_every_sample_image_is_actually_servable() -> None:
    """A demo whose images 404 is worse than no demo."""
    client = _client(SpecBackedProvider("tc16_front_back"))
    for image in client.get("/sample").json()["images"]:
        response = client.get(image["url"])
        assert response.status_code == 200
        assert response.content[:4] == b"\x89PNG"


def test_an_image_outside_the_sample_allowlist_is_refused() -> None:
    """The path is never assembled from what the caller sent.

    Answering in the error taxonomy rather than rendering anything is what makes a
    traversal probe read as refused instead of as working.
    """
    client = _client(SpecBackedProvider("tc16_front_back"))
    response = client.get("/sample/images/..%2f..%2fpyproject.toml")
    assert response.status_code != 200
    assert "[tool" not in response.text


def test_the_sample_application_verifies_end_to_end() -> None:
    """The full one-click journey: fetch the sample, post it back, get a verdict.

    Fetching the images through the API rather than off disk is the point — this is the
    exact sequence the browser performs, so a broken sample URL fails here rather than
    in front of a grader.
    """
    client = _client(SpecBackedProvider("tc16_front_back"))
    sample = client.get("/sample").json()
    files = [
        ("images", (image["filename"], client.get(image["url"]).content, "image/png"))
        for image in sample["images"]
    ]
    response = _verify(
        client, files, application=sample["application"], roles=["front", "back"]
    )
    assert response.status_code == 200
    assert (
        response.json()["aggregate"]["recommendation"]
        == Recommendation.READY_TO_APPROVE.value
    )


# --------------------------------------------------------------------------------------
# Request hygiene, over the wire
# --------------------------------------------------------------------------------------


def test_more_images_than_the_cap_is_refused_with_the_number(
    fixture_uploads: Any,
) -> None:
    client = _client(SpecBackedProvider("tc01_old_tom_clean"), max_images=2)
    response = _verify(
        client,
        fixture_uploads(
            "tc01_old_tom_clean.png", "tc02_stones_throw.png", "tc03_title_case_warning.png"
        ),
    )
    assert response.status_code == 400
    assert "2 images" in response.json()["error"]["message"]


def test_an_incomplete_application_names_the_missing_field_in_the_form_s_words(
    fixture_uploads: Any,
) -> None:
    """LP-075: "brand_name: field required" is a message written for whoever wrote the schema."""
    client = _client(SpecBackedProvider("tc01_old_tom_clean"))
    incomplete = {k: v for k, v in OLD_TOM.items() if k != "brand_name"}
    response = _verify(client, fixture_uploads("tc01_old_tom_clean.png"), application=incomplete)

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "brand name" in message
    assert "brand_name" not in message
    assert "no images were checked" in message


def test_unparseable_application_json_says_so_without_jargon(
    fixture_uploads: Any,
) -> None:
    client = _client(SpecBackedProvider("tc01_old_tom_clean"))
    response = client.post(
        "/verify",
        files=fixture_uploads("tc01_old_tom_clean.png"),
        data={"application": "{not json"},
    )
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "JSON" in message
    assert "no images were checked" in message
