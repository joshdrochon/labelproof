"""The real vision adapter (LP-050 through LP-053, LP-062). Server-side only — NET-2.

One call per image, images concurrent (LP-280), so wall clock on a two-image application
is `max()` and not `sum()`. Everything the model returns is validated on receipt before
it is allowed anywhere near the rules engine (LP-051).

**The extractor never invents a value.** `ExtractedField` has no channel for a guess and
this module keeps it that way: a field the model could not read comes back
`value=None, legible=False`, and a field that simply is not on this image is omitted
rather than reported as an empty reading. That distinction is the whole of LP-067 —
"I could not read it" and "it is not here" produce different verdicts (Unreadable versus
Missing), and collapsing them would be a false finding either way.

**Typography is tri-state and stays tri-state** (LP-053). `header_is_all_caps`,
`header_is_bold`, `body_is_bold` and `contrast_ok` are `bool | None`, and `None` means
the model could not tell. Nothing in this module ever turns an unknown into `False`,
because `False` reads downstream as "we checked, and it is not bold" — a determination
we did not make. `WarningTypography` is tri-state precisely so uncertainty cannot pass a
warning check silently (WARN-6).

**Prompt caching** (pinned build decision). The system prompt is fully static: it carries all three
commodity rule sets and never mentions which one is active. The active commodity travels
in the user message. `cache_control` sits on the last system block, so the images land
after the cached prefix and never invalidate it. Watch `usage.cache_read_tokens` — if it
is 0 across repeated requests, something in the prefix has started varying.
"""

from __future__ import annotations

import base64
import json
import random
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final

import anthropic
from pydantic import ValidationError

from api import logging as lp_logging
from api.config import Config, ConfigError
from api.models import (
    BoundingBox,
    Commodity,
    ExtractedField,
    Extraction,
    FieldName,
    WarningTypography,
)
from api.pipeline import quality as quality_mod
from api.provider.base import (
    ExtractionRequest,
    ExtractionResponse,
    ImageInput,
    ProviderError,
    ProviderUsage,
)
from api.provider.resilience import (
    CircuitBreaker,
    Deadline,
    RetryPolicy,
    call_with_retries,
)
from api.rules.adjudicate import AdjudicationRequest, Judgement
from api.rules.commodity import REQUIREMENTS, Requirement
from api.rules.typography import WarningReread, WarningRereadRequest

# --------------------------------------------------------------------------------------
# Call parameters
# --------------------------------------------------------------------------------------

#: A ceiling, not a target. The JSON payload is under a thousand tokens; the headroom is
#: for adaptive thinking. A cap costs nothing until it is hit — and being cut off
#: mid-JSON costs a whole retry.
MAX_TOKENS: Final[int] = 8192

#: Effort levels the API accepts. Checked at construction so a typo in the environment
#: fails at startup rather than as a 400 on the grader's first click (PERF-6).
VALID_EFFORTS: Final[frozenset[str]] = frozenset({"low", "medium", "high", "xhigh", "max"})

#: Models that reject `thinking` and `output_config.effort` outright — sending either is a
#: 400 on every call, not a silently ignored parameter ("adaptive thinking is not
#: supported on this model", "This model does not support the effort parameter").
#:
#: Haiku 4.5 is on this list and it is the model the 5-second adoption gate points at
#: (LP-330: 5.5s single call against 9.6s for Opus 5), so this is not an edge case — it is
#: the main path. Matched on prefix because the pinned id and its dated variants are the
#: same model.
_NO_THINKING_OR_EFFORT: Final[tuple[str, ...]] = ("claude-haiku-4-5",)


def supports_thinking_and_effort(model: str) -> bool:
    """Whether this model accepts `thinking` and `output_config.effort`.

    Sending them where they are not supported does not degrade — it fails the request.
    """
    return not model.startswith(_NO_THINKING_OR_EFFORT)


#: Models that reject `inference_geo` outright: "'claude-haiku-4-5-20251001' does not
#: support inference_geo." Sending it is a 400, not a no-op.
_NO_INFERENCE_GEO: Final[tuple[str, ...]] = ("claude-haiku-4-5",)


def supports_inference_geo(model: str) -> bool:
    """Whether this model can have its inference pinned to a geography (NET-1).

    This is not a performance knob, it is a compliance one, and the answer is a property
    of the model rather than of our configuration. A model that cannot be pinned cannot
    be told to keep label images inside the United States — for a federal customer that
    is a procurement question, not a preference.

    `describe_residency` exists so the answer reaches a human instead of being silently
    dropped by the request builder.
    """
    return not model.startswith(_NO_INFERENCE_GEO)


def describe_residency(model: str, inference_geo: str) -> str:
    """One sentence an operator or a grader can act on. Never silently reassuring."""
    if supports_inference_geo(model):
        return f"Inference for {model} is pinned to {inference_geo!r}."
    return (
        f"{model} does not accept an inference geography, so requests run wherever the "
        f"workspace default sends them. US data residency CANNOT be guaranteed on this "
        f"model — use a model that supports it if residency is required."
    )

#: Image formats the vision endpoint accepts. The preprocessor emits PNG or WebP; this
#: guard exists so an unsupported type is a clear message rather than a raw 400.
SUPPORTED_MEDIA_TYPES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

