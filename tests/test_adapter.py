"""The real vision adapter, exercised entirely offline (LP-050 through LP-053, LP-062).

**Every test here mocks the SDK client. Nothing in this file may ever reach the network**
— CI runs with no egress and the suite has to mean the same thing on a laptop and on a
build box (ENG-3). The adapter takes its client by injection for exactly this reason.

What is worth testing about an adapter is not "does it call the API" — it is the
translation on both sides: that the request we build is the one the prompt cache and the
schema need, and that the answer we accept can never become a value nobody read.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from api.config import Config, ConfigError
from api.models import Commodity, FieldName
from api.provider.anthropic_adapter import (
    EXTRACTION_SCHEMA,
    MAX_TOKENS,
    MAX_UNION_PARAMETERS,
    SYSTEM_BLOCKS,
    AnthropicVisionProvider,
    build_system_blocks,
    describe_residency,
    estimated_usd,
    parse_extraction,
    price_for,
)
from api.provider.base import (
    ExtractionProvider,
    ExtractionRequest,
    ImageInput,
    ProviderError,
    ProviderUsage,
)
from api.provider.resilience import CircuitBreaker, RetryPolicy

# --------------------------------------------------------------------------------------
# A stand-in for the SDK. Same surface, no socket.
# --------------------------------------------------------------------------------------


@dataclass
class StubBlock:
    text: str
    type: str = "text"


@dataclass
class StubUsage:
    input_tokens: int = 1200
    output_tokens: int = 300
    cache_read_input_tokens: int = 4000


@dataclass
class StubMessage:
    content: list[StubBlock]
    usage: StubUsage = field(default_factory=StubUsage)
    stop_reason: str = "end_turn"


class FakeMessages:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        with self._client.lock:
            self._client.calls.append(kwargs)
            index = len(self._client.calls)
        return self._client.responder(kwargs, index)


class FakeClient:
    """Records every request and returns whatever the test's responder decides."""

    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.calls: list[dict[str, Any]] = []
        self.options: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.messages = FakeMessages(self)

    def with_options(self, **kwargs: Any) -> FakeClient:
        with self.lock:
            self.options.append(kwargs)
        return self


def responds_with(payload: dict[str, Any] | str, **message_kwargs: Any) -> Any:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return lambda _kwargs, _index: StubMessage(content=[StubBlock(text=text)], **message_kwargs)


# --------------------------------------------------------------------------------------
# Payload helpers
# --------------------------------------------------------------------------------------

WARNING_TEXT = (
    "GOVERNMENT WARNING: (1) According to the Surgeon General, women should not drink "
    "alcoholic beverages during pregnancy because of the risk of birth defects. "
    "(2) Consumption of alcoholic beverages impairs your ability to drive a car or "
    "operate machinery, and may cause health problems."
)


def a_field(
    value: str | None = None,
    *,
    on_this_image: bool = True,
    legible: bool = True,
    confidence: float = 0.9,
    bbox: Any = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "on_this_image": on_this_image,
        "legible": legible,
        "confidence": confidence,
        "bbox": bbox,
    }


def a_label(
    *,
    fields: dict[str, dict[str, Any]] | None = None,
    warning_text: str | None = WARNING_TEXT,
    typography: dict[str, Any] | None = None,
    is_label: bool = True,
) -> dict[str, Any]:
    base = {
        "brand_name": a_field("OLD TOM DISTILLERY"),
        "class_type": a_field("Kentucky Straight Bourbon Whiskey"),
        "alcohol_content": a_field("45% ALC/VOL (90 PROOF)"),
        "net_contents": a_field("750 mL"),
        "producer": a_field("Old Tom Distillery, Bardstown, Kentucky"),
        "country_of_origin": a_field(None, on_this_image=False, confidence=0.0),
        "government_warning": a_field(WARNING_TEXT),
    }
    base.update(fields or {})
    return {
        "is_label": is_label,
        "fields": base,
        "warning_text": warning_text,
        "warning_typography": typography
        if typography is not None
        else {
            "header_is_all_caps": True,
            "header_is_bold": True,
            "body_is_bold": False,
            "relative_size": 0.9,
            "contrast_ok": True,
        },
    }


def a_config(**overrides: Any) -> Config:
    return Config(anthropic_api_key="test-key-not-used", **overrides)


def a_provider(responder: Any, **kwargs: Any) -> tuple[AnthropicVisionProvider, FakeClient]:
    client = FakeClient(responder)
    config = kwargs.pop("config", None) or a_config()
    provider = AnthropicVisionProvider(
        config,
        client=client,
        sleep=lambda _s: None,
        rand=lambda: 0.5,
        **kwargs,
    )
    return provider, client


def an_image(index: int = 0, role: str | None = "single") -> ImageInput:
    return ImageInput(index=index, data=b"\x89PNG fake bytes", media_type="image/png", role=role)


def a_request(*images: ImageInput, commodity: Commodity = Commodity.SPIRITS) -> ExtractionRequest:
    return ExtractionRequest(commodity=commodity, images=list(images or (an_image(),)))


