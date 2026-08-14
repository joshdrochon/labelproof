"""CONTRACT: the request we build must satisfy the documented limits of the API we send it to.

This is the layer whose absence caused the incident. The extraction schema exceeded two
Messages API ceilings at once, every live call returned HTTP 400 before the model saw an
image, and 624 offline tests passed — because the offline providers return already-parsed
`Extraction` objects and never build a request.

So these tests build the **real request**, with the real adapter, and inspect it. No
network: the SDK client is injectable, and a recording double captures the keyword
arguments the adapter would have sent. What is asserted is not "the adapter did not
crash" — it is that every field of the payload is something the documented API accepts.

The rule for adding to this file: assert things that are true of the **API**, not of our
code. "The model is `claude-opus-5`" is our choice and belongs elsewhere. "`temperature`
is not sent, because it is rejected on this model family" is the API's rule and belongs
here.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from api.config import Config
from api.models import Commodity, FieldName
from api.provider import anthropic_adapter as adapter
from api.provider.base import ExtractionRequest, ImageInput

pytestmark = pytest.mark.contract


# --------------------------------------------------------------------------------------
# A double that records instead of sending
# --------------------------------------------------------------------------------------


class _RecordedCall:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.options: dict[str, Any] = {}


class _Messages:
    def __init__(self, recorder: _RecordedCall, response: Any) -> None:
        self._recorder = recorder
        self._response = response

    def create(self, **kwargs: Any) -> Any:
        self._recorder.kwargs = kwargs
        # ANSWER THE SCHEMA YOU WERE ASKED FOR, which is what the real API does. A stub
        # that returns one fixed shape regardless made every split-mode call fail to
        # parse — and it failed in the tests rather than in production only because the
        # tests are the only place a canned response exists.
        schema = (kwargs.get("output_config") or {}).get("format", {}).get("schema")
        if schema is not None:
            return _Response(_payload_for(schema))
        return self._response


class _RecordingClient:
    """Stands in for `anthropic.Anthropic`, capturing the call rather than making it.

    Mirrors the two surfaces the adapter uses — `with_options(...)` returning a client
    and `.messages.create(**kwargs)` — and nothing else. A wider double would let the
    adapter start using a method the real SDK does not have without anything noticing.
    """

    def __init__(self, response: Any) -> None:
        self.recorder = _RecordedCall()
        self._response = response

    def with_options(self, **options: Any) -> _RecordingClient:
        self.recorder.options = options
        return self

    @property
    def messages(self) -> _Messages:
        return _Messages(self.recorder, self._response)


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    input_tokens = 1200
    output_tokens = 300
    cache_read_input_tokens = 900


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.content = [_Block(json.dumps(payload))]
        self.usage = _Usage()
        self.stop_reason = "end_turn"


def _payload_for(schema: dict[str, Any]) -> dict[str, Any]:
    """A response satisfying whichever half of the extraction this schema asks for.

    The adapter has three modes and they request three different shapes. Building the
    answer from the schema keeps this double honest for all of them without the test
    having to know which mode is under test.
    """
    props = schema.get("properties", {})
    payload: dict[str, Any] = {}
    if "is_label" in props:
        payload["is_label"] = True
    if "fields" in props:
        payload["fields"] = {
            name: _field_value() for name in props["fields"].get("properties", {})
        }
    if "government_warning" in props:
        payload["government_warning"] = _field_value()
    if "warning_text" in props:
        payload["warning_text"] = "GOVERNMENT WARNING: ..."
    if "warning_typography" in props:
        payload["warning_typography"] = {
            "header_is_all_caps": True,
            "header_is_bold": True,
            "body_is_bold": False,
            "relative_size": 1.0,
            "contrast_ok": True,
        }
    return payload


def _field_value() -> dict[str, Any]:
    return {
        "value": "OLD TOM DISTILLERY",
        "on_this_image": True,
        "legible": True,
        "confidence": 0.9,
        "bbox": "0.1,0.2,0.9,0.3",
    }


def _valid_payload() -> dict[str, Any]:
    """A response that satisfies `EXTRACTION_SCHEMA`, so parsing does not mask the test."""
    from api.models import FieldName

    return {
        "is_label": True,
        "fields": {
            name.value: {
                "value": "OLD TOM DISTILLERY",
                "on_this_image": True,
                "legible": True,
                "confidence": 0.9,
                "bbox": "0.1,0.2,0.9,0.3",
            }
            for name in FieldName
        },
        "warning_text": "GOVERNMENT WARNING: ...",
        "warning_typography": {
            "header_is_all_caps": True,
            "header_is_bold": True,
            "body_is_bold": False,
            "relative_size": 1.0,
            "contrast_ok": True,
        },
    }


@pytest.fixture
def captured() -> _RecordedCall:
    """Run one real extraction against the recording client and return what it sent."""
    client = _RecordingClient(_Response(_valid_payload()))
    # `single`, explicitly. These tests measure the COMBINED schema against the two API
    # ceilings, and in any split mode the last recorded call is a half — which carries
    # no `fields` block and would make the ceiling assertions pass by being small.
    provider = adapter.AnthropicVisionProvider(
        Config(
            anthropic_api_key="",
            extraction_model="claude-opus-5",
            effort="low",
            extraction_mode="single",
        ),
        client=client,
    )
    provider.extract(
        ExtractionRequest(
            commodity=Commodity.SPIRITS,
            images=[ImageInput(index=0, data=b"\x89PNG\r\n\x1a\n fake", role="front")],
        )
    )
    return client.recorder


# --------------------------------------------------------------------------------------
# The request is well formed at all
# --------------------------------------------------------------------------------------


def test_a_request_is_actually_built(captured: _RecordedCall) -> None:
    """The premise. Every other test in this file is vacuous if nothing was sent."""
    assert captured.kwargs, "the adapter never called messages.create"


def test_the_request_carries_only_parameters_the_api_accepts(
    captured: _RecordedCall,
) -> None:
    """An unknown top-level parameter is a 400, and a typo is invisible offline.

    Pinned as an allowlist rather than a denylist: a denylist only catches the mistakes
    somebody already made.
    """
    permitted = {
        "model", "max_tokens", "thinking", "output_config", "system", "messages",
        "tools", "tool_choice", "stop_sequences", "metadata", "stream",
        # Data residency (NET-4 / SEC-8). A top-level Messages API parameter — it is
        # *not* nested inside `output_config`, and putting it there would be accepted
        # by the SDK and ignored by the API.
        "inference_geo",
    }
    assert set(captured.kwargs) <= permitted, sorted(set(captured.kwargs) - permitted)


@pytest.mark.parametrize("parameter", ["temperature", "top_p", "top_k"])
def test_no_sampling_parameter_is_sent(
    captured: _RecordedCall, parameter: str
) -> None:
    """Sampling parameters are removed on this model family and return a 400.

    Exactly the shape of the incident: a parameter that is fine on an older model,
    invisible to every offline test, and a hard failure on every live call.
    """
    assert parameter not in captured.kwargs


def test_thinking_is_adaptive_rather_than_a_token_budget(
    captured: _RecordedCall,
) -> None:
    """`budget_tokens` is removed on this model family and returns a 400."""
    assert captured.kwargs["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(captured.kwargs["thinking"])


def test_the_effort_level_is_one_the_api_accepts(captured: _RecordedCall) -> None:
    """Effort lives inside `output_config`, not at the top level, and has a fixed set."""
    effort = captured.kwargs["output_config"]["effort"]
    assert effort in adapter.VALID_EFFORTS
    assert "effort" not in captured.kwargs


def test_max_tokens_is_present_and_positive(captured: _RecordedCall) -> None:
    """Required on every request. Absent is a 400; too small truncates the JSON."""
    assert captured.kwargs["max_tokens"] == adapter.MAX_TOKENS
    assert adapter.MAX_TOKENS > 0


def test_the_per_call_timeout_is_passed_through_with_options(
    captured: _RecordedCall,
) -> None:
    """The deadline is ours, and the SDK must not retry behind it.

    `max_retries=0` matters: the SDK's own backoff is invisible to our deadline and
    would spend the request budget without telling anyone.
    """
    assert captured.options["max_retries"] == 0
    assert captured.options["timeout"] > 0


# --------------------------------------------------------------------------------------
# Structured output
# --------------------------------------------------------------------------------------


def test_the_structured_output_format_is_the_documented_shape(
    captured: _RecordedCall,
) -> None:
    """`output_config.format`, not the deprecated top-level `output_format`."""
    fmt = captured.kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert "schema" in fmt
    assert "output_format" not in captured.kwargs


def test_the_schema_that_is_sent_is_the_schema_that_is_tested(
    captured: _RecordedCall,
) -> None:
    """Closes the gap between the constant and the wire.

    `tests/regression/test_extraction_schema_overflow.py` checks the compiled schemas
    against the ceilings. That is only worth anything if the request actually carries one
    of them — a second, hand-built schema inside the adapter would sail past every one of
    those checks.

    `is`, not `==`, and against the SET rather than one constant: there are three
    extraction modes now and they send three different shapes. Identity is what proves
    the outgoing object was the one measured, rather than an equal-looking copy built
    somewhere the ceiling tests never see.
    """
    sent = captured.kwargs["output_config"]["format"]["schema"]
    compiled = (
        adapter.EXTRACTION_SCHEMA,
        adapter.FIELDS_SCHEMA,
        adapter.WARNING_SCHEMA,
        adapter.FAST_FIELDS_SCHEMA,
        adapter.CAREFUL_SCHEMA,
    )
    assert any(sent is one for one in compiled), (
        "the request carried a schema that is not one of the compiled constants, so "
        "nothing has checked it against the union-count or grammar-size ceilings"
    )


# --------------------------------------------------------------------------------------
# The three documented schema ceilings, measured on the outgoing payload
# --------------------------------------------------------------------------------------
#
# Shared with tests/regression/test_extraction_schema_overflow.py, which asserts the same
# three against the module constant. Both matter: the regression file pins the shape that
# shipped, this file pins what actually leaves the process.

#: A conservative stand-in for the API's compiled-grammar ceiling, in serialized bytes.
#: The real limit is on the compiled grammar and is not published as a byte count, so
#: this is set well below the size that failed and comfortably above the size that works.
GRAMMAR_SIZE_BUDGET_BYTES = 4096

#: Nested-object levels. The shipped schema was four deep (root -> fields -> field ->
#: bbox); flattening the box to a string made it three.
MAX_OBJECT_NESTING = 3


def _count_unions(node: Any) -> int:
    """Every parameter whose type is a union — what the 16-parameter ceiling counts."""
    if isinstance(node, dict):
        here = 1 if "anyOf" in node or isinstance(node.get("type"), list) else 0
        return here + sum(_count_unions(child) for child in node.values())
    if isinstance(node, list):
        return sum(_count_unions(child) for child in node)
    return 0


def _object_nesting(node: Any) -> int:
    """How many `type: object` levels deep the schema goes."""
    if isinstance(node, dict):
        here = 1 if node.get("type") == "object" else 0
        return here + max((_object_nesting(c) for c in node.values()), default=0)
    if isinstance(node, list):
        return max((_object_nesting(c) for c in node), default=0)
    return 0



def _wire_schema(captured: _RecordedCall) -> Any:
    return captured.kwargs["output_config"]["format"]["schema"]


def test_the_schema_on_the_wire_is_within_the_union_ceiling(
    captured: _RecordedCall,
) -> None:
    """The incident's first ceiling, asserted on the outgoing payload itself."""
    assert _count_unions(_wire_schema(captured)) <= adapter.MAX_UNION_PARAMETERS