#: USD per input / output / cached-read token, for the cost line only (OPS-4). Claude
#: Opus 5 list price; cached reads are a tenth of input.
#: List price per million tokens, by model: (input, output). Cache reads are 0.1x input
#: and cache writes 1.25x, which is why writes cannot be quietly folded into either.
#:
#: Keyed by model because a single hardcoded table is a 5x error waiting for someone to
#: change `LABELPROOF_EXTRACTION_MODEL`: Opus 5 and Haiku 4.5 differ by exactly that
#: factor, and the cost line looks equally authoritative either way.
_PRICES_PER_MTOK: Final[dict[str, tuple[float, float]]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

#: Used when the model is not in the table. Deliberately the MOST expensive tier: an
#: unknown model should over-report cost, never under-report it, so a surprise shows up
#: as a number someone questions rather than a number nobody notices.
_UNKNOWN_PRICE_PER_MTOK: Final[tuple[float, float]] = (10.0, 50.0)

_CACHE_READ_MULTIPLIER: Final[float] = 0.1
_CACHE_WRITE_MULTIPLIER: Final[float] = 1.25


def price_for(model: str) -> tuple[tuple[float, float], bool]:
    """`((input, output) per MTok, is_known)` — the second half is the point.

    A caller that cannot tell a priced model from a guessed one will report both with
    the same confidence.
    """
    for known, price in _PRICES_PER_MTOK.items():
        if model.startswith(known):
            return price, True
    return _UNKNOWN_PRICE_PER_MTOK, False


def estimated_usd(usage: ProviderUsage, model: str | None = None) -> float:
    """List-price cost of one extraction. Logged on every call (OPS-4).

    Counts cache WRITES. They are billed at 1.25x input and were previously priced at
    zero, so every cold request — which is every first click a grader makes — under-
    reported by 14-21%, in the flattering direction, into a Cost Analysis deliverable.
    """
    (input_rate, output_rate), known = price_for(model or usage.model)
    if not known and (model or usage.model):
        lp_logging.warn(
            "provider_price_unknown", provider="anthropic", reason_code="unpriced_model"
        )
    per_token = 1 / 1_000_000
    return round(
        usage.input_tokens * input_rate * per_token
        + usage.output_tokens * output_rate * per_token
        + usage.cache_read_tokens * input_rate * _CACHE_READ_MULTIPLIER * per_token
        + usage.cache_creation_tokens * input_rate * _CACHE_WRITE_MULTIPLIER * per_token,
        6,
    )


# --------------------------------------------------------------------------------------
# Structured output schema (LP-051)
# --------------------------------------------------------------------------------------

_NULLABLE_STRING: Final[dict[str, Any]] = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULLABLE_NUMBER: Final[dict[str, Any]] = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_TRISTATE: Final[dict[str, Any]] = {"anyOf": [{"type": "boolean"}, {"type": "null"}]}


def _bbox_schema() -> dict[str, Any]:
    """The evidence box, as a flat `"left,top,right,bottom"` string. Empty means no box.

    A nested object here would be the natural shape, and it is the one the API refuses.
    Structured output compiles the schema to a grammar and enforces two separate ceilings;
    the LP-330 spike hit both on its first real call, and neither is visible offline
    because the fake providers never build a request:

    1. At most 16 union-typed parameters. The natural schema has 20 — seven `value`s,
       seven `bbox`es, `warning_text`, `relative_size`, four tri-states.
    2. A total grammar-size cap. Seven nested four-number objects blow past it *even with
       every union removed*, which is what makes this a representation problem rather than
       a nullability one. (A four-element array is not a way out either: `minItems` above
       1 is unsupported.)

    Flattening the box to a string clears both and — this is the reason to prefer it over
    the alternatives — costs nothing that matters. An evidence box points an agent's eye
    at a region; no verdict depends on one (pinned build decision), a malformed one is dropped
    rather than raised, and the seven boxes were always the cheapest thing in this schema
    to spend. `value` stays nullable and the typography signals stay `bool | None`, which
    is the part that had to survive (LP-067, WARN-6).
    """
    return {"type": "string"}


#: Hard ceiling the Messages API enforces on union-typed parameters in a JSON schema.
#: Asserted by test, because exceeding it is a 400 on every live call and zero test
#: failures — the schema is only ever exercised for real against the API.
MAX_UNION_PARAMETERS: Final[int] = 16


def _field_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "value": _NULLABLE_STRING,
            "on_this_image": {"type": "boolean"},
            "legible": {"type": "boolean"},
            "confidence": {"type": "number"},
            "bbox": _bbox_schema(),
        },
        "required": ["value", "on_this_image", "legible", "confidence", "bbox"],
        "additionalProperties": False,
    }


#: The six elements that are not the government warning. Split out so they can be read
#: CONCURRENTLY with the warning (LP-339): structured output carries a large fixed cost
#: per call, and a single call producing ~700 output tokens measured ~7.3s against ~5.5s
#: for two calls of ~400 and ~190 tokens running at the same time. Same pixels, same
#: model, same fidelity — the saving is only that the two halves stop queueing behind
#: each other.
NON_WARNING_FIELDS: Final[tuple[FieldName, ...]] = tuple(
    name for name in FieldName if name is not FieldName.GOVERNMENT_WARNING
)


def build_fields_schema() -> dict[str, Any]:
    """Everything except the warning."""
    return {
        "type": "object",
        "properties": {
            "is_label": {"type": "boolean"},
            "fields": {
                "type": "object",
                "properties": {name.value: _field_schema() for name in NON_WARNING_FIELDS},
                "required": [name.value for name in NON_WARNING_FIELDS],
                "additionalProperties": False,
            },
        },
        "required": ["is_label", "fields"],
        "additionalProperties": False,
    }


def build_warning_schema() -> dict[str, Any]:
    """The warning statement and its typography, alone.

    Kept whole rather than split further: the text and the type styling are read off the
    same block of print, and a verdict assembled from two separate readings of it would
    be two opinions about one paragraph.
    """
    return {
        "type": "object",
        "properties": {
            "government_warning": _field_schema(),
            "warning_text": _NULLABLE_STRING,
            "warning_typography": {
                "type": "object",
                "properties": {
                    "header_is_all_caps": _TRISTATE,
                    "header_is_bold": _TRISTATE,
                    "body_is_bold": _TRISTATE,
                    "relative_size": _NULLABLE_NUMBER,
                    "contrast_ok": _TRISTATE,
                },
                "required": [
                    "header_is_all_caps",
                    "header_is_bold",
                    "body_is_bold",
                    "relative_size",
                    "contrast_ok",
                ],
                "additionalProperties": False,
            },
        },
        "required": ["government_warning", "warning_text", "warning_typography"],
        "additionalProperties": False,
    }