# --------------------------------------------------------------------------------------
# The interface
# --------------------------------------------------------------------------------------


def test_the_adapter_satisfies_the_provider_protocol() -> None:
    provider, _ = a_provider(responds_with(a_label()))
    assert isinstance(provider, ExtractionProvider)
    assert provider.name == "anthropic"


def test_construction_without_a_key_fails_loudly() -> None:
    """A half-configured provider discovered on the grader's first click is PERF-6."""
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicVisionProvider(Config(anthropic_api_key=""))


def test_a_nonsense_effort_is_caught_at_startup_not_at_call_time() -> None:
    with pytest.raises(ConfigError, match="LABELPROOF_EFFORT"):
        AnthropicVisionProvider(a_config(effort="maximum"), client=FakeClient(None))


def test_no_images_makes_no_calls() -> None:
    provider, client = a_provider(responds_with(a_label()))
    response = provider.extract(ExtractionRequest(commodity=Commodity.WINE, images=[]))
    assert client.calls == []
    assert response.extractions == []


# --------------------------------------------------------------------------------------
# One call per image, images concurrent (LP-280)
# --------------------------------------------------------------------------------------


def test_one_call_per_image() -> None:
    provider, client = a_provider(responds_with(a_label()))
    response = provider.extract(a_request(an_image(0, "front"), an_image(1, "back")))

    assert len(client.calls) == 2
    assert [e.image_index for e in response.extractions] == [0, 1]


def test_images_run_concurrently_so_wall_clock_is_max_not_sum() -> None:
    """LP-280. Two images must not cost twice one image against the 5s gate."""
    per_call = 0.15

    def slow(_kwargs: dict[str, Any], _index: int) -> StubMessage:
        time.sleep(per_call)
        return StubMessage(content=[StubBlock(text=json.dumps(a_label()))])

    provider, _ = a_provider(slow)
    started = time.monotonic()
    provider.extract(a_request(an_image(0, "front"), an_image(1, "back")))
    elapsed = time.monotonic() - started

    assert elapsed < per_call * 1.8, "the two calls were serialised"


def test_extractions_come_back_in_image_order_however_the_calls_finish() -> None:
    """Threads finish out of order; the pipeline indexes by position."""

    def uneven(kwargs: dict[str, Any], _index: int) -> StubMessage:
        text = kwargs["messages"][0]["content"][1]["text"]
        if "front" in text:
            time.sleep(0.05)
        return StubMessage(content=[StubBlock(text=json.dumps(a_label()))])

    provider, _ = a_provider(uneven)
    response = provider.extract(a_request(an_image(0, "front"), an_image(1, "back")))
    assert [e.image_index for e in response.extractions] == [0, 1]


def test_one_failed_image_fails_the_whole_extraction() -> None:
    """Half a label read as a whole label reports the missing half as Missing."""

    def half_broken(_kwargs: dict[str, Any], index: int) -> StubMessage:
        if index == 2:
            raise ConnectionError("socket closed")
        return StubMessage(content=[StubBlock(text=json.dumps(a_label()))])

    provider, _ = a_provider(half_broken, policy=RetryPolicy(max_attempts=1))
    with pytest.raises(ProviderError):
        provider.extract(a_request(an_image(0, "front"), an_image(1, "back")))


# --------------------------------------------------------------------------------------
# Prompt caching (pinned build decision) — the two rules that are easy to get wrong
# --------------------------------------------------------------------------------------


def test_the_system_prompt_is_byte_identical_across_commodities() -> None:
    """Interpolating the commodity into the system prompt fragments the cache silently."""
    provider, client = a_provider(responds_with(a_label()))
    for commodity in Commodity:
        provider.extract(a_request(commodity=commodity))

    rendered = {json.dumps(call["system"], sort_keys=True) for call in client.calls}
    assert len(rendered) == 1


def test_the_system_prompt_is_byte_identical_across_requests() -> None:
    assert json.dumps(build_system_blocks()) == json.dumps(build_system_blocks())
    assert json.dumps(build_system_blocks()) == json.dumps(SYSTEM_BLOCKS)


def test_the_system_prompt_carries_all_three_commodity_rule_sets() -> None:
    text = " ".join(block["text"] for block in SYSTEM_BLOCKS)
    for commodity in Commodity:
        assert commodity.value.upper() in text


def test_the_active_commodity_travels_in_the_user_message() -> None:
    provider, client = a_provider(responds_with(a_label()))
    provider.extract(a_request(commodity=Commodity.MALT))

    user_text = client.calls[0]["messages"][0]["content"][1]["text"]
    assert "malt" in user_text.lower()


def test_cache_control_sits_on_the_last_system_block_only() -> None:
    """So the images land after the cached prefix and never invalidate it."""
    marked = [i for i, block in enumerate(SYSTEM_BLOCKS) if "cache_control" in block]
    assert marked == [len(SYSTEM_BLOCKS) - 1]
    assert SYSTEM_BLOCKS[-1]["cache_control"] == {"type": "ephemeral"}