def test_the_schema_on_the_wire_is_within_the_grammar_size_budget(
    captured: _RecordedCall,
) -> None:
    """The incident's *second* ceiling, on the payload rather than on the constant.

    All three ceilings were being checked against `EXTRACTION_SCHEMA` in
    tests/regression/, bridged to the wire by a single `is` assertion. That is one
    identity holding three claims up: replace `_bbox_schema` with a nested object of
    plain (non-nullable) numbers and the union count stays legal while the grammar
    overruns — the regression file caught it and the *wire* contract did not, because
    the wire contract only counted unions.

    Grammar size and nesting now travel with the request too, so each ceiling is
    asserted where it is actually enforced.
    """
    size = len(json.dumps(_wire_schema(captured), separators=(",", ":")))
    assert size <= GRAMMAR_SIZE_BUDGET_BYTES, f"{size} bytes on the wire"


def test_the_schema_on_the_wire_stays_shallow(captured: _RecordedCall) -> None:
    """Nesting is what the compiled grammar grows fastest with."""
    assert _object_nesting(_wire_schema(captured)) <= MAX_OBJECT_NESTING


def test_the_wire_checks_would_catch_the_shipped_schema(captured: _RecordedCall) -> None:
    """Teeth, and specifically for the ceiling the union count cannot see.

    A nested four-number bbox with no unions at all keeps the union count legal. If the
    size and nesting assertions above were satisfied by it, they would be decoration.
    """
    schema = json.loads(json.dumps(_wire_schema(captured)))
    plain_nested_bbox = {
        "type": "object",
        "properties": {axis: {"type": "number"} for axis in ("x0", "y0", "x1", "y1")},
        "required": ["x0", "y0", "x1", "y1"],
        "additionalProperties": False,
    }
    for name in FieldName:
        schema["properties"]["fields"]["properties"][name.value]["properties"]["bbox"] = (
            json.loads(json.dumps(plain_nested_bbox))
        )

    assert _count_unions(schema) <= adapter.MAX_UNION_PARAMETERS, (
        "the union count alone should NOT catch this shape — that is the point"
    )
    assert len(json.dumps(schema, separators=(",", ":"))) > GRAMMAR_SIZE_BUDGET_BYTES
    assert _object_nesting(schema) > MAX_OBJECT_NESTING