def build_extraction_schema() -> dict[str, Any]:
    """The JSON schema every extraction must satisfy.

    All seven fields, each with a confidence and an evidence box, plus the warning text
    and the five typography signals. `required` lists every key: a model that leaves a
    field out has not told us it could not read it, and silence is exactly the ambiguity
    this schema exists to remove.
    """
    return {
        "type": "object",
        "properties": {
            "is_label": {"type": "boolean"},
            "fields": {
                "type": "object",
                "properties": {name.value: _field_schema() for name in FieldName},
                "required": [name.value for name in FieldName],
                "additionalProperties": False,
            },
            "warning_text": _NULLABLE_STRING,
            "warning_typography": {
                "type": "object",
                "properties": {
                    "header_is_all_caps": _TRISTATE,
                    "header_is_bold": _TRISTATE,
                    "body_is_bold": _TRISTATE,
                    "relative_size": _NULLABLE_NUMBER,
                    "contrast_ok": _TRISTATE,
                },
                "required": [
                    "header_is_all_caps",
                    "header_is_bold",
                    "body_is_bold",
                    "relative_size",
                    "contrast_ok",
                ],
                "additionalProperties": False,
            },
        },
        "required": ["is_label", "fields", "warning_text", "warning_typography"],
        "additionalProperties": False,
    }


EXTRACTION_SCHEMA: Final[dict[str, Any]] = build_extraction_schema()
FIELDS_SCHEMA: Final[dict[str, Any]] = build_fields_schema()
WARNING_SCHEMA: Final[dict[str, Any]] = build_warning_schema()


# --------------------------------------------------------------------------------------
# The system prompt — fully static, all three commodities (pinned build decision)
# --------------------------------------------------------------------------------------

_FIELD_LABELS: Final[dict[FieldName, str]] = {
    FieldName.BRAND_NAME: "Brand name",
    FieldName.CLASS_TYPE: "Class and type designation",
    FieldName.ALCOHOL_CONTENT: "Alcohol content",
    FieldName.NET_CONTENTS: "Net contents",
    FieldName.PRODUCER: "Producer name and address",
    FieldName.COUNTRY_OF_ORIGIN: "Country of origin",
    FieldName.GOVERNMENT_WARNING: "Government warning statement",
}

_REQUIREMENT_PHRASES: Final[dict[Requirement, str]] = {
    Requirement.REQUIRED: "required",
    Requirement.OPTIONAL: "optional",
    Requirement.REQUIRED_IF_IMPORT: "required only on imported products",
    Requirement.REQUIRED_UNLESS_LOW_ALCOHOL_WINE: (
        'required, except on wine designated "table wine" or "light wine" at 14% alcohol '
        "or below"
    ),
}