def test_images_are_sent_after_the_system_prompt() -> None:
    provider, client = a_provider(responds_with(a_label()))
    provider.extract(a_request())

    content = client.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["type"] == "base64"
    assert "cache_control" not in json.dumps(content)


# --------------------------------------------------------------------------------------
# Call parameters
# --------------------------------------------------------------------------------------


def test_the_pinned_model_and_effort_are_what_we_send() -> None:
    provider, client = a_provider(responds_with(a_label()), config=a_config(effort="low"))
    provider.extract(a_request())

    call = client.calls[0]
    assert call["model"] == provider.config.extraction_model
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["effort"] == "low"
    assert call["max_tokens"] == MAX_TOKENS


def test_the_call_carries_our_timeout_and_no_sdk_retries() -> None:
    """The SDK's own backoff is invisible to the deadline and would outspend it."""
    provider, client = a_provider(responds_with(a_label()), config=a_config())
    provider.extract(a_request())

    options = client.options[0]
    assert options["max_retries"] == 0
    # Bounded by the configured timeout, which now follows the model's measured latency
    # (LP-330) rather than a fixed 4s that no model could actually finish inside.
    assert 0 < options["timeout"] <= provider.config.provider_timeout_ms / 1000


def test_an_unsupported_image_format_never_reaches_the_provider() -> None:
    provider, client = a_provider(responds_with(a_label()))
    heic = ImageInput(index=0, data=b"...", media_type="image/heic")

    with pytest.raises(ProviderError) as exc:
        provider.extract(ExtractionRequest(commodity=Commodity.WINE, images=[heic]))
    assert exc.value.retryable is False
    assert client.calls == []


# --------------------------------------------------------------------------------------
# Structured output schema (LP-051)
# --------------------------------------------------------------------------------------


def test_the_schema_is_sent_as_the_output_format() -> None:
    provider, client = a_provider(responds_with(a_label()))
    provider.extract(a_request())

    fmt = client.calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] == EXTRACTION_SCHEMA


def test_every_call_pins_inference_to_the_united_states() -> None:
    """NET-1 — data residency has to be a request parameter, not a README paragraph.

    Without `inference_geo` the request follows the workspace default, which is `global`.
    The customer is a US federal agency and the README claims label images never leave the
    country; this is the line that makes the claim true. Verified against the live API.
    """
    provider, client = a_provider(responds_with(a_label()))
    provider.extract(a_request())

    assert client.calls[0]["inference_geo"] == "us"


def test_haiku_is_not_sent_an_inference_geo_it_would_reject() -> None:
    """Haiku 4.5 answers `inference_geo` with a 400, so pinning is a model capability.

    Found by measurement, not by reading docs: every call died with
    "'claude-haiku-4-5-20251001' does not support inference_geo."
    """
    provider, client = a_provider(
        responds_with(a_label()), config=Config(extraction_model="claude-haiku-4-5")
    )
    provider.extract(a_request())

    assert "inference_geo" not in client.calls[0]


def test_a_model_that_cannot_be_pinned_says_so_rather_than_going_quiet() -> None:
    """The compliance answer must reach a human, not be dropped by the request builder.

    Silently omitting the parameter is exactly how a federal customer ends up told the
    product guarantees residency it does not.
    """
    assert "pinned to 'us'" in describe_residency("claude-opus-5", "us")

    unpinnable = describe_residency("claude-haiku-4-5", "us")
    assert "CANNOT be guaranteed" in unpinnable
    assert "claude-haiku-4-5" in unpinnable


def test_a_thinking_capable_model_gets_thinking_and_effort() -> None:
    provider, client = a_provider(responds_with(a_label()))
    provider.extract(a_request())

    call = client.calls[0]
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["effort"] == provider.config.effort


def test_haiku_is_sent_neither_thinking_nor_effort() -> None:
    """Both are 400s on Haiku 4.5, not ignored parameters — and it is the main path.

    Haiku is the only model that fits the 5-second adoption gate (LP-330), so "the model
    that rejects these two parameters" is the one the product runs on. Sending them was a
    400 on all twenty calls of the LP-331 spike.
    """
    provider, client = a_provider(
        responds_with(a_label()), config=Config(extraction_model="claude-haiku-4-5")
    )
    provider.extract(a_request())

    call = client.calls[0]
    assert "thinking" not in call
    assert "effort" not in call["output_config"]
    # The schema still has to go, or nothing constrains the answer.
    assert call["output_config"]["format"]["schema"] == EXTRACTION_SCHEMA


def test_the_schema_covers_all_seven_fields() -> None:
    properties = EXTRACTION_SCHEMA["properties"]["fields"]["properties"]
    assert set(properties) == {name.value for name in FieldName}
    assert set(EXTRACTION_SCHEMA["properties"]["fields"]["required"]) == set(properties)


