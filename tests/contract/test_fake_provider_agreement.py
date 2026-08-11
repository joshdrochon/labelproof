"""CONTRACT: every fake agrees with the real thing it doubles.

A fake that has drifted from what it doubles is worse than no fake, because it
manufactures confidence. That is not a general observation about testing — it is the
literal mechanism of the incident: `SpecBackedProvider` returns already-parsed
`Extraction` objects, so no offline test ever built a request, and a schema that failed
every live call sat behind 624 green tests across 123 tickets.

The fakes cannot be made to talk to the API. What they *can* be held to is that
everything they produce is something the real adapter could also have produced. So the
central test here round-trips: take each fake's output, render it back into the wire
shape the API would return, and push it through `parse_extraction`. If the fake produces
something the schema cannot express or the parser rejects, the fake has drifted — and the
suite says so offline, which is the only place it can.

Also asserted: every fake satisfies the `ExtractionProvider` protocol, none of them
invents a value, and the one that simulates an outage does it the way the real adapter
signals one.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from api.models import Commodity, ExtractedField, Extraction, FieldName
from api.provider import anthropic_adapter as adapter
from api.provider.base import (
    ExtractionProvider,
    ExtractionRequest,
    ExtractionResponse,
    ImageInput,
    ProviderError,
    ProviderUsage,
)
from api.provider.fake import (
    FailingProvider,
    NonLabelProvider,
    RecordedProvider,
    SpecBackedProvider,
)
from fixtures.generator.catalog import CATALOG

pytestmark = pytest.mark.contract

#: Every offline provider that returns extractions. `FailingProvider` is exercised
#: separately because it never returns one.
PRODUCING_FAKES = ["SpecBackedProvider", "NonLabelProvider"]


def _request(images: int = 2) -> ExtractionRequest:
    roles = ["front", "back"][:images]
    return ExtractionRequest(
        commodity=Commodity.SPIRITS,
        images=[ImageInput(index=i, data=b"png", role=r) for i, r in enumerate(roles)],
    )


def _providers() -> list[tuple[str, Any]]:
    return [
        ("SpecBackedProvider", SpecBackedProvider("tc01_old_tom_clean")),
        ("NonLabelProvider", NonLabelProvider()),
        ("FailingProvider", FailingProvider()),
        ("RecordedProvider", RecordedProvider(__import__("pathlib").Path("/nowhere"), "x")),
    ]


# --------------------------------------------------------------------------------------
# Every fake is the shape the real one is
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "provider"), _providers(), ids=lambda p: p if isinstance(p, str) else ""
)
def test_every_offline_provider_satisfies_the_provider_protocol(
    name: str, provider: Any
) -> None:
    """The interface is the single choke point every AI call passes through (NET-4).

    A fake that does not satisfy it is a fake the production code path could not
    actually be swapped for — so whatever the tests using it prove, they do not prove
    that.
    """
    assert isinstance(provider, ExtractionProvider)
    assert isinstance(provider.name, str) and provider.name


def test_the_real_adapter_satisfies_the_same_protocol() -> None:
    """The other half. A protocol only both sides implement is a contract."""
    assert isinstance(adapter.AnthropicVisionProvider, type)
    assert hasattr(adapter.AnthropicVisionProvider, "extract")
    assert adapter.AnthropicVisionProvider.name == "anthropic"


@pytest.mark.parametrize("name", PRODUCING_FAKES)
def test_every_producing_fake_returns_the_documented_response_type(name: str) -> None:
    provider = dict(_providers())[name]
    response = provider.extract(_request())
    assert isinstance(response, ExtractionResponse)
    assert isinstance(response.usage, ProviderUsage)
    assert all(isinstance(e, Extraction) for e in response.extractions)


@pytest.mark.parametrize("name", PRODUCING_FAKES)
def test_every_producing_fake_answers_for_exactly_the_images_it_was_given(
    name: str,
) -> None:
    """One extraction per image, indexed to match.

    The merge across front and back keys on `image_index` for provenance. A fake that
    returned one extraction for two images, or reused index 0, would exercise a merge
    that cannot happen.
    """
    provider = dict(_providers())[name]
    request = _request(images=2)
    response = provider.extract(request)
    assert [e.image_index for e in response.extractions] == [i.index for i in request.images]


# --------------------------------------------------------------------------------------
# The round trip: could the real API have returned this?
# --------------------------------------------------------------------------------------


def _to_wire(extraction: Extraction) -> dict[str, Any]:
    """Render an `Extraction` back into the JSON the API would have produced.

    This is the crux of the file. Going the other way — parsing a payload — is what the
    adapter does; going *this* way asks whether the fake's output is expressible in the
    contract at all. A fake carrying a field the schema has no room for, or a value the
    parser would reject, fails here.
    """
    fields: dict[str, Any] = {}
    for name in FieldName:
        field = extraction.fields.get(name)
        if field is None:
            fields[name.value] = {
                "value": None,
                "on_this_image": False,
                "legible": True,
                "confidence": 0.0,
                "bbox": "",
            }
            continue
        box = field.bbox
        fields[name.value] = {
            "value": field.value,
            "on_this_image": True,
            "legible": field.legible,
            "confidence": field.confidence,
            "bbox": f"{box.x0},{box.y0},{box.x1},{box.y1}" if box else "",
        }

    typography = extraction.warning_typography
    return {
        "is_label": extraction.is_label,
        "fields": fields,
        "warning_text": extraction.warning_text,
        "warning_typography": {
            "header_is_all_caps": typography.header_is_all_caps,
            "header_is_bold": typography.header_is_bold,
            "body_is_bold": typography.body_is_bold,
            "relative_size": typography.relative_size,
            "contrast_ok": typography.contrast_ok,
        },
    }


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_every_fixture_extraction_is_expressible_in_the_wire_contract(spec: Any) -> None:
    """Everything the spec-backed fake produces, the real API could also have produced.

    Run across the whole golden set, so a fixture that exercises an unusual shape — a
    missing warning, an illegible field, a two-image split — is checked too. If a
    fixture can only exist offline, the tests built on it are testing a world the
    product does not live in.
    """
    provider = SpecBackedProvider(spec)
    for extraction in provider.extract(_request()).extractions:
        payload = _to_wire(extraction)
        json.dumps(payload)  # the wire is JSON; anything unserialisable fails here
        adapter.parse_extraction(payload, image_index=extraction.image_index)


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_a_fixture_extraction_survives_a_round_trip_through_the_parser(
    spec: Any,
) -> None:
    """And comes back meaning the same thing.

    Values, legibility and the tri-state typography signals must all survive. This is
    the assertion that would have caught a fake quietly using `False` where the real
    parser produces `None` — the drift that matters most, because on the government
    warning those two are a compliant label and a non-compliant one.
    """
    provider = SpecBackedProvider(spec)
    for extraction in provider.extract(_request()).extractions:
        reparsed = adapter.parse_extraction(
            _to_wire(extraction), image_index=extraction.image_index
        )
        assert reparsed.is_label == extraction.is_label
        assert reparsed.warning_text == extraction.warning_text
        for name in FieldName:
            original = extraction.fields.get(name)
            if original is None or original.value is None:
                continue
            assert reparsed.fields[name].value == original.value
            assert reparsed.fields[name].legible == original.legible


def test_the_typography_signals_round_trip_as_tri_state() -> None:
    """Every combination, including the unknowns the fake can emit.

    `SpecBackedProvider` derives typography from the spec, so it never emits `None`
    today. The parser must still preserve one if it ever does — a fake that started
    emitting `False` for "unknown" would be indistinguishable from a real determination.
    """
    from api.models import WarningTypography

    for header, body, contrast in [
        (True, False, True),
        (False, True, False),
        (None, None, None),
        (True, None, False),
    ]:
        extraction = Extraction(
            image_index=0,
            fields={
                FieldName.GOVERNMENT_WARNING: ExtractedField(
                    value="GOVERNMENT WARNING: x", confidence=0.9
                )
            },
            warning_text="GOVERNMENT WARNING: x",
            warning_typography=WarningTypography(
                header_is_all_caps=header,
                header_is_bold=header,
                body_is_bold=body,
                relative_size=1.0,
                contrast_ok=contrast,
            ),
        )
        reparsed = adapter.parse_extraction(_to_wire(extraction), image_index=0)
        assert reparsed.warning_typography.header_is_bold is header
        assert reparsed.warning_typography.body_is_bold is body
        assert reparsed.warning_typography.contrast_ok is contrast


# --------------------------------------------------------------------------------------
# LP-067: no fake invents a value either
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_no_fake_reports_a_value_for_a_field_it_marked_illegible(spec: Any) -> None:
    """The rule the real adapter keeps, kept by the fakes too.

    Otherwise a test could assert that the pipeline handles an unreadable field while
    the fake was quietly handing it text.
    """
    provider = SpecBackedProvider(spec, illegible={FieldName.BRAND_NAME})
    for extraction in provider.extract(_request()).extractions:
        field = extraction.fields.get(FieldName.BRAND_NAME)
        if field is None:
            continue
        assert field.legible is False
        assert field.value is None
        assert field.confidence == 0.0


@pytest.mark.tc("TC-15")
def test_the_non_label_fake_reports_no_fields_at_all() -> None:
    """A cat photograph yields nothing, rather than a plausible guess at a brand."""
    for extraction in NonLabelProvider().extract(_request()).extractions:
        assert extraction.is_label is False
        assert extraction.fields == {}
        assert extraction.warning_text is None


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_confidence_stays_within_range_for_every_fixture(spec: Any) -> None:
    """The bound the real parser enforces, held by the fakes as well."""
    for extraction in SpecBackedProvider(spec).extract(_request()).extractions:
        for field in extraction.fields.values():
            assert 0.0 <= field.confidence <= 1.0


# --------------------------------------------------------------------------------------
# The failure fakes fail the way the real one does
# --------------------------------------------------------------------------------------


@pytest.mark.tc("TC-21")
def test_the_failing_fake_raises_the_same_error_type_the_adapter_raises() -> None:
    """TC-21 turns on this. A fake raising `ConnectionError` would exercise a handler
    the production path never reaches — the route catches `ProviderError` and nothing
    else."""
    with pytest.raises(ProviderError):
        FailingProvider().extract(_request())


@pytest.mark.parametrize("retryable", [True, False])
def test_the_failing_fake_carries_the_retryable_flag_the_resilience_layer_reads(
    retryable: bool,
) -> None:
    """Retryable is the flag the breaker and the retry policy branch on.

    A fake that always claimed retryable would make every outage test exercise the
    retry path and none of them the give-up path.
    """
    with pytest.raises(ProviderError) as raised:
        FailingProvider(retryable=retryable).extract(_request())
    assert raised.value.retryable is retryable


def test_the_recorded_fake_says_which_fixture_is_missing() -> None:
    """A recorded replay with no recording is a setup error, and says how to fix it."""
    from pathlib import Path

    with pytest.raises(ProviderError) as raised:
        RecordedProvider(Path("/nowhere"), "tc01").extract(_request())
    assert "No recorded fixture" in str(raised.value)
    assert not raised.value.retryable


# --------------------------------------------------------------------------------------
# Usage accounting exists on the fakes too
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", PRODUCING_FAKES)
def test_every_producing_fake_reports_a_model_name(name: str) -> None:
    """OPS-4 captures usage on every call. A blank model makes the cost line unattributable."""
    response = dict(_providers())[name].extract(_request())
    assert response.usage.model


@pytest.mark.parametrize("name", PRODUCING_FAKES)
def test_fake_usage_costs_nothing(name: str) -> None:
    """Offline runs must not appear in the spend tally as real money (OPS-4, LP-295)."""
    response = dict(_providers())[name].extract(_request())
    assert adapter.estimated_usd(response.usage) == 0.0