def _commodity_rules_text() -> str:
    """Render all three rule sets from the requirement matrix.

    Generated from `api.rules.commodity.REQUIREMENTS` rather than transcribed, so the
    prompt cannot drift away from the table the verdicts are computed against. The table
    is frozen data and iteration order is insertion order, so the bytes are stable —
    which is what the prompt cache needs.
    """
    blocks: list[str] = []
    for commodity, matrix in REQUIREMENTS.items():
        lines = [f"{commodity.value.upper()}"]
        for name in FieldName:
            lines.append(f"  - {_FIELD_LABELS[name]}: {_REQUIREMENT_PHRASES[matrix[name]]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


_ROLE_BLOCK: Final[str] = """\
You read US alcohol beverage labels for a TTB compliance reviewer and report exactly what \
is printed on the image. You do not judge compliance, compare against an application, or \
correct anything — a later step does all of that, and it can only do it correctly if what \
you report is what is actually there.

Three rules govern everything you return.

1. Report only what you can read. If a field is printed on this image but you cannot make \
it out — too small, too blurry, obscured, cut off at the edge — report it with a null \
value and legible set to false. Never supply a plausible value, a partial guess, a \
corrected spelling, or a value carried over from elsewhere on the label. A field reported \
as unreadable is handled correctly downstream; an invented value becomes a wrong verdict \
that no one catches.

2. Distinguish "not on this image" from "unreadable". A label often spans two images with \
the brand on the front and the warning on the back. If a field simply does not appear on \
the image you are looking at, set on_this_image to false, value to null, and legible to \
true — you looked, and it is not here. Reserve legible false for text that is present but \
that you cannot read.

3. Transcribe exactly. Keep the label's own capitalisation, punctuation, spacing, \
abbreviations and line order. Do not expand "ALC/VOL", do not normalise "750ML" to \
"750 mL", do not fix a misspelling. The differences you would smooth away are frequently \
the violation being looked for."""

_FIELDS_BLOCK: Final[str] = """\
The seven fields to look for:

- brand_name — the brand under which the product is sold, usually the largest text.
- class_type — the class and type designation, e.g. "Kentucky Straight Bourbon Whiskey", \
"Cabernet Sauvignon", "India Pale Ale".
- alcohol_content — the alcohol statement exactly as printed, including proof if shown, \
e.g. "45% ALC/VOL (90 PROOF)".
- net_contents — the volume statement as printed, e.g. "750 mL", "12 FL. OZ.".
- producer — the responsible party's name and address as printed, including the bottled \
by / produced by / imported by phrasing that precedes it.
- country_of_origin — e.g. "Product of Scotland", when present.
- government_warning — the full health warning statement, transcribed verbatim including \
its heading and punctuation.

For each field give a confidence between 0 and 1 that your transcription is character-for-\
character correct. Confidence is about legibility and certainty, not about whether you \
think the label is compliant.

Also give bbox, the region the text occupies, as four comma-separated numbers — \
"left,top,right,bottom" — as fractions of image width and height with the origin at the \
top left. For example "0.12,0.30,0.88,0.41". Approximate is fine. Use the empty string \
"" if you did not read the field or would rather not place a box; that costs nothing. \
Never place a box over a region you did not actually read the value from — the box is \
what a reviewer's eye follows to check your work, and one pointing at the wrong text is \
worse than none."""

_WARNING_BLOCK: Final[str] = """\
The government warning statement gets extra attention, and it is the one place where \
saying "I do not know" is genuinely more useful than a guess.

Put the full statement in warning_text exactly as printed — same capitalisation, same \
punctuation, same wording, including any error. Collapse line breaks caused by wrapping \
into single spaces, and change nothing else. If there is no warning statement on this \
image, warning_text is null.

Then report five typography signals about how the statement is printed. Every one of them \
is three-valued: true, false, or null. Null means you could not determine it from this \
image, and it is the correct answer whenever you are unsure. Do not answer false when you \
mean "I could not tell" — false is read downstream as a definite finding that the label \
does not meet the requirement, and a wrong false is a wrong verdict on the most important \
element of the label.

- header_is_all_caps — are the words GOVERNMENT WARNING printed in capital letters? \
False if they are title case or lower case.
- header_is_bold — are the words GOVERNMENT WARNING printed in bold or heavy type, \
noticeably heavier than the text that follows them?
- body_is_bold — is the remainder of the statement, after the heading, printed in bold? \
Judge it by comparing stroke weight against the label's other body text, such as the \
producer's name and address line, and against the heading. Do not assume either answer \
is the common one; if the two runs look the same weight to you and you cannot tell which \
weight that is, the answer is null.
- relative_size — the height of the warning statement's letters divided by the height of \
the label's ordinary body text, such as the producer's name and address line. 1.0 means \
the same size; 0.5 means half as tall. Null if there is no other body text to compare \
against.
- contrast_ok — is the statement readily legible against the background it is printed on? \
False when it is printed in a colour close to the background, screened back, or over busy \
artwork.

If the whole statement is illegible, set warning_text to null, report the \
government_warning field with legible false, and set every typography signal to null. Do \
not infer typography from a statement you could not read."""

_OUTPUT_BLOCK_HEAD: Final[str] = """\
Different products are governed by different rules. All three rule sets are below; the \
user message names which one applies to the label in front of you. The rules do not \
change what you transcribe — they tell you which absences matter, so you look hard for a \
required field before reporting it absent.

"""

_OUTPUT_BLOCK_TAIL: Final[str] = """

Set is_label to false if the image is not an alcohol beverage label at all — a photograph \
of something else, a blank page, a screenshot. In that case report every field with \
on_this_image false and value null; do not try to salvage text from it.

Return the JSON object described by the response schema and nothing else."""


def build_system_blocks() -> list[dict[str, Any]]:
    """The system prompt, as content blocks, with the cache breakpoint on the last one.

    Nothing per-request is interpolated here — not the commodity, not a request id, not a
    timestamp. The bytes are identical on every call the process ever makes, which is the
    only reason the cache pays (pinned build decision).
    """
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": _ROLE_BLOCK},
        {"type": "text", "text": _FIELDS_BLOCK},
        {"type": "text", "text": _WARNING_BLOCK},
        {
            "type": "text",
            "text": _OUTPUT_BLOCK_HEAD + _commodity_rules_text() + _OUTPUT_BLOCK_TAIL,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    return blocks


SYSTEM_BLOCKS: Final[list[dict[str, Any]]] = build_system_blocks()


def build_user_text(commodity: Commodity, role: str | None) -> str:
    """The per-request half. Everything that varies lives here, after the cached prefix."""
    face = {
        "front": "This is the front of the label.",
        "back": "This is the back of the label.",
        "single": "This is the whole label.",
    }.get(role or "", "")
    parts = [
        f"This label is for a {commodity.value} product, so apply the "
        f"{commodity.value.upper()} rule set.",
    ]
    if face:
        parts.append(face)
    parts.append("Read it and return the JSON object.")
    return " ".join(parts)


# --------------------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------------------


class AnthropicVisionProvider:
    """`ExtractionProvider` backed by the Anthropic vision API.

    Constructed once per process. The circuit breaker and the SDK client are shared
    across requests on purpose — a breaker that resets every request has learned nothing,
    and a client rebuilt per request throws away its connection pool.
    """

    name = "anthropic"

    def __init__(
        self,
        config: Config,
        *,
        client: Any | None = None,
        policy: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if config.effort not in VALID_EFFORTS:
            raise ConfigError(
                f"LABELPROOF_EFFORT is {config.effort!r}. It must be one of "
                f"{sorted(VALID_EFFORTS)}."
            )
        if client is None and not config.anthropic_api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set, so the vision adapter cannot be built. "
                "Set it, or set LABELPROOF_FAKE_PROVIDER=1 to run against recorded "
                "fixtures."
            )

        self.config = config
        # Deliberately `Any`: the client is injectable so the suite can run with no
        # network (ENG-3), and a test double is not an `anthropic.Anthropic`. The request
        # payload is pinned by test instead of by the SDK's TypedDicts.
        # `max_retries=0`: retries are ours. The SDK's own backoff is invisible to the
        # deadline and would quietly spend the request budget behind our back.
        self._client: Any = client or anthropic.Anthropic(
            api_key=config.anthropic_api_key, max_retries=0
        )
        self.policy = policy or RetryPolicy()
        self.breaker = breaker if breaker is not None else CircuitBreaker(clock=clock)
        self._sleep = sleep
        self._rand = rand
        self._clock = clock

    # --- the interface ---------------------------------------------------------------

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        """One call per image, all images in flight at once (LP-280)."""
        started = self._clock()
        if not request.images:
            return ExtractionResponse(usage=ProviderUsage(model=self.config.extraction_model))

        deadline = Deadline(self.config.provider_timeout_ms, clock=self._clock)

        # A failure on any image fails the whole extraction. Returning the images that
        # did succeed would look like a complete reading of a label whose front never
        # arrived, and the missing brand name would be reported as Missing — a false
        # finding dressed up as a verdict. Degrading honestly is the lesser cost.
        with ThreadPoolExecutor(max_workers=len(request.images)) as pool:
            futures = [
                pool.submit(self._extract_image, image, request.commodity, deadline)
                for image in request.images
            ]
            results = [future.result() for future in futures]

        extractions = [extraction for extraction, _ in results]
        usage = ProviderUsage(model=self.config.extraction_model)
        for _, one in results:
            usage.merge(one)

        latency_ms = int((self._clock() - started) * 1000)
        lp_logging.log(
            "provider_extract",
            provider=self.name,
            model=self.config.extraction_model,
            commodity=request.commodity.value,
            count=len(request.images),
            duration_ms=latency_ms,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            usd=estimated_usd(usage, self.config.extraction_model),
        )
        extractions.sort(key=lambda e: e.image_index)
        return ExtractionResponse(extractions=extractions, usage=usage, latency_ms=latency_ms)

    # --- one image -------------------------------------------------------------------

    def _extract_image(
        self, image: ImageInput, commodity: Commodity, deadline: Deadline
    ) -> tuple[Extraction, ProviderUsage]:
        run = self._split_call if self.config.split_extraction else self._one_call
        return call_with_retries(
            lambda remaining: run(image, commodity, remaining),
            policy=self.policy,
            deadline=deadline,
            breaker=self.breaker,
            sleep=self._sleep,
            rand=self._rand,
            provider=self.name,
        )

    def _call(
        self,
        image: ImageInput,
        commodity: Commodity,
        timeout_seconds: float,
        schema: dict[str, Any],
        half: str,
    ) -> tuple[Any, ProviderUsage]:
        """One request. Shared by both halves of a split so nothing can diverge between
        them — residency, effort, timeout and retries are settings a compliance claim
        rests on, and two code paths would eventually disagree about one of them.
        """
        if image.media_type not in SUPPORTED_MEDIA_TYPES:
            raise ProviderError(
                f"Images must be PNG, JPEG, WebP or GIF by the time they reach the "
                f"label reading service; this one is {image.media_type}.",
                retryable=False,
            )

        payload = base64.standard_b64encode(image.data).decode("ascii")
        started = self._clock()
        model = self.config.extraction_model

        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        extra_body: dict[str, Any] = {}
        if supports_inference_geo(model):
            extra_body["inference_geo"] = self.config.inference_geo
        extra: dict[str, Any] = {}
        if supports_thinking_and_effort(model):
            output_config["effort"] = self.config.effort
            extra["thinking"] = {"type": "adaptive"}

        try:
            message = self._client.with_options(
                timeout=timeout_seconds, max_retries=0
            ).messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                output_config=output_config,
                **extra_body,
                **extra,
                system=SYSTEM_BLOCKS,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": image.media_type,
                                    "data": payload,
                                },
                            },
                            {"type": "text", "text": build_user_text(commodity, image.role)},
                        ],
                    }
                ],
            )
        except Exception as exc:
            raise _translate(exc) from exc

        usage = _usage_from(message, model)
        lp_logging.log(
            "provider_call",
            provider=self.name,
            model=model,
            image_index=image.index,
            stage=half,
            duration_ms=int((self._clock() - started) * 1000),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            usd=estimated_usd(usage, model),
        )
        return message, usage

    def _split_call(
        self, image: ImageInput, commodity: Commodity, timeout_seconds: float
    ) -> tuple[Extraction, ProviderUsage]:
        """The same reading, as two concurrent calls (LP-339).

        Structured output carries a large fixed cost per call that does not scale with
        how much is asked for: measured on one label, ~700 output tokens took ~7.3s while
        the same work as ~400 and ~190 tokens took ~5.5s when the two ran at once. The
        halves are chosen so neither can answer for the other — the six ordinary elements
        in one, the warning statement and its typography in the other, because those two
        are read off the same block of print and a verdict assembled from two separate
        readings of one paragraph would be two opinions rather than one reading.

        Both halves must succeed. A partial merge would produce an Extraction missing a
        field, and a missing field is reported as Missing — a finding against the label
        manufactured by our own concurrency.
        """
        with ThreadPoolExecutor(max_workers=2) as pool:
            fields_future = pool.submit(
                self._call, image, commodity, timeout_seconds, FIELDS_SCHEMA, "fields"
            )
            warning_future = pool.submit(
                self._call, image, commodity, timeout_seconds, WARNING_SCHEMA, "warning"
            )
            fields_message, fields_usage = fields_future.result()
            warning_message, warning_usage = warning_future.result()

        payload = _payload_of(fields_message)
        warning_payload = _payload_of(warning_message)
        payload["fields"][FieldName.GOVERNMENT_WARNING.value] = warning_payload[
            "government_warning"
        ]
        payload["warning_text"] = warning_payload["warning_text"]
        payload["warning_typography"] = warning_payload["warning_typography"]

        usage = ProviderUsage(model=self.config.extraction_model)
        usage.merge(fields_usage)
        usage.merge(warning_usage)
        return parse_extraction(payload, image.index), usage

    def _one_call(
        self, image: ImageInput, commodity: Commodity, timeout_seconds: float
    ) -> tuple[Extraction, ProviderUsage]:
        if image.media_type not in SUPPORTED_MEDIA_TYPES:
            raise ProviderError(
                f"Images must be PNG, JPEG, WebP or GIF by the time they reach the "
                f"label reading service; this one is {image.media_type}.",
                retryable=False,
            )

        payload = base64.standard_b64encode(image.data).decode("ascii")
        started = self._clock()

        model = self.config.extraction_model
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}
        }
        # Data residency, asserted rather than assumed (NET-1). Without this, requests
        # follow the workspace default inference geography — `global` unless someone has
        # configured otherwise — and the claim that label images never leave the United
        # States is one the code does not make. For a federal customer that distinction
        # is the whole question, and it is one parameter.
        #
        # Not every model accepts it. Haiku 4.5 rejects it with a 400, so pinning is a
        # capability the model either has or lacks — see `supports_inference_geo`, and
        # `describe_residency` for the sentence that says so out loud.
        extra_body: dict[str, Any] = {}
        if supports_inference_geo(model):
            extra_body["inference_geo"] = self.config.inference_geo
        extra: dict[str, Any] = {}
        if supports_thinking_and_effort(model):
            output_config["effort"] = self.config.effort
            extra["thinking"] = {"type": "adaptive"}

        try:
            message = self._client.with_options(
                timeout=timeout_seconds, max_retries=0
            ).messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                output_config=output_config,
                **extra_body,
                **extra,
                system=SYSTEM_BLOCKS,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": image.media_type,
                                    "data": payload,
                                },
                            },
                            {"type": "text", "text": build_user_text(commodity, image.role)},
                        ],
                    }
                ],
            )
        except Exception as exc:
            raise _translate(exc) from exc

        usage = _usage_from(message, self.config.extraction_model)
        extraction = _parse_message(message, image.index)

        lp_logging.log(
            "provider_call",
            provider=self.name,
            model=self.config.extraction_model,
            image_index=image.index,
            duration_ms=int((self._clock() - started) * 1000),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            usd=estimated_usd(usage, self.config.extraction_model),
        )
        return extraction, usage