def test_every_field_carries_confidence_a_box_and_a_legibility_flag() -> None:
    for name in FieldName:
        schema = EXTRACTION_SCHEMA["properties"]["fields"]["properties"][name.value]
        assert set(schema["required"]) == {
            "value",
            "on_this_image",
            "legible",
            "confidence",
            "bbox",
        }
        assert schema["additionalProperties"] is False


def test_the_schema_asks_for_every_typography_signal() -> None:
    typography = EXTRACTION_SCHEMA["properties"]["warning_typography"]
    assert set(typography["required"]) == {
        "header_is_all_caps",
        "header_is_bold",
        "body_is_bold",
        "relative_size",
        "contrast_ok",
    }


def test_the_tri_state_signals_admit_null_in_the_schema() -> None:
    """If the schema only allowed booleans, "could not tell" would be unsayable."""
    typography = EXTRACTION_SCHEMA["properties"]["warning_typography"]["properties"]
    for key in ("header_is_all_caps", "header_is_bold", "body_is_bold", "contrast_ok"):
        assert typography[key] == {"anyOf": [{"type": "boolean"}, {"type": "null"}]}


def _count_unions(node: Any) -> int:
    """Union-typed parameters anywhere in the schema, however deeply nested."""
    if not isinstance(node, dict):
        return 0
    here = 1 if ("anyOf" in node or "oneOf" in node or isinstance(node.get("type"), list)) else 0
    for key in ("properties", "$defs", "definitions"):
        for child in (node.get(key) or {}).values():
            here += _count_unions(child)
    for key in ("items", "additionalProperties"):
        here += _count_unions(node.get(key))
    for branch in node.get("anyOf") or node.get("oneOf") or []:
        here += _count_unions(branch)
    return here


def test_the_schema_stays_under_the_api_union_limit() -> None:
    """Structured output rejects a schema with more than 16 union-typed parameters.

    Not a style rule — it is a 400 on *every* live call, and the offline suite cannot
    see it, because the fake providers never build a request. The natural shape of this
    schema has 20 (seven values, seven boxes, warning_text, relative_size, four
    tri-states); the LP-330 spike hit the wall on its first real call. Anything that adds
    a nullable field has to give one back.
    """
    assert _count_unions(EXTRACTION_SCHEMA) <= MAX_UNION_PARAMETERS


def test_the_evidence_box_is_flat_and_never_a_nested_object() -> None:
    """Seven nested boxes overflow the grammar-size cap — a 400 on every live call.

    Independent of the union limit above: the spike's schema failed this way even with
    every union stripped out. Nesting is the cost, so the shape has to stay flat.
    """
    for name in FieldName:
        field_schema = EXTRACTION_SCHEMA["properties"]["fields"]["properties"][name.value]
        bbox = field_schema["properties"]["bbox"]
        assert bbox == {"type": "string"}


@pytest.mark.parametrize(
    "raw",
    ["0.1,0.2,0.9,0.4", " 0.1 , 0.2 , 0.9 , 0.4 "],
    ids=["plain", "padded"],
)
def test_a_flat_box_string_is_parsed_into_an_evidence_box(raw: str) -> None:
    labelled = a_label(fields={"brand_name": a_field("OLD TOM", bbox=raw)})
    provider, _ = a_provider(responds_with(labelled))
    box = provider.extract(a_request()).extractions[0].fields[FieldName.BRAND_NAME].bbox

    assert box is not None
    assert (box.x0, box.y0, box.x1, box.y1) == (0.1, 0.2, 0.9, 0.4)


def test_an_empty_box_string_reads_as_no_box_and_is_not_logged_as_a_defect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """"" is the expected answer for a field the model did not read, not a bad box."""
    labelled = a_label(fields={"brand_name": a_field("OLD TOM", bbox="")})
    provider, _ = a_provider(responds_with(labelled))
    with caplog.at_level(logging.WARNING):
        extraction = provider.extract(a_request()).extractions[0]

    assert extraction.fields[FieldName.BRAND_NAME].value == "OLD TOM"
    assert extraction.fields[FieldName.BRAND_NAME].bbox is None
    assert "provider_bbox_dropped" not in caplog.text


@pytest.mark.parametrize(
    "raw",
    ["0.1,0.2,0.9", "0.1,0.2,0.9,0.4,0.5", "left,top,right,bottom", "0.1;0.2;0.9;0.4"],
    ids=["too-few", "too-many", "not-numbers", "wrong-separator"],
)
def test_a_malformed_box_string_is_dropped_and_the_reading_survives(raw: str) -> None:
    """A decorative field must never cost a correctly-read value (pinned build decision)."""
    labelled = a_label(fields={"brand_name": a_field("OLD TOM", bbox=raw)})
    provider, _ = a_provider(responds_with(labelled))
    brand = provider.extract(a_request()).extractions[0].fields[FieldName.BRAND_NAME]

    assert brand.value == "OLD TOM"
    assert brand.bbox is None


# --------------------------------------------------------------------------------------
# Parsing — the fabrication guard (LP-067)
# --------------------------------------------------------------------------------------


