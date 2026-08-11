"""DEFECT: the extraction schema was structurally invalid and no offline test could tell.

**This is the incident this whole test layer exists because of.**

The JSON schema sent to the Messages API for structured output broke two separate
documented ceilings at once:

1. **At most 16 union-typed parameters.** The natural schema had twenty — seven field
   `value`s, seven `bbox`es, `warning_text`, `relative_size`, and four tri-states.
2. **A total compiled-grammar size cap.** Seven nested four-number objects blew past it
   *even with every union removed*, which is what made this a representation problem
   rather than a nullability one. (A four-element array is not a way out either:
   `minItems` above 1 is not supported.)

**Every live call returned HTTP 400 before the model ever saw an image.** And 624
offline tests passed against it, across 123 tickets, because the fake providers return
already-parsed `Extraction` objects and never build a request. The contract with the
outside world was the one thing nothing exercised.

**The fix.** Flatten the evidence box to a `"left,top,right,bottom"` string. That clears
both ceilings and costs nothing that matters: a box points an agent's eye at a region,
no verdict depends on one, and a malformed one is dropped rather than raised. `value`
stays nullable and the typography signals stay `bool | None`, which is the part that had
to survive (LP-067, WARN-6).

The tests below count what the schema actually contains, offline, on every run. The last
two reconstruct the *pre-fix* shape and assert that it violates the limits — a check
with no teeth is how this got shipped, and a limit check that would pass on the broken
schema is exactly that.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from api.models import FieldName
from api.provider import anthropic_adapter as adapter

pytestmark = pytest.mark.regression

#: A conservative stand-in for the API's compiled-grammar ceiling, expressed in
#: serialized schema bytes. The real limit is on the compiled grammar and is not
#: published as a byte count, so this is a proxy: it is set well below the size that
#: failed and comfortably above the size that works, and its job is to fail loudly when
#: the schema starts growing in the direction that broke it. A proxy that fires early is
#: the right kind of wrong here — the alternative is finding out from a 400 in
#: production.
GRAMMAR_SIZE_BUDGET_BYTES = 4096

#: Nested-object levels the grammar may contain. The compiled grammar grows fast with
#: this: the shipped schema was four objects deep (root -> fields -> field -> bbox); the
#: fix is three, because the box became a string.
MAX_OBJECT_NESTING = 3


def _count_unions(node: Any) -> int:
    """Every parameter whose type is a union — what the 16-parameter ceiling counts."""
    if isinstance(node, dict):
        total = 1 if "anyOf" in node or isinstance(node.get("type"), list) else 0
        return total + sum(_count_unions(child) for child in node.values())
    if isinstance(node, list):
        return sum(_count_unions(child) for child in node)
    return 0


def _object_nesting(node: Any) -> int:
    """How many `type: object` levels deep the schema goes.

    Counts objects rather than raw dictionary levels: `properties` and `anyOf` are
    encoding, not structure, and counting them would make the number sensitive to how
    JSON Schema happens to spell things rather than to what the grammar has to compile.
    """
    if isinstance(node, dict):
        here = 1 if node.get("type") == "object" else 0
        children = [_object_nesting(child) for child in node.values()]
        return here + max(children, default=0)
    if isinstance(node, list):
        return max((_object_nesting(child) for child in node), default=0)
    return 0


def _walk_objects(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for child in node.values():
            found.extend(_walk_objects(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_walk_objects(child))
    return found


# --------------------------------------------------------------------------------------
# The two ceilings the shipped schema broke
# --------------------------------------------------------------------------------------


def test_the_schema_is_within_the_union_parameter_ceiling() -> None:
    """Ceiling one, counted offline on every run.

    `MAX_UNION_PARAMETERS` lives in the adapter next to the schema so that the limit
    and the thing it constrains cannot drift apart.
    """
    unions = _count_unions(adapter.EXTRACTION_SCHEMA)
    assert unions <= adapter.MAX_UNION_PARAMETERS, (
        f"{unions} union-typed parameters; the API accepts at most "
        f"{adapter.MAX_UNION_PARAMETERS}. Every live call would return HTTP 400."
    )


def test_the_schema_is_within_the_grammar_size_budget() -> None:
    """Ceiling two, which removing every union would not have fixed.

    This is the ceiling that made flattening the box the right answer rather than a
    workaround: seven nested four-number objects exceeded it on their own.
    """
    size = len(json.dumps(adapter.EXTRACTION_SCHEMA, separators=(",", ":")))
    assert size <= GRAMMAR_SIZE_BUDGET_BYTES, (
        f"schema serialises to {size} bytes, budget {GRAMMAR_SIZE_BUDGET_BYTES}"
    )


def test_the_schema_stays_shallow() -> None:
    """Depth is what the grammar size is most sensitive to."""
    assert _object_nesting(adapter.EXTRACTION_SCHEMA) <= MAX_OBJECT_NESTING


def test_the_evidence_box_is_a_flat_string_rather_than_a_nested_object() -> None:
    """The specific representation choice, pinned so it cannot be quietly reverted.

    A nested object here is the natural shape and the one the API refuses. Someone
    tidying the schema would reach for it without knowing why it is not already that
    way.
    """
    fields = adapter.EXTRACTION_SCHEMA["properties"]["fields"]["properties"]
    for name in FieldName:
        assert fields[name.value]["properties"]["bbox"] == {"type": "string"}


# --------------------------------------------------------------------------------------
# The checks have teeth: the pre-fix schema must fail them
# --------------------------------------------------------------------------------------


def _pre_fix_schema() -> dict[str, Any]:
    """The schema as it shipped: nested bbox objects, nullable numbers throughout.

    Reconstructed rather than remembered. Recreating the failing shape is the only way
    to know the limit checks above would actually have caught it.
    """
    nullable_number = {"anyOf": [{"type": "number"}, {"type": "null"}]}
    nested_bbox = {
        "type": "object",
        "properties": {axis: nullable_number for axis in ("x0", "y0", "x1", "y1")},
        "required": ["x0", "y0", "x1", "y1"],
        "additionalProperties": False,
    }
    schema = json.loads(json.dumps(adapter.EXTRACTION_SCHEMA))
    for name in FieldName:
        schema["properties"]["fields"]["properties"][name.value]["properties"]["bbox"] = (
            json.loads(json.dumps(nested_bbox))
        )
    return schema


def test_the_union_check_would_have_caught_the_shipped_schema() -> None:
    """If this passes, the union check is decoration."""
    assert _count_unions(_pre_fix_schema()) > adapter.MAX_UNION_PARAMETERS


def test_the_grammar_budget_would_have_caught_the_shipped_schema() -> None:
    """If this passes, the size budget is decoration."""
    size = len(json.dumps(_pre_fix_schema(), separators=(",", ":")))
    assert size > GRAMMAR_SIZE_BUDGET_BYTES


def test_the_nesting_check_would_have_caught_the_shipped_schema() -> None:
    """The nested bbox is the fourth object level. If this passes, so is that check."""
    assert _object_nesting(_pre_fix_schema()) > MAX_OBJECT_NESTING


# --------------------------------------------------------------------------------------
# What the fix was not allowed to cost
# --------------------------------------------------------------------------------------


def test_field_values_are_still_nullable() -> None:
    """LP-067: a field the model could not read comes back null, never a guess.

    The cheapest way to get under the union ceiling would have been to make `value` a
    plain string. That would have forced the model to invent text for every unreadable
    field — trading a 400 for a false pass, which is the wrong direction.
    """
    fields = adapter.EXTRACTION_SCHEMA["properties"]["fields"]["properties"]
    for name in FieldName:
        assert fields[name.value]["properties"]["value"] == {
            "anyOf": [{"type": "string"}, {"type": "null"}]
        }


def test_the_typography_signals_are_still_tri_state() -> None:
    """WARN-6: `null` means "could not determine" and must never become `false`.

    `false` reads downstream as "we checked, and it is not bold" — a determination we
    did not make. On the government warning statement that is the false pass the
    product exists to prevent.
    """
    typography = adapter.EXTRACTION_SCHEMA["properties"]["warning_typography"]["properties"]
    for signal in ("header_is_all_caps", "header_is_bold", "body_is_bold", "contrast_ok"):
        assert typography[signal] == {"anyOf": [{"type": "boolean"}, {"type": "null"}]}


def test_every_object_forbids_additional_properties() -> None:
    """Structured output requires it, and a missing one is a 400 on every call."""
    for node in _walk_objects(adapter.EXTRACTION_SCHEMA):
        assert node.get("additionalProperties") is False


def test_every_object_requires_all_of_its_properties() -> None:
    """A model that leaves a field out has not told us it could not read it.

    Silence is exactly the ambiguity this schema exists to remove — and strict
    structured output rejects a schema whose `required` is not the full property list.
    """
    for node in _walk_objects(adapter.EXTRACTION_SCHEMA):
        assert sorted(node.get("required", [])) == sorted(node["properties"])


def test_the_schema_uses_no_unsupported_json_schema_keywords() -> None:
    """Structured output supports a subset of JSON Schema, and this is the subset.

    `minItems`, `minLength`, `minimum` and friends are not supported — a schema using
    one is rejected. The bbox array alternative died on `minItems` specifically, so the
    keyword is worth naming here rather than leaving as folklore in a docstring.
    """
    unsupported = {
        "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "pattern",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "minProperties", "maxProperties", "patternProperties", "dependentSchemas",
        "if", "then", "else", "not", "oneOf",
    }
    serialized = json.dumps(adapter.EXTRACTION_SCHEMA)
    used = sorted(keyword for keyword in unsupported if f'"{keyword}"' in serialized)
    assert used == [], f"schema uses unsupported keywords: {used}"


def test_the_schema_is_deterministic_across_calls() -> None:
    """The same bytes every time, because the prompt cache is a prefix match.

    A schema whose key order varied would invalidate the cached prefix on every
    request, and the only symptom would be a cost line nobody was watching.
    """
    assert json.dumps(adapter.build_extraction_schema(), sort_keys=False) == json.dumps(
        adapter.build_extraction_schema(), sort_keys=False
    )