# --------------------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------------------

_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset(
    {408, 409, 425, 429, 500, 502, 503, 504, 529}
)


def _translate(exc: Exception) -> ProviderError:
    """Every SDK failure becomes a `ProviderError` with an honest retryable flag.

    Retryable means "the same request might work in a moment". A 400 will not, and
    retrying it three times only spends the budget more slowly.
    """
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError("The label reading service timed out.", retryable=True)
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError("Could not reach the label reading service.", retryable=True)
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", 500)
        return ProviderError(
            f"The label reading service returned an error (HTTP {status}).",
            retryable=status in _RETRYABLE_STATUSES,
        )
    return ProviderError(f"The label reading service failed: {type(exc).__name__}.", retryable=True)


# --------------------------------------------------------------------------------------
# Response parsing and validation (LP-051)
# --------------------------------------------------------------------------------------


def _usage_from(message: Any, model: str) -> ProviderUsage:
    """Token accounting on every call, whatever the outcome (OPS-4)."""
    usage = getattr(message, "usage", None)
    return ProviderUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_creation_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        model=model,
    )


def _first_text(message: Any) -> str:
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return str(getattr(block, "text", ""))
    raise ProviderError(
        "The label reading service returned no readable answer.", retryable=True
    )


def _payload_of(message: Any) -> dict[str, Any]:
    """The JSON body of one response, with the same refusals `_parse_message` enforces.

    Split out so both halves of a split call are checked identically — a refusal or a
    truncated answer on the warning half must fail the extraction exactly as it would
    have when the two were one request.
    """
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason == "refusal":
        raise ProviderError(
            "The label reading service declined to process this image. Nothing has "
            "been checked — check this label by eye.",
            retryable=False,
        )
    if stop_reason == "max_tokens":
        raise ProviderError(
            "The label reading service's answer was cut off before it finished.",
            retryable=True,
        )
    try:
        body: dict[str, Any] = json.loads(_first_text(message))
    except json.JSONDecodeError as exc:
        raise ProviderError(
            "The label reading service returned an answer that could not be read.",
            retryable=True,
        ) from exc
    return body