def test_the_whole_request_is_json_serialisable(captured: _RecordedCall) -> None:
    """Anything the SDK cannot serialise is a failure that only happens live.

    A `set`, a `Path`, an enum member — all of them look fine in a Python assertion and
    none of them survive the wire.
    """
    payload = dict(captured.kwargs)
    json.dumps(payload)


# --------------------------------------------------------------------------------------
# Prompt caching (pinned build decision) — a contract with the pricing, not just with the API
# --------------------------------------------------------------------------------------


def test_the_system_prompt_carries_exactly_one_cache_breakpoint(
    captured: _RecordedCall,
) -> None:
    """At most four per request, and one is all this prompt needs."""
    blocks = captured.kwargs["system"]
    breakpoints = [b for b in blocks if "cache_control" in b]
    assert len(breakpoints) == 1
    assert len(breakpoints) <= 4


def test_the_cache_breakpoint_sits_on_the_last_system_block(
    captured: _RecordedCall,
) -> None:
    """Caching is a prefix match, so the breakpoint must be the end of the stable part.

    Placed anywhere earlier and the commodity rule sets are re-read at full price on
    every call.
    """
    blocks = captured.kwargs["system"]
    assert "cache_control" in blocks[-1]
    assert all("cache_control" not in b for b in blocks[:-1])