def test_a_read_field_comes_back_intact() -> None:
    provider, _ = a_provider(responds_with(a_label()))
    extraction = provider.extract(a_request()).extractions[0]

    brand = extraction.fields[FieldName.BRAND_NAME]
    assert brand.value == "OLD TOM DISTILLERY"
    assert brand.legible is True
    assert brand.confidence == pytest.approx(0.9)


def test_an_unreadable_field_is_null_and_illegible_never_a_guess() -> None:
    payload = a_label(fields={"brand_name": a_field(None, legible=False, confidence=0.0)})
    provider, _ = a_provider(responds_with(payload))
    brand = provider.extract(a_request()).extractions[0].fields[FieldName.BRAND_NAME]

    assert brand.value is None
    assert brand.legible is False


def test_a_field_not_on_this_image_is_omitted_rather_than_reported_empty() -> None:
    """"Not here" and "could not read it" are different verdicts (Missing vs Unreadable)."""
    provider, _ = a_provider(responds_with(a_label()))
    fields = provider.extract(a_request()).extractions[0].fields

    assert FieldName.COUNTRY_OF_ORIGIN not in fields
    assert FieldName.BRAND_NAME in fields


def test_confidence_in_a_value_we_do_not_have_is_zero() -> None:
    """A confident null is a contradiction; letting it through skews the merge."""
    payload = a_label(fields={"brand_name": a_field(None, legible=False, confidence=0.97)})
    provider, _ = a_provider(responds_with(payload))
    brand = provider.extract(a_request()).extractions[0].fields[FieldName.BRAND_NAME]
    assert brand.confidence == 0.0


def test_a_blank_string_is_treated_as_nothing_read() -> None:
    payload = a_label(fields={"brand_name": a_field("   ", legible=False)})
    provider, _ = a_provider(responds_with(payload))
    assert provider.extract(a_request()).extractions[0].fields[FieldName.BRAND_NAME].value is None


def test_transcription_is_not_normalised_on_the_way_in() -> None:
    """Smoothing "750ML" into "750 mL" here would erase the difference being looked for."""
    payload = a_label(fields={"net_contents": a_field("750ML")})
    provider, _ = a_provider(responds_with(payload))
    fields = provider.extract(a_request()).extractions[0].fields
    assert fields[FieldName.NET_CONTENTS].value == "750ML"


def test_a_non_label_image_reports_no_fields() -> None:
    """TC-15 — somebody uploads a cat."""
    empty = {name.value: a_field(None, on_this_image=False, confidence=0.0) for name in FieldName}
    payload = a_label(fields=empty, warning_text=None, is_label=False)
    provider, _ = a_provider(responds_with(payload))

    extraction = provider.extract(a_request()).extractions[0]
    assert extraction.is_label is False
    assert extraction.fields == {}


# --------------------------------------------------------------------------------------
# Typography signals (LP-053) — tri-state, and it stays tri-state
# --------------------------------------------------------------------------------------


def test_typography_signals_come_through() -> None:
    provider, _ = a_provider(responds_with(a_label()))
    typography = provider.extract(a_request()).extractions[0].warning_typography

    assert typography.header_is_all_caps is True
    assert typography.header_is_bold is True
    assert typography.body_is_bold is False
    assert typography.relative_size == pytest.approx(0.9)
    assert typography.contrast_ok is True


def test_a_null_signal_stays_null_and_never_becomes_false() -> None:
    """None routes to Needs review; False asserts a violation we did not observe."""
    payload = a_label(
        typography={
            "header_is_all_caps": None,
            "header_is_bold": None,
            "body_is_bold": None,
            "relative_size": None,
            "contrast_ok": None,
        }
    )
    provider, _ = a_provider(responds_with(payload))
    typography = provider.extract(a_request()).extractions[0].warning_typography

    assert typography.header_is_bold is None
    assert typography.body_is_bold is None
    assert typography.header_is_all_caps is None
    assert typography.contrast_ok is None
    assert typography.relative_size is None


@pytest.mark.parametrize("junk", ["unknown", "", 0, 1, [], {}, "false"])
def test_an_unusable_signal_becomes_unknown_not_false(junk: Any) -> None:
    """Anything that is not a bool is "could not determine". Never a coercion."""
    payload = a_label(typography={"header_is_bold": junk, "body_is_bold": junk,
                                  "header_is_all_caps": junk, "relative_size": None,
                                  "contrast_ok": junk})
    provider, _ = a_provider(responds_with(payload))
    typography = provider.extract(a_request()).extractions[0].warning_typography

    assert typography.header_is_bold is None
    assert typography.body_is_bold is None
    assert typography.contrast_ok is None