def _parse_message(message: Any, image_index: int) -> Extraction:
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason == "refusal":
        raise ProviderError(
            "The label reading service declined to process this image. Nothing has "
            "been checked — check this label by eye.",
            retryable=False,
        )
    if stop_reason == "max_tokens":
        raise ProviderError(
            "The label reading service's answer was cut off before it finished.",
            retryable=True,
        )

    try:
        payload = json.loads(_first_text(message))
    except json.JSONDecodeError as exc:
        raise ProviderError(
            "The label reading service returned an answer that could not be read.",
            retryable=True,
        ) from exc

    return parse_extraction(payload, image_index)


def _validated_bbox(raw: Any) -> BoundingBox | None:
    """Build the evidence box, or drop it. A bad box never fails an extraction.

    An evidence box points an agent's eye at a region and no verdict depends on it
    (pinned build decision). Throwing away a correctly-read brand name because the model returned
    a box with x1 of 1.02 would trade something load-bearing for something decorative.
    """
    # A dict is still accepted so a recorded fixture or an offline provider can hand over
    # a box in its natural shape. The wire format from the live model is the flat string
    # described in `_bbox_schema`.
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            # The empty string is "no box" — the expected answer for any field the model
            # did not read. Not a defect, so it is not logged as one; a warning on every
            # unread field would bury the real ones.
            return None
        parts = text.split(",")
        if len(parts) != 4:
            lp_logging.warn("provider_bbox_dropped", provider="anthropic", reason_code="malformed")
            return None
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            lp_logging.warn("provider_bbox_dropped", provider="anthropic", reason_code="malformed")
            return None
        raw = dict(zip(("x0", "y0", "x1", "y1"), numbers, strict=True))

    if not isinstance(raw, dict):
        return None

    try:
        box = BoundingBox.model_validate(raw)
    except ValidationError:
        lp_logging.warn("provider_bbox_dropped", provider="anthropic", reason_code="out_of_range")
        return None
    if box.x1 <= box.x0 or box.y1 <= box.y0:
        lp_logging.warn("provider_bbox_dropped", provider="anthropic", reason_code="inverted")
        return None
    return box


def _tristate(raw: Any, key: str) -> bool | None:
    """`bool | None`, and never a coercion.

    Anything that is not a bool is `None` — "could not determine" — because the one
    outcome that must be impossible here is an unknown becoming `False`. `False` means
    "we looked, and the label does not comply"; `None` routes to Needs review. Guessing
    in that direction is how a non-compliant warning silently passes (WARN-6).
    """
    if isinstance(raw, bool):
        return raw
    if raw is not None:
        lp_logging.warn(
            "provider_typography_unusable", provider="anthropic", reason_code=key
        )
    return None


def parse_extraction(payload: Any, image_index: int) -> Extraction:
    """Validate the model's JSON and turn it into an `Extraction`.

    Structured output makes the shape very likely to be right; validating anyway is the
    point of LP-051. The schema is a request, not a guarantee, and this is the boundary
    where a surprise stops being cheap.
    """
    if not isinstance(payload, dict):
        raise ProviderError(
            "The label reading service returned an answer in an unexpected shape.",
            retryable=True,
        )

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, dict):
        raise ProviderError(
            "The label reading service's answer was missing its field readings.",
            retryable=True,
        )

    fields: dict[FieldName, ExtractedField] = {}
    for name in FieldName:
        raw = raw_fields.get(name.value)
        if not isinstance(raw, dict):
            raise ProviderError(
                f"The label reading service did not report the {name.value} field.",
                retryable=True,
            )

        legible = bool(raw.get("legible", True))
        value = raw.get("value")
        if value is not None and not isinstance(value, str):
            raise ProviderError(
                "The label reading service returned a non-text value for a label field.",
                retryable=True,
            )
        value = value.strip() if isinstance(value, str) else None
        value = value or None

        # A field that is not on this image is left out entirely rather than reported as
        # an empty reading. Downstream, an absent key and a null value mean the same
        # thing; omitting keeps the merge across front and back images unambiguous and
        # mirrors what the offline providers produce.
        on_this_image = bool(raw.get("on_this_image", True))
        if value is None and legible and not on_this_image:
            continue

        confidence = raw.get("confidence", 0.0)
        if not isinstance(confidence, int | float) or isinstance(confidence, bool):
            raise ProviderError(
                "The label reading service returned an unusable confidence score.",
                retryable=True,
            )
        if not 0.0 <= float(confidence) <= 1.0:
            raise ProviderError(
                f"The label reading service returned a confidence of {confidence}, "
                f"which is outside 0 to 1.",
                retryable=True,
            )

        fields[name] = ExtractedField(
            value=value,
            # No value read means no confidence in a value, whatever the model claimed.
            confidence=float(confidence) if value is not None else 0.0,
            legible=legible,
            bbox=_validated_bbox(raw.get("bbox")),
        )

    warning_text = payload.get("warning_text")
    if warning_text is not None and not isinstance(warning_text, str):
        raise ProviderError(
            "The label reading service returned a non-text warning statement.",
            retryable=True,
        )
    warning_text = warning_text.strip() if isinstance(warning_text, str) else None
    warning_text = warning_text or None

    raw_typography = payload.get("warning_typography")
    raw_typography = raw_typography if isinstance(raw_typography, dict) else {}
    relative_size = raw_typography.get("relative_size")
    if isinstance(relative_size, bool) or not isinstance(relative_size, int | float):
        relative_size = None

    typography = WarningTypography(
        header_is_all_caps=_tristate(
            raw_typography.get("header_is_all_caps"), "header_is_all_caps"
        ),
        header_is_bold=_tristate(raw_typography.get("header_is_bold"), "header_is_bold"),
        body_is_bold=_tristate(raw_typography.get("body_is_bold"), "body_is_bold"),
        relative_size=float(relative_size) if relative_size is not None else None,
        contrast_ok=_tristate(raw_typography.get("contrast_ok"), "contrast_ok"),
    )

    # Typography read off a statement we could not read is not a determination. Drop it
    # rather than let it be treated as one.
    warning_field = fields.get(FieldName.GOVERNMENT_WARNING)
    if warning_text is None or (warning_field is not None and not warning_field.legible):
        typography = WarningTypography()

    return Extraction(
        image_index=image_index,
        is_label=bool(payload.get("is_label", True)),
        fields=fields,
        warning_text=warning_text,
        warning_typography=typography,
    )