def test_the_images_land_after_the_cached_prefix(captured: _RecordedCall) -> None:
    """An image in the system prompt would invalidate the cache on every request.

    Render order is tools -> system -> messages, so anything per-request has to be in
    `messages`. The only symptom of getting this wrong is a cost line nobody watches.
    """
    # Block types, not the word — the prompt talks about images at length.
    assert all(block["type"] == "text" for block in captured.kwargs["system"])
    content = captured.kwargs["messages"][0]["content"]
    assert any(block["type"] == "image" for block in content)


def test_the_system_prompt_is_byte_identical_across_calls() -> None:
    """No timestamp, no request id, no commodity — nothing per-request in the prefix.

    Built twice and compared, because "we did not interpolate anything" is a claim that
    a single f-string quietly breaks. `usage.cache_read_tokens` going to zero is the
    only production symptom, and it is on a dashboard rather than in a test.
    """
    assert json.dumps(adapter.build_system_blocks()) == json.dumps(
        adapter.build_system_blocks()
    )


@pytest.mark.parametrize("commodity", list(Commodity))
def test_the_commodity_never_reaches_the_cached_prefix(commodity: Commodity) -> None:
    """All three rule sets are in the static prompt; the active one travels per request.

    If the prompt named the active commodity, the cache would key per commodity and a
    mixed batch would miss on every switch.
    """
    client = _RecordingClient(_Response(_valid_payload()))
    provider = adapter.AnthropicVisionProvider(
        Config(anthropic_api_key=""), client=client
    )
    provider.extract(
        ExtractionRequest(
            commodity=commodity,
            images=[ImageInput(index=0, data=b"png", role="single")],
        )
    )
    system = json.dumps(client.recorder.kwargs["system"])
    for name in Commodity:
        assert name.value.upper() in system, "all three rule sets must be in the prefix"
    assert commodity.value.upper() in client.recorder.kwargs["messages"][0]["content"][1]["text"]