def test_a_false_signal_survives_as_false() -> None:
    """TC-03/TC-04 depend on this: a real observation must not be softened to unknown."""
    payload = a_label(
        typography={
            "header_is_all_caps": False,
            "header_is_bold": False,
            "body_is_bold": True,
            "relative_size": 0.4,
            "contrast_ok": False,
        }
    )
    provider, _ = a_provider(responds_with(payload))
    typography = provider.extract(a_request()).extractions[0].warning_typography

    assert typography.header_is_all_caps is False
    assert typography.header_is_bold is False
    assert typography.body_is_bold is True
    assert typography.contrast_ok is False


def test_typography_read_off_an_unreadable_warning_is_discarded() -> None:
    """Signals about a statement nobody could read are not observations."""
    payload = a_label(
        fields={"government_warning": a_field(None, legible=False, confidence=0.0)},
        warning_text=None,
    )
    provider, _ = a_provider(responds_with(payload))
    typography = provider.extract(a_request()).extractions[0].warning_typography

    assert typography.header_is_bold is None
    assert typography.body_is_bold is None
    assert typography.contrast_ok is None


def test_the_warning_statement_is_transcribed_verbatim() -> None:
    """Title case is TC-03. Casefolding it here would erase the violation."""
    title_case = WARNING_TEXT.replace("GOVERNMENT WARNING:", "Government Warning:")
    payload = a_label(
        fields={"government_warning": a_field(title_case)},
        warning_text=title_case,
        typography={
            "header_is_all_caps": False,
            "header_is_bold": True,
            "body_is_bold": False,
            "relative_size": 1.0,
            "contrast_ok": True,
        },
    )
    provider, _ = a_provider(responds_with(payload))
    extraction = provider.extract(a_request()).extractions[0]

    assert extraction.warning_text == title_case
    assert extraction.warning_typography.header_is_all_caps is False


# --------------------------------------------------------------------------------------
# Validation on receipt (LP-051)
# --------------------------------------------------------------------------------------


def test_junk_instead_of_json_is_a_provider_error_not_a_crash() -> None:
    provider, _ = a_provider(responds_with("I'm sorry, I can't help with that."),
                             policy=RetryPolicy(max_attempts=1))
    with pytest.raises(ProviderError):
        provider.extract(a_request())


def test_a_missing_field_key_is_rejected() -> None:
    payload = a_label()
    del payload["fields"]["net_contents"]
    provider, _ = a_provider(responds_with(payload), policy=RetryPolicy(max_attempts=1))

    with pytest.raises(ProviderError, match="net_contents"):
        provider.extract(a_request())


@pytest.mark.parametrize("confidence", [1.5, -0.1, "high", None])
def test_an_impossible_confidence_is_rejected(confidence: Any) -> None:
    payload = a_label(fields={"brand_name": a_field("OLD TOM", confidence=confidence)})
    provider, _ = a_provider(responds_with(payload), policy=RetryPolicy(max_attempts=1))

    with pytest.raises(ProviderError):
        provider.extract(a_request())


def test_a_non_text_value_is_rejected() -> None:
    payload = a_label(fields={"brand_name": a_field(42)})  # type: ignore[arg-type]
    provider, _ = a_provider(responds_with(payload), policy=RetryPolicy(max_attempts=1))

    with pytest.raises(ProviderError):
        provider.extract(a_request())