# --------------------------------------------------------------------------------------
# Tier 3 — the adjudicator (LP-220, MATCH-4)
# --------------------------------------------------------------------------------------

#: Closed schema, same discipline as the extraction one. `additionalProperties: false`
#: and every key required, so the model cannot answer with a shrug and cannot omit the
#: confidence the rules engine gates on.
ADJUDICATION_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "same_thing": {
            "type": "boolean",
            "description": (
                "True only if these name the same entity or designation and a TTB "
                "specialist would treat the difference as immaterial."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "0 to 1. How sure you are of the judgement above.",
        },
        "rationale": {
            "type": "string",
            "description": (
                "One sentence, plain English, naming the specific difference. Written "
                "for a compliance officer, not for a developer."
            ),
        },
    },
    "required": ["same_thing", "confidence", "rationale"],
    "additionalProperties": False,
}

#: The judge's instructions.
#:
#: Three things it is deliberately NOT told, each of which would change the answer:
#:
#: **The image.** This tier answers "are these two strings the same thing". Handing it the
#: artwork would invite it to re-read the label, which is Tier 0's job and is already done
#: — and a second reading that disagreed with the first would be resolved by whichever
#: happened to run last.
#:
#: **Which one came from the label.** The question is symmetric, and telling it that one
#: side is "what the applicant filed" invites deference to the filing. What matters is
#: whether they denote the same thing.
#:
#: **That a Mismatch is the current verdict.** Framing it as "we found a problem, is it
#: really one" is an invitation to be helpful, and helpful here means clearing things.
ADJUDICATION_SYSTEM: Final[str] = """\
You are assisting a TTB compliance specialist reviewing an alcohol beverage label \
application. Two values are given for the same field: one printed on the label, one \
recorded in the application. An automatic comparison could not resolve them.

Decide whether they name the same thing.

Say YES when the difference is a matter of expression rather than substance:
  - word order, when the words are the same entity ("Old Tom Distillery" / "Distillery of Old Tom")
  - a standard abbreviation ("Co." / "Company", "&" / "and", "St." / "Street")
  - a trading name shown alongside or instead of a registered name
  - punctuation, spacing, or a legal suffix that does not change who or what is named

Say NO when anything of substance differs. Two entities with similar names are NOT the \
same entity. A different city, a different state, a different product class, a different \
brand — all NO, however small the edit distance looks.

If you are unsure, say NO and give a low confidence. A wrongly cleared difference reaches \
a federal agency as an approval; a wrongly kept one costs a specialist thirty seconds of \
reading. Those are not comparable errors, and you should be biased accordingly.

Your rationale is shown to the specialist and must name the actual difference in one \
plain sentence."""


class AnthropicAdjudicator:
    """Tier 3 against the real model (LP-220).

    Text-only and on the cheap model: this compares two short strings and never sees an
    image, so it is a different workload from extraction and priced like one. The rules
    engine bounds it before it is ever called — see `api/rules/adjudicate.py` — so there
    is no timeout negotiation or retry policy here. One call, one answer, and any failure
    raises so the caller can leave the Mismatch standing.
    """

    name = "anthropic:adjudicator"

    def __init__(self, config: Config, client: Any | None = None) -> None:
        self.config = config
        # Same `Any` and same `max_retries=0` as the extractor, for the same two
        # reasons: the client is injectable so CI runs with no network, and retries here
        # would spend a request budget the rules engine already measured.
        self._client: Any = client or anthropic.Anthropic(
            api_key=config.anthropic_api_key, max_retries=0
        )

    def judge(self, request: AdjudicationRequest) -> Judgement:
        model = self.config.adjudication_model
        extra: dict[str, Any] = {}
        if supports_inference_geo(model):
            extra["inference_geo"] = self.config.inference_geo

        message = self._client.with_options(
            timeout=_ADJUDICATION_TIMEOUT_S, max_retries=0
        ).messages.create(
            model=model,
            max_tokens=_ADJUDICATION_MAX_TOKENS,
            output_config={
                "format": {"type": "json_schema", "schema": ADJUDICATION_SCHEMA}
            },
            system=[{"type": "text", "text": ADJUDICATION_SYSTEM}],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Field: {request.field.value.replace('_', ' ')}\n"
                        f"Commodity: {request.commodity}\n"
                        f"Value A: {request.extracted}\n"
                        f"Value B: {request.expected}\n\n"
                        "Do these name the same thing?"
                    ),
                }
            ],
            **extra,
        )

        payload = json.loads(_first_text(message))
        same = payload.get("same_thing")
        confidence = payload.get("confidence")
        if not isinstance(same, bool) or not isinstance(confidence, int | float):
            raise ProviderError(
                "The adjudicating model returned an answer this service could not read."
            )
        return Judgement(
            same_thing=same,
            confidence=max(0.0, min(1.0, float(confidence))),
            rationale=str(payload.get("rationale", "")).strip(),
        )