def test_the_system_prompt_contains_no_volatile_looking_content() -> None:
    """A cheap smoke test for the class of mistake that kills prompt caching."""
    text = json.dumps(adapter.build_system_blocks())
    for marker in ("datetime", "uuid", "request_id", "20260", 'Z"', "timestamp"):
        assert marker not in text, f"{marker!r} in the cached prefix"


# --------------------------------------------------------------------------------------
# The image block
# --------------------------------------------------------------------------------------


def test_the_image_is_sent_as_base64_with_a_supported_media_type(
    captured: _RecordedCall,
) -> None:
    """The documented source shape. A raw `bytes` here is a serialisation error, live only."""
    image = next(
        block
        for block in captured.kwargs["messages"][0]["content"]
        if block["type"] == "image"
    )
    source = image["source"]
    assert source["type"] == "base64"
    assert source["media_type"] in adapter.SUPPORTED_MEDIA_TYPES
    assert isinstance(source["data"], str)
    base64.standard_b64decode(source["data"])


def test_the_base64_payload_has_no_newlines(captured: _RecordedCall) -> None:
    """Wrapped base64 is rejected. `standard_b64encode` does not wrap; `encodebytes` does."""
    image = next(
        block
        for block in captured.kwargs["messages"][0]["content"]
        if block["type"] == "image"
    )
    assert "\n" not in image["source"]["data"]


def test_the_conversation_starts_with_a_user_turn(captured: _RecordedCall) -> None:
    """First message must be `user`, and there is no assistant prefill.

    Prefills return a 400 on this model family — another parameter that is fine on an
    older model and invisible offline.
    """
    messages = captured.kwargs["messages"]
    assert messages[0]["role"] == "user"
    assert all(m["role"] != "assistant" for m in messages)


def test_an_unsupported_media_type_is_refused_before_the_call_is_made() -> None:
    """A clear message rather than a raw 400 from the far end.

    The preprocessor emits PNG or WebP; this guard exists so that a pipeline change
    which starts emitting TIFF fails with a sentence instead of an HTTP status.
    """
    from api.provider.base import ProviderError

    client = _RecordingClient(_Response(_valid_payload()))
    provider = adapter.AnthropicVisionProvider(
        Config(anthropic_api_key=""), client=client
    )
    with pytest.raises(ProviderError) as raised:
        provider.extract(
            ExtractionRequest(
                commodity=Commodity.SPIRITS,
                images=[ImageInput(index=0, data=b"II*\x00", media_type="image/tiff")],
            )
        )
    assert not raised.value.retryable
    assert "PNG" in str(raised.value)
    assert client.recorder.kwargs == {}, "no call should have been attempted"


