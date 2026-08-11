"""CONTRACT: anything the API can legally return must parse, and nothing unusable may pass.

The schema is a request, not a guarantee. Structured output makes the shape very likely
to be right, and `parse_extraction` validates anyway — this file is what makes that
validation trustworthy rather than decorative.

Two directions, and both matter:

* **Everything conformant parses.** Payloads are generated *from the schema we send*, so
  any response the API is entitled to produce is one the parser survives. A parser that
  only handles the payloads a human imagined is a 500 waiting for an unusual label.
* **Nothing unusable passes.** A confidence of 3.0, a numeric brand name, a missing
  field, a `stop_reason` of `refusal` — each has to become a `ProviderError` rather than
  a plausible-looking `Extraction`. This is the direction that produces false passes.

The generator is derived from `EXTRACTION_SCHEMA` rather than hand-written. Hand-written
generators drift from the schema exactly the way hand-written fakes drift from the thing
they double, and drift here would be invisible.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from api.models import Extraction, FieldName, WarningTypography
from api.provider import anthropic_adapter as adapter
from api.provider.base import ProviderError

pytestmark = pytest.mark.contract

SETTINGS = settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


# --------------------------------------------------------------------------------------
# A generator derived from the schema we actually send
# --------------------------------------------------------------------------------------


def _from_schema(node: dict[str, Any], *, path: str = "") -> st.SearchStrategy[Any]:
    """Build a hypothesis strategy for any value this schema permits.

    Deliberately covers only the JSON Schema subset structured output supports, which is
    the same subset `tests/regression/test_extraction_schema_overflow.py` pins. If the
    schema grows a construct this does not understand, the `KeyError` is the point: the
    contract layer should not silently stop covering part of the payload.

    **`confidence` is generated inside 0..1.** With a generic float strategy, a random
    payload almost never lands in range for all seven fields at once — instrumented over
    300 examples, 13 parsed and 287 were refused. The tests below accept "parses OR is
    refused", so at that rate they were very nearly asserting nothing about parsing.
    Structured output cannot express numeric bounds, so the *schema* cannot say
    `0 <= confidence <= 1` — but the model is instructed to, and a contract test for the
    happy path has to actually reach it. Out-of-range confidence is still covered, by a
    targeted test further down that asserts it is refused.
    """
    if "anyOf" in node:
        return st.one_of(*[_from_schema(o, path=path) for o in node["anyOf"]])

    match node["type"]:
        case "null":
            return st.none()
        case "boolean":
            return st.booleans()
        case "number":
            if path.endswith("confidence"):
                return st.floats(min_value=0.0, max_value=1.0, width=32)
            return st.floats(allow_nan=False, allow_infinity=False, width=32)
        case "integer":
            return st.integers(min_value=-1000, max_value=1000)
        case "string":
            return st.text(max_size=40)
        case "object":
            return st.fixed_dictionaries(
                {
                    key: _from_schema(value, path=f"{path}.{key}")
                    for key, value in node["properties"].items()
                }
            )
        case unknown:  # pragma: no cover - a schema construct the generator lacks
            raise KeyError(f"no strategy for schema type {unknown!r}")


CONFORMANT_PAYLOADS = _from_schema(adapter.EXTRACTION_SCHEMA)


# --------------------------------------------------------------------------------------
# Direction one: everything the schema permits parses
# --------------------------------------------------------------------------------------


@SETTINGS
@given(CONFORMANT_PAYLOADS)
def test_every_schema_conformant_response_parses(payload: dict[str, Any]) -> None:
    """Anything the API is entitled to return is something the parser handles.

    The headline claim of this file, and it used to be written as "parses **or** is
    refused". That tolerance existed because the generator produced unbounded
    confidences, so a payload almost never landed in range for all seven fields at once
    — instrumented, 13 of 300 examples parsed and 287 were refused. Under that
    tolerance, zero successful parses would still have been green.

    With `confidence` generated in range the tolerance is unnecessary: 400 of 400
    conformant payloads parse. So the assertion is now unconditional, which is what the
    docstring claimed all along. Genuinely unusable payloads are covered by the
    targeted refusal tests below, where the expected outcome is stated rather than
    tolerated.
    """
    assert isinstance(adapter.parse_extraction(payload, image_index=0), Extraction)


@SETTINGS
@given(CONFORMANT_PAYLOADS)
def test_a_parsed_extraction_always_validates_against_the_domain_model(
    payload: dict[str, Any],
) -> None:
    """The parser's output is the rules engine's input, so it has to be valid there.

    Every field within 0..1 confidence, every bbox inside the frame. The rules engine
    trusts this and does not re-validate.
    """
    extraction = adapter.parse_extraction(payload, image_index=0)
    Extraction.model_validate(extraction.model_dump())


@SETTINGS
@given(CONFORMANT_PAYLOADS)
def test_parsing_is_deterministic(payload: dict[str, Any]) -> None:
    """The same response always produces the same extraction (ENG-3, LP-246).

    Determinism is the difference between a suite that gates a deploy and one that gets
    retried until it goes green.
    """
    assert adapter.parse_extraction(payload, image_index=0) == adapter.parse_extraction(
        payload, image_index=0
    )


# --------------------------------------------------------------------------------------
# Direction two: nothing unusable becomes a value
# --------------------------------------------------------------------------------------


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "is_label": True,
        "fields": {
            name.value: {
                "value": "OLD TOM",
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
    base.update(overrides)
    return base


@SETTINGS
@given(st.floats(allow_nan=False, allow_infinity=False).filter(lambda v: not 0.0 <= v <= 1.0))
def test_a_confidence_outside_zero_to_one_is_refused(confidence: float) -> None:
    """The schema cannot express numeric bounds, so the parser has to.

    Structured output does not support `minimum`/`maximum`, which is exactly why
    validating on receipt is not belt-and-braces here — it is the only check there is.
    """
    payload = _payload()
    payload["fields"][FieldName.BRAND_NAME.value]["confidence"] = confidence
    with pytest.raises(ProviderError, match="0 to 1"):
        adapter.parse_extraction(payload, image_index=0)


@pytest.mark.parametrize("value", [42, 3.5, True, ["a"], {"b": 1}])
def test_a_non_text_field_value_is_refused(value: Any) -> None:
    """A number where text belongs is not a brand name, however confidently returned."""
    payload = _payload()
    payload["fields"][FieldName.BRAND_NAME.value]["value"] = value
    with pytest.raises(ProviderError):
        adapter.parse_extraction(payload, image_index=0)


@pytest.mark.parametrize("field", list(FieldName), ids=lambda f: f.value)
def test_a_field_the_model_omitted_entirely_is_refused(field: FieldName) -> None:
    """Silence is the ambiguity the schema's `required` list exists to remove.

    A model that leaves a field out has not told us it could not read it. Defaulting
    would turn "no answer" into "not on the label", which is a verdict.
    """
    payload = _payload()
    del payload["fields"][field.value]
    with pytest.raises(ProviderError, match=field.value):
        adapter.parse_extraction(payload, image_index=0)


@pytest.mark.parametrize("payload", [None, [], "text", 42, True])
def test_a_response_of_the_wrong_shape_is_refused(payload: Any) -> None:
    with pytest.raises(ProviderError):
        adapter.parse_extraction(payload, image_index=0)


def test_a_response_with_no_field_readings_is_refused() -> None:
    with pytest.raises(ProviderError, match="field readings"):
        adapter.parse_extraction({"is_label": True}, image_index=0)


# --------------------------------------------------------------------------------------
# LP-067: never a guess, and never a coerced unknown
# --------------------------------------------------------------------------------------


def test_a_field_that_is_not_on_this_image_is_omitted_rather_than_reported_empty() -> None:
    """"Not on this image" and "unreadable" are different, and must stay different.

    Omitting keeps the front/back merge unambiguous: the other image can supply the
    field without a null from this one competing with it.
    """
    payload = _payload()
    payload["fields"][FieldName.PRODUCER.value].update(
        {"value": None, "on_this_image": False, "legible": True}
    )
    extraction = adapter.parse_extraction(payload, image_index=0)
    assert FieldName.PRODUCER not in extraction.fields


def test_a_field_that_is_present_but_unreadable_is_reported_as_unreadable() -> None:
    payload = _payload()
    payload["fields"][FieldName.PRODUCER.value].update(
        {"value": None, "on_this_image": True, "legible": False}
    )
    field = adapter.parse_extraction(payload, image_index=0).fields[FieldName.PRODUCER]
    assert field.value is None
    assert field.legible is False


@SETTINGS
@given(st.floats(min_value=0.0, max_value=1.0))
def test_a_field_with_no_value_carries_no_confidence(claimed: float) -> None:
    """No value read means no confidence in a value, whatever the model claimed.

    A high confidence next to a null value would let a downstream tie-break prefer the
    reading that read nothing.
    """
    payload = _payload()
    payload["fields"][FieldName.PRODUCER.value].update(
        {"value": None, "on_this_image": True, "legible": False, "confidence": claimed}
    )
    field = adapter.parse_extraction(payload, image_index=0).fields[FieldName.PRODUCER]
    assert field.confidence == 0.0


@pytest.mark.parametrize("junk", ["maybe", 1, 0, "true", "false", [], {}, 1.0])
@pytest.mark.parametrize(
    "signal", ["header_is_all_caps", "header_is_bold", "body_is_bold", "contrast_ok"]
)
def test_an_unusable_typography_signal_becomes_unknown_rather_than_false(
    signal: str, junk: Any
) -> None:
    """WARN-6, and the single most important coercion in the codebase.

    `False` reads downstream as "we checked, and the label does not comply" — a
    determination we did not make. `None` routes to Needs review. Guessing in that
    direction on the government warning is how a non-compliant label passes.
    """
    payload = _payload()
    payload["warning_typography"][signal] = junk
    typography = adapter.parse_extraction(payload, image_index=0).warning_typography
    assert getattr(typography, signal) is None


def test_typography_read_off_an_unreadable_statement_is_discarded() -> None:
    """Typography from a statement nobody could read is not a determination.

    Keeping it would let "the heading is not bold" be reported about text the model
    never resolved — a finding manufactured from noise.
    """
    payload = _payload()
    payload["fields"][FieldName.GOVERNMENT_WARNING.value].update(
        {"value": None, "on_this_image": True, "legible": False}
    )
    extraction = adapter.parse_extraction(payload, image_index=0)
    assert extraction.warning_typography == WarningTypography()


def test_typography_is_discarded_when_there_is_no_warning_text() -> None:
    payload = _payload(warning_text=None)
    extraction = adapter.parse_extraction(payload, image_index=0)
    assert extraction.warning_typography == WarningTypography()


# --------------------------------------------------------------------------------------
# Evidence boxes: dropped, never fatal, never wrong
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "", "   ", "0.1,0.2", "0.1,0.2,0.3,0.4,0.5",
        "a,b,c,d", "1.2,0,2.0,1", "0.9,0.9,0.1,0.1", "not a box",
    ],
)
def test_a_malformed_evidence_box_is_dropped_rather_than_failing_the_extraction(
    raw: str,
) -> None:
    """No verdict depends on a box, so a bad one must not cost a correct reading.

    Throwing away a correctly-read brand name because the model returned `x1=1.02`
    would trade something load-bearing for something decorative.
    """
    payload = _payload()
    payload["fields"][FieldName.BRAND_NAME.value]["bbox"] = raw
    field = adapter.parse_extraction(payload, image_index=0).fields[FieldName.BRAND_NAME]
    assert field.bbox is None
    assert field.value == "OLD TOM"


def test_a_well_formed_evidence_box_survives() -> None:
    """Dropping bad boxes must not mean dropping all of them."""
    payload = _payload()
    payload["fields"][FieldName.BRAND_NAME.value]["bbox"] = "0.12,0.30,0.88,0.41"
    box = adapter.parse_extraction(payload, image_index=0).fields[FieldName.BRAND_NAME].bbox
    assert box is not None
    assert (box.x0, box.y0, box.x1, box.y1) == (0.12, 0.30, 0.88, 0.41)


def test_a_box_in_its_natural_object_shape_is_still_accepted() -> None:
    """Recorded fixtures and offline providers hand over boxes as objects.

    The flat string is the wire format the live model uses; accepting both is what lets
    a recorded fixture from before the schema change still replay.
    """
    payload = _payload()
    payload["fields"][FieldName.BRAND_NAME.value]["bbox"] = {
        "x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.3
    }
    box = adapter.parse_extraction(payload, image_index=0).fields[FieldName.BRAND_NAME].bbox
    assert box is not None


# --------------------------------------------------------------------------------------
# Message-level outcomes
# --------------------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Message:
    def __init__(self, stop_reason: str, text: str = "{}") -> None:
        self.stop_reason = stop_reason
        self.content = [_Block(text)]
        self.usage = None


def test_a_refusal_is_reported_as_unverified_and_not_retried() -> None:
    """A refusal will not succeed on a retry, and the agent must be told nothing was checked."""
    with pytest.raises(ProviderError) as raised:
        adapter._parse_message(_Message("refusal"), image_index=0)
    assert not raised.value.retryable
    assert "Nothing has" in str(raised.value)


def test_a_truncated_answer_is_retryable_rather_than_parsed() -> None:
    """`max_tokens` means the JSON stopped mid-object. Parsing it would invent a label."""
    with pytest.raises(ProviderError) as raised:
        adapter._parse_message(_Message("max_tokens"), image_index=0)
    assert raised.value.retryable


def test_unreadable_json_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(ProviderError, match="could not be read"):
        adapter._parse_message(_Message("end_turn", "{not json"), image_index=0)


def test_a_message_with_no_text_block_is_refused() -> None:
    class _Empty:
        stop_reason = "end_turn"
        content: ClassVar[list[Any]] = []

    with pytest.raises(ProviderError, match="no readable answer"):
        adapter._parse_message(_Empty(), image_index=0)


# --------------------------------------------------------------------------------------
# Error translation: retryable means "this might work in a moment"
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(adapter._RETRYABLE_STATUSES))
def test_transient_statuses_are_marked_retryable(status: int) -> None:
    import anthropic

    error = anthropic.APIStatusError("busy", response=_http_response(status), body=None)
    assert adapter._translate(error).retryable


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_statuses_are_not_retried(status: int) -> None:
    """A 400 will not succeed on a retry; retrying it three times spends the budget slower.

    This is the class the incident lived in. Had the schema 400 been retried as
    transient, the symptom would have been latency rather than an error, and it would
    have taken even longer to find.
    """
    import anthropic

    error = anthropic.APIStatusError("bad", response=_http_response(status), body=None)
    assert not adapter._translate(error).retryable


def _http_response(status: int) -> httpx.Response:
    """A real `httpx.Response`, because the SDK's error types demand one.

    Faking it with a stand-in object would type-check only under an ignore comment and
    would stop resembling the thing the adapter actually receives.
    """
    return httpx.Response(status, request=httpx.Request("POST", "https://api.invalid/v1"))


def test_every_translated_error_speaks_to_an_agent_rather_than_an_engineer() -> None:
    """UX-6: no stack traces, no "inference", no exception class names on screen."""
    import anthropic

    errors = [
        adapter._translate(anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.invalid/v1"))),
        adapter._translate(anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.invalid/v1"))),
        adapter._translate(RuntimeError("boom")),
    ]
    for error in errors:
        message = str(error)
        assert "label reading service" in message
        assert "Traceback" not in message
        assert "inference" not in message.lower()


# --------------------------------------------------------------------------------------
# Usage accounting (OPS-4)
# --------------------------------------------------------------------------------------


def test_token_counts_are_read_from_the_documented_usage_field_names() -> None:
    """`cache_read_input_tokens` is the API's spelling; ours is `cache_read_tokens`.

    A rename on either side silently zeroes the cache-hit metric — and a zero there is
    indistinguishable from "the cache is not working", which is a real thing that
    happens and which the metric exists to detect.
    """

    class _Usage:
        input_tokens = 11
        output_tokens = 22
        cache_read_input_tokens = 33

    class _WithUsage:
        usage = _Usage()

    usage = adapter._usage_from(_WithUsage(), "claude-opus-5")
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (11, 22, 33)


def test_a_response_with_no_usage_block_costs_zero_rather_than_crashing() -> None:
    """A cost line is worth showing and never worth failing a verification over."""

    class _NoUsage:
        pass

    usage = adapter._usage_from(_NoUsage(), "claude-opus-5")
    assert usage.input_tokens == 0
    assert adapter.estimated_usd(usage) == 0.0


def test_the_cost_estimate_prices_cached_reads_below_fresh_input() -> None:
    """The whole point of the cache breakpoint, expressed as arithmetic.

    If these two ever priced the same, the caching work would be invisible in the cost
    report and nobody would notice it breaking.
    """
    from api.provider.base import ProviderUsage

    fresh = adapter.estimated_usd(ProviderUsage(input_tokens=100_000))
    cached = adapter.estimated_usd(ProviderUsage(cache_read_tokens=100_000))
    assert 0 < cached < fresh