#: Six seconds. Generous for two short strings on the cheap model, and irrelevant most of
#: the time because the rules engine declines to call at all unless the request budget has
#: room — this is the backstop for a call that hangs, not the budget.
_ADJUDICATION_TIMEOUT_S: Final[float] = 6.0
_ADJUDICATION_MAX_TOKENS: Final[int] = 300


# --------------------------------------------------------------------------------------
# Escalation — the second look at the warning region (LP-325, LP-326, IMG-5)
# --------------------------------------------------------------------------------------

#: What the second look is asked for. Narrower than the extraction schema on purpose: one
#: region, one question. Tri-state on every typography signal, because the whole reason to
#: escalate is that the first pass could not tell — and a second pass that converts its own
#: uncertainty into a boolean to look useful would defeat the design it was added to serve.
REREAD_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "warning_text": {
            "type": ["string", "null"],
            "description": (
                "The statement exactly as printed, or null if it cannot be read. Never "
                "complete it from memory — a warning you recall is not a warning you saw."
            ),
        },
        "header_is_all_caps": {"type": ["boolean", "null"]},
        "header_is_bold": {"type": ["boolean", "null"]},
        "body_is_bold": {"type": ["boolean", "null"]},
        "contrast_ok": {"type": ["boolean", "null"]},
    },
    "required": [
        "warning_text",
        "header_is_all_caps",
        "header_is_bold",
        "body_is_bold",
        "contrast_ok",
    ],
    "additionalProperties": False,
}

REREAD_SYSTEM: Final[str] = """\
You are looking at a cropped region of an alcohol beverage label at full resolution. A \
first pass could not resolve something about the government warning, so this is a second, \
closer look at that region alone.

Report only what you can see in THIS image.

Use null for any typography question you cannot answer from these pixels. Null is the \
correct answer far more often than people expect — bold is hard to judge without a \
comparison, and contrast is hard to judge at all. A null costs a compliance specialist \
one glance at the label. A guess that happens to be wrong clears a defective warning on a \
federal filing.

Never reproduce the statement from memory. If the text is not legible here, warning_text \
is null. The wording of this warning is fixed by regulation and you almost certainly know \
it — that is precisely why reciting it would be worthless."""


class AnthropicWarningRereader:
    """The adapter for `typography.WarningRereader` (LP-326).

    Sends ONE crop of ONE image. The crop is taken from the preprocessed frame the first
    pass measured its bbox against, so the region is the region — cropping the original
    upload with a box measured after a downscale would move it.

    When there is no bbox the whole image is sent rather than a guessed crop. A crop that
    clips the warning is worse than no crop: it produces a confident reading of half a
    statement, which is a false pass with evidence attached.
    """

    name = "anthropic:warning-rereader"

    def __init__(
        self,
        config: Config,
        frames: dict[int, Any],
        client: Any | None = None,
    ) -> None:
        # The frames are a CONSTRUCTOR argument because they belong to one request. An
        # adapter built once per process and handed images through a setter would let a
        # second request read the first one's label — a bug that only appears under
        # concurrency and is a privacy incident when it does.
        self.config = config
        self._frames = frames
        self._client: Any = client or anthropic.Anthropic(
            api_key=config.anthropic_api_key, max_retries=0
        )

    def reread_warning(self, request: WarningRereadRequest) -> WarningReread:
        frame = self._frames.get(request.image_index)
        if frame is None:
            raise ProviderError(
                "The warning region could not be re-read because that image is not "
                "available.",
                retryable=False,
            )

        # Crop to the region the first pass measured, at the resolution it measured it
        # against — more pixels on the SAME region is the entire point of escalating.
        # Sending the whole frame again spends a second call to look at the same thing at
        # the same size.
        #
        # No bbox means send the whole frame rather than guess a crop. A crop that clips
        # the warning produces a confident reading of half a statement, which is a false
        # pass with evidence attached (LP-326).
        region = quality_mod.crop(frame, request.bbox) if request.bbox else frame
        payload = base64.standard_b64encode(_png_bytes(region)).decode("ascii")
        model = self.config.extraction_model
        extra: dict[str, Any] = {}
        if supports_inference_geo(model):
            extra["inference_geo"] = self.config.inference_geo

        message = self._client.with_options(
            timeout=_REREAD_TIMEOUT_S, max_retries=0
        ).messages.create(
            model=model,
            max_tokens=_REREAD_MAX_TOKENS,
            output_config={"format": {"type": "json_schema", "schema": REREAD_SCHEMA}},
            system=[{"type": "text", "text": REREAD_SYSTEM}],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": payload,
                            },
                        },
                        {"type": "text", "text": request.reason or "Read this region."},
                    ],
                }
            ],
            **extra,
        )

        body = json.loads(_first_text(message))
        text = body.get("warning_text")
        return WarningReread(
            warning_text=str(text) if isinstance(text, str) and text.strip() else None,
            typography=WarningTypography(
                header_is_all_caps=_tri(body.get("header_is_all_caps")),
                header_is_bold=_tri(body.get("header_is_bold")),
                body_is_bold=_tri(body.get("body_is_bold")),
                contrast_ok=_tri(body.get("contrast_ok")),
            ),
            model=model,
        )

def _png_bytes(frame: Any) -> bytes:
    """Encode a cropped frame.

    PNG rather than WebP. This is the escalation path — reached because the first pass
    could not resolve 4-point type — and lossy artefacts on exactly that type are what the
    second look exists to see through. The region is small, so the bytes are cheap.
    """
    import cv2

    ok, buffer = cv2.imencode(".png", frame)
    if not ok:
        raise ProviderError("The warning region could not be encoded.", retryable=False)
    return bytes(buffer.tobytes())


def _tri(value: Any) -> bool | None:
    """Anything that is not exactly a bool is None. A string "true" is not a reading."""
    return value if isinstance(value, bool) else None


#: Eight seconds. One crop on the extraction model, and the escalation is only reached
#: when the first pass already abstained — so the request has spent most of its budget by
#: then and this is a backstop rather than an allowance.
_REREAD_TIMEOUT_S: Final[float] = 8.0
_REREAD_MAX_TOKENS: Final[int] = 1500