# --------------------------------------------------------------------------------------
# Configuration that would only fail live
# --------------------------------------------------------------------------------------


def test_an_invalid_effort_fails_at_construction_rather_than_on_the_first_click() -> None:
    """PERF-6: a typo in the environment is a startup error, not a 400 for the grader."""
    from api.config import ConfigError

    with pytest.raises(ConfigError, match="LABELPROOF_EFFORT"):
        adapter.AnthropicVisionProvider(
            Config(anthropic_api_key="", effort="very-high"),
            client=_RecordingClient(_Response(_valid_payload())),
        )


def test_a_missing_api_key_fails_at_construction_with_an_actionable_message() -> None:
    from api.config import ConfigError

    with pytest.raises(ConfigError) as raised:
        adapter.AnthropicVisionProvider(Config(anthropic_api_key=""))
    assert "ANTHROPIC_API_KEY" in str(raised.value)
    assert "LABELPROOF_FAKE_PROVIDER" in str(raised.value)


def test_every_valid_effort_level_builds_a_request() -> None:
    """The set the adapter accepts is the set it can actually send."""
    for effort in sorted(adapter.VALID_EFFORTS):
        client = _RecordingClient(_Response(_valid_payload()))
        provider = adapter.AnthropicVisionProvider(
            Config(anthropic_api_key="", effort=effort), client=client
        )
        provider.extract(
            ExtractionRequest(
                commodity=Commodity.SPIRITS,
                images=[ImageInput(index=0, data=b"png")],
            )
        )
        assert client.recorder.kwargs["output_config"]["effort"] == effort


@pytest.mark.parametrize(
    ("mode", "calls_per_image"),
    [("single", 1), ("split", 2), ("hybrid", 2)],
)
def test_one_request_is_built_per_image(mode: str, calls_per_image: int) -> None:
    """LP-280: images go concurrently, so wall clock is max() not sum().

    Batching several images into one message would change the token profile and the
    failure mode — one bad image would take the whole label with it.

    The count per image is now the mode's business: `split` and `hybrid` each send two
    concurrent halves. What must hold in every mode is that the count scales with the
    number of IMAGES and nothing batches them together.
    """
    calls: list[dict[str, Any]] = []

    class _Counting(_RecordingClient):
        @property
        def messages(self) -> Any:
            outer = self

            class _M:
                def create(self, **kwargs: Any) -> Any:
                    calls.append(kwargs)
                    schema = (kwargs.get("output_config") or {}).get("format", {}).get(
                        "schema"
                    )
                    if schema is not None:
                        return _Response(_payload_for(schema))
                    return outer._response

            return _M()

    client = _Counting(_Response(_valid_payload()))
    provider = adapter.AnthropicVisionProvider(
        Config(anthropic_api_key="", extraction_mode=mode), client=client
    )
    provider.extract(
        ExtractionRequest(
            commodity=Commodity.SPIRITS,
            images=[
                ImageInput(index=0, data=b"front", role="front"),
                ImageInput(index=1, data=b"back", role="back"),
            ],
        )
    )
    assert len(calls) == 2 * calls_per_image
    for kwargs in calls:
        images = [b for b in kwargs["messages"][0]["content"] if b["type"] == "image"]
        assert len(images) == 1


def test_no_call_is_made_when_there_are_no_images() -> None:
    """An empty request must not become a request for zero images."""
    client = _RecordingClient(_Response(_valid_payload()))
    provider = adapter.AnthropicVisionProvider(
        Config(anthropic_api_key=""), client=client
    )
    response = provider.extract(
        ExtractionRequest(commodity=Commodity.SPIRITS, images=[])
    )
    assert response.extractions == []
    assert client.recorder.kwargs == {}