def test_a_valid_box_is_kept() -> None:
    payload = a_label(
        fields={"brand_name": a_field("OLD TOM", bbox={"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2})}
    )
    provider, _ = a_provider(responds_with(payload))
    box = provider.extract(a_request()).extractions[0].fields[FieldName.BRAND_NAME].bbox

    assert box is not None
    assert box.x1 == pytest.approx(0.9)


@pytest.mark.parametrize(
    "bbox",
    [
        {"x0": 0.1, "y0": 0.1, "x1": 1.4, "y1": 0.2},  # off the image
        {"x0": 0.9, "y0": 0.1, "x1": 0.2, "y1": 0.2},  # inverted
        {"x0": 0.1, "y0": 0.1},  # incomplete
        "somewhere near the top",
    ],
)
def test_a_bad_box_is_dropped_but_the_reading_survives(bbox: Any) -> None:
    """No verdict depends on an evidence box, so it never costs us a field."""
    payload = a_label(fields={"brand_name": a_field("OLD TOM", bbox=bbox)})
    provider, _ = a_provider(responds_with(payload))
    brand = provider.extract(a_request()).extractions[0].fields[FieldName.BRAND_NAME]

    assert brand.value == "OLD TOM"
    assert brand.bbox is None


def test_a_refusal_degrades_and_is_not_retried() -> None:
    provider, client = a_provider(responds_with(a_label(), stop_reason="refusal"))

    with pytest.raises(ProviderError) as exc:
        provider.extract(a_request())
    assert exc.value.retryable is False
    assert "Nothing has been checked" in str(exc.value)
    assert len(client.calls) == 1


def test_a_truncated_answer_is_retried() -> None:
    calls = {"n": 0}

    def truncated_then_fine(_kwargs: dict[str, Any], index: int) -> StubMessage:
        calls["n"] = index
        if index == 1:
            return StubMessage(
                content=[StubBlock(text='{"is_label": true, "fields"')], stop_reason="max_tokens"
            )
        return StubMessage(content=[StubBlock(text=json.dumps(a_label()))])

    provider, _ = a_provider(truncated_then_fine)
    response = provider.extract(a_request())

    assert calls["n"] == 2
    assert response.extractions[0].fields[FieldName.BRAND_NAME].value == "OLD TOM DISTILLERY"


def test_parse_extraction_rejects_a_payload_that_is_not_an_object() -> None:
    with pytest.raises(ProviderError):
        parse_extraction(["not", "an", "object"], 0)


# --------------------------------------------------------------------------------------
# Resilience wiring and cost capture
# --------------------------------------------------------------------------------------


def test_a_transient_failure_is_retried_then_succeeds() -> None:
    def flaky(_kwargs: dict[str, Any], index: int) -> StubMessage:
        if index == 1:
            raise ConnectionError("reset by peer")
        return StubMessage(content=[StubBlock(text=json.dumps(a_label()))])

    provider, client = a_provider(flaky)
    response = provider.extract(a_request())

    assert len(client.calls) == 2
    assert response.extractions[0].is_label is True


def test_an_open_breaker_short_circuits_without_calling_the_provider() -> None:
    """TC-21 — the app degrades in a sentence, not a stack trace and not a hang."""
    breaker = CircuitBreaker(failure_threshold=1)
    breaker.record_failure()
    provider, client = a_provider(responds_with(a_label()), breaker=breaker)

    with pytest.raises(ProviderError) as exc:
        provider.extract(a_request())

    assert client.calls == []
    assert exc.value.retryable is False
    assert "Try again" in str(exc.value)


def test_repeated_failures_open_the_breaker() -> None:
    def always_down(_kwargs: dict[str, Any], _index: int) -> StubMessage:
        raise ConnectionError("no route to host")

    breaker = CircuitBreaker(failure_threshold=2)
    provider, _ = a_provider(always_down, breaker=breaker, policy=RetryPolicy(max_attempts=2))

    with pytest.raises(ProviderError):
        provider.extract(a_request())
    assert breaker.state == CircuitBreaker.OPEN


def test_tokens_are_captured_on_every_call_and_summed_across_images() -> None:
    """OPS-4 — cost accounting from day one, not bolted on later."""
    provider, _ = a_provider(responds_with(a_label()))
    response = provider.extract(a_request(an_image(0, "front"), an_image(1, "back")))

    assert response.usage.input_tokens == 2400
    assert response.usage.output_tokens == 600
    assert response.usage.cache_read_tokens == 8000
    assert response.usage.model == provider.config.extraction_model
    assert response.latency_ms >= 0


def test_cost_is_derived_from_the_list_price_of_the_model_that_ran() -> None:
    """A single hardcoded price table is a 5x error waiting for a config change.

    Opus 5 and Haiku 4.5 differ by exactly that factor, and the cost line looks equally
    authoritative either way — so the model has to reach the arithmetic.
    """
    usage = ProviderUsage(input_tokens=1_000_000)
    assert estimated_usd(usage, "claude-opus-5") == pytest.approx(5.0)
    assert estimated_usd(usage, "claude-sonnet-5") == pytest.approx(3.0)
    assert estimated_usd(usage, "claude-haiku-4-5") == pytest.approx(1.0)

    cached = ProviderUsage(output_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert estimated_usd(cached, "claude-opus-5") == pytest.approx(25.5)


def test_cache_writes_are_priced_rather_than_free() -> None:
    """They cost 1.25x input and were counted at zero (OPS-4).

    Every COLD request writes the cached prefix, which is every first click a grader
    makes — so the error ran in the flattering direction on exactly the requests a
    reviewer sees, and fed a Cost Analysis deliverable.
    """
    written = ProviderUsage(cache_creation_tokens=1_000_000)
    assert estimated_usd(written, "claude-opus-5") == pytest.approx(6.25)

    # A write must never be priced as a read; that is the mistake being fixed.
    read = ProviderUsage(cache_read_tokens=1_000_000)
    assert estimated_usd(read, "claude-opus-5") == pytest.approx(0.5)


def test_an_unpriced_model_costs_the_expensive_default_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown model must over-report, never under-report.

    A surprise that shows up as a number someone questions is recoverable. One that
    shows up as a number nobody notices is not.
    """
    price, known = price_for("claude-not-yet-released")
    assert known is False
    assert price > price_for("claude-opus-5")[0]

    with caplog.at_level(logging.WARNING):
        cost = estimated_usd(ProviderUsage(input_tokens=1_000_000), "claude-not-yet-released")

    assert cost > estimated_usd(ProviderUsage(input_tokens=1_000_000), "claude-opus-5")
    assert "provider_price_unknown" in caplog.text


def test_the_adapter_records_cache_writes_from_the_sdk_response() -> None:
    """The whole cost chain downstream is plumbing until this field is populated."""

    @dataclass
    class UsageWithWrites:
        input_tokens: int = 1200
        output_tokens: int = 300
        cache_read_input_tokens: int = 0
        cache_creation_input_tokens: int = 4351

    provider, _ = a_provider(
        responds_with(a_label(), usage=UsageWithWrites())  # type: ignore[arg-type]
    )
    usage = provider.extract(a_request()).usage

    assert usage.cache_creation_tokens == 4351
    assert estimated_usd(usage, "claude-opus-5") > 0


def test_the_adapter_logs_nothing_that_could_carry_label_text(capsys: Any) -> None:
    """SEC-4 is enforced by the logger; this proves the adapter stays inside it."""
    from api import logging as lp_logging

    lp_logging.configure()
    provider, _ = a_provider(responds_with(a_label()))
    provider.extract(a_request())

    captured = capsys.readouterr().out
    assert "OLD TOM DISTILLERY" not in captured
    assert "GOVERNMENT WARNING" not in captured
    assert '"event": "provider_extract"' in captured


# --------------------------------------------------------------------------------------
# The adjudicator's request (LP-220, MATCH-4)
# --------------------------------------------------------------------------------------


class _RecordingClient:
    """Captures the request without a network. The reply is a valid judgement."""

    def __init__(self, reply: dict[str, object] | None = None) -> None:
        self.kwargs: dict[str, Any] = {}
        self._reply = reply or {
            "same_thing": True,
            "confidence": 0.93,
            "rationale": "Both name the same distillery; the label reverses the order.",
        }

    def with_options(self, **_: object) -> _RecordingClient:
        return self

    @property
    def messages(self) -> _RecordingClient:
        return self

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        block = SimpleNamespace(type="text", text=json.dumps(self._reply))
        return SimpleNamespace(content=[block], stop_reason="end_turn", usage=None)


def _judge(reply: dict[str, object] | None = None) -> tuple[Any, _RecordingClient]:
    from api.provider.anthropic_adapter import AnthropicAdjudicator

    client = _RecordingClient(reply)
    return AnthropicAdjudicator(Config(anthropic_api_key="sk-test"), client=client), client


def _request() -> Any:
    from api.models import FieldName
    from api.rules.adjudicate import AdjudicationRequest

    return AdjudicationRequest(
        field=FieldName.PRODUCER,
        expected="Old Tom Distillery",
        extracted="Distillery of Old Tom",
        commodity="spirits",
    )


def test_the_adjudicator_never_sees_an_image() -> None:
    """This tier compares two strings. Handing it the artwork would invite it to re-read
    the label — Tier 0's job, already done — and a second reading that disagreed with the
    first would be settled by whichever ran last."""
    judge, client = _judge()
    judge.judge(_request())

    serialised = json.dumps(client.kwargs["messages"])
    assert "image" not in serialised
    assert "base64" not in serialised


def test_it_does_not_say_which_value_came_from_the_label() -> None:
    """The question is symmetric. Telling the model one side is the applicant's filing
    invites deference to the filing rather than a judgement about the two values."""
    judge, client = _judge()
    judge.judge(_request())

    prompt = json.dumps(client.kwargs["messages"]).lower()
    assert "application says" not in prompt
    assert "the label reads" not in prompt


def test_it_is_not_told_a_mismatch_was_already_found() -> None:
    """"We found a problem, is it really one" is an invitation to be helpful, and helpful
    here means clearing things."""
    judge, client = _judge()
    judge.judge(_request())

    everything = json.dumps(
        [client.kwargs["messages"], client.kwargs["system"]]
    ).lower()
    assert "mismatch" not in everything


def test_the_instructions_bias_toward_refusing() -> None:
    """The asymmetry has to be in the prompt, not only in the code around it."""
    from api.provider.anthropic_adapter import ADJUDICATION_SYSTEM

    text = ADJUDICATION_SYSTEM.lower()
    assert "if you are unsure, say no" in text
    assert "approval" in text


def test_the_schema_is_closed_and_requires_a_confidence() -> None:
    """The rules engine gates on confidence; a schema that let the model omit it would
    make that gate depend on a default nobody chose."""
    from api.provider.anthropic_adapter import ADJUDICATION_SCHEMA

    assert ADJUDICATION_SCHEMA["additionalProperties"] is False
    assert set(ADJUDICATION_SCHEMA["required"]) == {
        "same_thing",
        "confidence",
        "rationale",
    }


def test_a_malformed_answer_raises_rather_than_being_guessed_at() -> None:
    """The caller leaves the Mismatch standing on a raise. Coercing a missing boolean to
    False would work today and become a silent pass the day the shape changes again."""
    judge, _ = _judge({"confidence": 0.9, "rationale": "no verdict field"})
    with pytest.raises(ProviderError):
        judge.judge(_request())


def test_confidence_is_clamped_rather_than_trusted() -> None:
    judge, _ = _judge(
        {"same_thing": True, "confidence": 4.2, "rationale": "over-confident"}
    )
    assert judge.judge(_request()).confidence == 1.0
