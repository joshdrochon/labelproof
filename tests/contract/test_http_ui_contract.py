"""CONTRACT: what the server sends and what the browser expects are the same thing.

`web/src/types.ts` says of itself that it is "a copy of the server's pydantic models, not
an interpretation of them". Nothing enforced that. The Python suite tests the API and the
vitest suite tests the components, and the seam between them — the wire format — was
checked by two files agreeing with each other by hand.

This is the same shape as the incident. The Python side was fully tested, the TypeScript
side was fully tested, and the contract between them was the thing nobody exercised.

So these tests parse `types.ts` and compare it, field by field and union member by union
member, against the pydantic models and the enums. A field added to `FieldResult` without
a matching line in `types.ts` fails here, offline, on every run — instead of appearing as
`undefined` in a component three deploys later.

Then they check the *served* payload too, because a model and a serialization are not the
same thing: `model_dump()` renames, excludes and aliases, and only the response body is
what the browser actually receives.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from api import errors
from api.config import Config
from api.main import create_app
from api.models import (
    Aggregate,
    Application,
    BoundingBox,
    Commodity,
    Cost,
    Evidence,
    FieldName,
    FieldResult,
    Finding,
    ImageQuality,
    ImageReport,
    Recommendation,
    Timings,
    Verdict,
    VerificationResult,
)

pytestmark = pytest.mark.contract

TYPES_TS = Path(__file__).resolve().parents[2] / "web" / "src" / "types.ts"


# --------------------------------------------------------------------------------------
# A small TypeScript reader
# --------------------------------------------------------------------------------------


def _source() -> str:
    """The TypeScript wire contract, or a hard failure.

    `pytest.fail`, not `pytest.skip`. Skipping evaporated the entire layer: delete
    `web/src/types.ts` and all 67 tests here reported SKIPPED and the run went green —
    so a slim checkout, or a Docker build that excludes `web/`, silently ran with no
    HTTP-UI contract at all. Another agent's UI tests already vanished exactly this way
    on this project.

    A missing contract file is not a reason to check less; it is the loudest possible
    signal that something is wrong with the checkout. `test_golden_set_contract.py`
    already fails rather than skips on its missing input, and this now matches it.
    """
    if not TYPES_TS.exists():  # pragma: no cover - the file is committed
        pytest.fail(
            "web/src/types.ts is missing, so the HTTP/UI wire contract is unchecked. "
            "This is a failure rather than a skip on purpose — see the docstring."
        )
    return TYPES_TS.read_text()


def _interface_fields(name: str) -> dict[str, str]:
    """Field name -> declared type, for one `export interface`.

    A deliberately small parser. It reads what this file actually contains — flat
    interfaces of `name: type;` lines with `//` and `/** */` comments — and nothing
    more. A real TypeScript parser would be a dependency and a maintenance surface for
    a job this size; the failure mode of the simple one is a loud `KeyError` rather
    than a silently wrong answer.
    """
    match = re.search(rf"export interface {name} \{{(.*?)\n\}}", _source(), re.S)
    assert match, f"no `export interface {name}` in types.ts"

    body = re.sub(r"/\*\*.*?\*/", "", match.group(1), flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)

    fields: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip().rstrip(";")
        if not line:
            continue
        key, _, declared = line.partition(":")
        fields[key.strip().rstrip("?")] = declared.strip()
    return fields


def _optional_fields(name: str) -> set[str]:
    """The `name?:` fields of ONE interface.

    Scoped per interface deliberately. The first version scanned the whole file for
    `(\\w+)\\?:` and exempted those names in *every* interface — so because
    `ImageReport.url?` exists, adding a required `url: string;` to `Aggregate` passed
    clean even though the server never sends one. Same for `next_step`. The docstring
    below calls that "the more dangerous direction", and it was the one direction the
    check was holed in.
    """
    match = re.search(rf"export interface {name} \{{(.*?)\n\}}", _source(), re.S)
    assert match, f"no `export interface {name}` in types.ts"
    body = re.sub(r"/\*\*.*?\*/", "", match.group(1), flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"(\w+)\?:", body))


def _union_members(name: str) -> set[str]:
    """The string literals of an `export type X = 'a' | 'b';`."""
    match = re.search(rf"export type {name} =(.*?);", _source(), re.S)
    assert match, f"no `export type {name}` in types.ts"
    return set(re.findall(r"'([^']+)'", match.group(1)))


#: (pydantic model, TypeScript interface). Every model that crosses the wire.
MODEL_PAIRS = [
    (VerificationResult, "VerificationResult"),
    (FieldResult, "FieldResult"),
    (Aggregate, "Aggregate"),
    (Finding, "Finding"),
    (Evidence, "Evidence"),
    (BoundingBox, "BoundingBox"),
    (ImageQuality, "ImageQuality"),
    (ImageReport, "ImageReport"),
    (Timings, "Timings"),
    (Cost, "Cost"),
    (Application, "Application"),
]

#: (python enum, TypeScript union). Every closed set the UI branches on.
ENUM_PAIRS = [
    (Verdict, "Verdict"),
    (Recommendation, "Recommendation"),
    (FieldName, "FieldName"),
    (Commodity, "Commodity"),
    (errors.ErrorKind, "ErrorKind"),
]


# --------------------------------------------------------------------------------------
# Enums: the UI branches on these, so a missing member is a blank screen
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("enum", "union"), ENUM_PAIRS, ids=[u for _, u in ENUM_PAIRS])
def test_every_enum_matches_its_typescript_union_exactly(enum: Any, union: str) -> None:
    """Both directions. A server value the UI has no case for renders as nothing.

    `Verdict` is fixed at exactly six values (MATCH-1) and the UI has a chip for each.
    A seventh added on the server without a chip is a row that renders blank — a field
    an agent sees no verdict for and reasonably reads as "fine".
    """
    assert {member.value for member in enum} == _union_members(union)


def test_the_verdict_taxonomy_is_still_exactly_six_values() -> None:
    """MATCH-1. A seventh verdict is a product decision, not an implementation convenience.

    Pinned on both sides at once, because "add a verdict" is the change most likely to
    be made in one place and remembered in the other.
    """
    assert len(list(Verdict)) == 6
    assert len(_union_members("Verdict")) == 6


# --------------------------------------------------------------------------------------
# Interfaces: field-for-field
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("model", "interface"), MODEL_PAIRS, ids=[i for _, i in MODEL_PAIRS])
def test_every_model_field_is_declared_in_typescript(model: Any, interface: str) -> None:
    """A field the server sends that the UI does not know about is data thrown away.

    Usually harmless; occasionally it is the evidence box, the finding citation, or the
    rationale an agent needs to make an override decision (HITL-4).
    """
    declared = _interface_fields(interface)
    missing = sorted(set(model.model_fields) - set(declared))
    assert missing == [], f"{interface} is missing: {missing}"


@pytest.mark.parametrize(("model", "interface"), MODEL_PAIRS, ids=[i for _, i in MODEL_PAIRS])
def test_typescript_declares_no_field_the_server_does_not_send(
    model: Any, interface: str
) -> None:
    """The more dangerous direction: a field the UI reads and the server never sends.

    It arrives as `undefined`, renders as empty, and looks like an answer. `ImageReport.url`
    is the one known exception — it is declared optional precisely because the server does
    not send it yet, and the component says so.

    The exemption is per interface. Reading optionals from the whole file made
    `ImageReport.url?` exempt `url` everywhere, so a required `url: string;` added to
    `Aggregate` — which the server never sends — passed clean.
    """
    declared = set(_interface_fields(interface))
    extra = sorted(declared - set(model.model_fields) - _optional_fields(interface))
    assert extra == [], f"{interface} declares fields the server never sends: {extra}"


def test_only_the_known_gap_is_declared_optional() -> None:
    """Optionality is the escape hatch, so the list of users of it is worth pinning.

    `ImageReport.url` is optional because the server does not send the preprocessed
    image URL yet and the component falls back to the local upload. That is a documented
    contract gap. A second optional field appearing is either another gap that needs
    documenting or somebody silencing this file.
    """
    optional = {
        f"{interface}.{field}"
        for _, interface in MODEL_PAIRS
        for field in _optional_fields(interface)
    }
    assert optional == {"ImageReport.url"}, sorted(optional)


@pytest.mark.parametrize(("model", "interface"), MODEL_PAIRS, ids=[i for _, i in MODEL_PAIRS])
def test_every_optional_python_field_is_nullable_in_typescript(
    model: Any, interface: str
) -> None:
    """`str | None` on the server must be `| null` in the browser.

    Otherwise TypeScript believes a value is always present, the component reads
    `.length` on it, and the first label with no country of origin throws.
    """
    declared = _interface_fields(interface)
    for name, field in model.model_fields.items():
        annotation = str(field.annotation)
        if "None" not in annotation and "Optional" not in annotation:
            continue
        assert "null" in declared[name], f"{interface}.{name} is optional but not nullable"


@pytest.mark.parametrize(("model", "interface"), MODEL_PAIRS, ids=[i for _, i in MODEL_PAIRS])
def test_list_fields_are_arrays_and_scalar_fields_are_not(
    model: Any, interface: str
) -> None:
    """Cardinality has to agree or the UI maps over a scalar."""
    declared = _interface_fields(interface)
    for name, field in model.model_fields.items():
        is_list = str(field.annotation).startswith("list[")
        assert is_list == ("[]" in declared[name]), f"{interface}.{name} cardinality"


# --------------------------------------------------------------------------------------
# The served payload, not just the model
# --------------------------------------------------------------------------------------


@pytest.fixture
def verified_body(fixture_uploads: Any) -> dict[str, Any]:
    """A real `POST /verify` response, over the real HTTP stack, offline."""
    import json as _json

    from api.provider.fake import SpecBackedProvider

    app = create_app(
        config=Config(use_fake_provider=True),
        provider=SpecBackedProvider("tc16_front_back"),
    )
    client = TestClient(app)
    response = client.post(
        "/verify",
        files=fixture_uploads("tc16_front_back_front.png", "tc16_front_back_back.png"),
        data={
            "application": _json.dumps(
                {
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
            ),
            "roles": ["front", "back"],
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_the_served_result_carries_exactly_the_declared_top_level_keys(
    verified_body: dict[str, Any],
) -> None:
    """Serialization is not the model. `model_dump` renames, excludes and aliases.

    `timings_ms` is the example: the model field is `timings_ms` and the type is
    `Timings`, and only the response body proves which name reaches the browser.
    """
    assert set(verified_body) == set(_interface_fields("VerificationResult"))


def test_every_served_field_row_carries_the_declared_keys(
    verified_body: dict[str, Any],
) -> None:
    declared = set(_interface_fields("FieldResult"))
    rows: list[dict[str, Any]] = verified_body["fields"]
    for row in rows:
        assert set(row) == declared


def test_every_served_verdict_is_one_the_ui_can_render(
    verified_body: dict[str, Any],
) -> None:
    """The end-to-end version of the enum test, on real output."""
    renderable = _union_members("Verdict")
    for row in verified_body["fields"]:
        assert row["verdict"] in renderable


def test_the_served_recommendation_is_one_the_ui_can_render(
    verified_body: dict[str, Any],
) -> None:
    assert verified_body["aggregate"]["recommendation"] in _union_members("Recommendation")


def test_every_served_field_name_is_one_the_ui_has_a_row_for(
    verified_body: dict[str, Any],
) -> None:
    known = _union_members("FieldName")
    for row in verified_body["fields"]:
        assert row["field"] in known


def test_the_served_timings_and_cost_carry_their_declared_keys(
    verified_body: dict[str, Any],
) -> None:
    assert set(verified_body["timings_ms"]) == set(_interface_fields("Timings"))
    assert set(verified_body["cost"]) == set(_interface_fields("Cost"))


def test_the_served_image_reports_carry_their_declared_keys(
    verified_body: dict[str, Any],
) -> None:
    """`url` is optional on the TypeScript side and absent here — the known gap."""
    declared = set(_interface_fields("ImageReport"))
    reports: list[dict[str, Any]] = verified_body["images"]
    for report in reports:
        assert set(report) <= declared
        assert set(report["quality"]) == set(_interface_fields("ImageQuality"))


# --------------------------------------------------------------------------------------
# The error envelope
# --------------------------------------------------------------------------------------


def test_the_error_envelope_matches_the_declared_shape() -> None:
    """One error renderer in the UI, so one shape from the server (UX-6, OPS-5).

    Every failure path — validation, provider, internal, a wrong URL — leaves as this
    object. A path that emitted a bare string or a FastAPI `detail` would render as
    nothing in the one component that handles errors.
    """
    declared = set(_interface_fields("ApiError"))
    envelope = errors.UserError("x", next_step="retry", code="c").to_payload()
    payload = cast("dict[str, Any]", envelope["error"])
    assert set(payload) == declared


@pytest.mark.parametrize(
    "error",
    [
        errors.UserError("x", next_step="fix", code="invalid"),
        errors.ImageError("x"),
        errors.ProviderUnavailable(),
        errors.InternalError(),
        errors.file_too_large(10),
        errors.unsupported_file_type("PDF"),
        errors.not_a_label(),
        errors.unreadable("too dark"),
    ],
    ids=lambda e: type(e).__name__ + ":" + e.code,
)
def test_every_error_the_app_can_raise_serialises_to_the_declared_shape(
    error: errors.LabelProofError,
) -> None:
    declared = set(_interface_fields("ApiError"))
    payload = cast("dict[str, Any]", error.to_payload()["error"])
    assert set(payload) == declared
    assert payload["kind"] in _union_members("ErrorKind")
    assert isinstance(payload["message"], str) and payload["message"].strip()


def test_a_real_error_response_matches_the_declared_shape() -> None:
    """Over HTTP, because the envelope is wrapped in `{"error": ...}` on the way out.

    Probed at an unknown address *under an API prefix*. A bare unknown path is owned by
    the client-side router once `web/dist` exists, so it answers 200 with `index.html`
    rather than an error envelope — which made this test's result depend on whether the
    SPA had been built. See `tests/regression/test_routing_defects.UNKNOWN_API_ADDRESS`.
    """
    client = TestClient(
        create_app(config=Config(use_fake_provider=True), provider=None),
        raise_server_exceptions=False,
    )
    body = client.get("/health/nope").json()
    assert set(body) == {"error"}
    assert set(body["error"]) == set(_interface_fields("ApiError"))
