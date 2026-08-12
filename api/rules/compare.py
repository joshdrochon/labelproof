"""Field comparators — label value against application value, producing verdicts.

Every comparator follows the same precedence, and the order is the product:

1. **Not legible** -> Unreadable. We did not check it; say so.
2. **Not required** and absent -> Not applicable. Never Missing.
3. **Required** and absent -> Missing.
4. Tier 1: equal after normalization -> Match.
5. Tier 2: difference is explainable -> Acceptable variation, with the note.
6. Otherwise -> Mismatch.

Unreadable outranks Missing because "we could not read it" and "it is not there" are
different findings, and reporting the second when the first is true is a false finding on
a compliant label. Not applicable outranks Missing for the same reason.

Tier 3 (LLM adjudication) is not wired here — gray cases fall through to Mismatch until
`adjudicate.py` lands, which is the safe direction (asymmetry law: flag, never pass).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from api.models import (
    Commodity,
    Evidence,
    ExtractedField,
    FieldName,
    FieldResult,
    Finding,
    Verdict,
)
from api.rules import abv as abv_rules
from api.rules import commodity as com
from api.rules import fills as fill_rules
from api.rules.normalize import (
    classify_variation,
    contains_after_normalization,
    equal_after_normalization,
    surrounding_words,
    variation_note,
)

#: US state and territory names, for address tolerance. `Frankfort, KY` and
#: `Frankfort, Kentucky` are the same address written two ways.
_STATE_ABBREVIATIONS: dict[str, str] = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
    "pr": "puerto rico",
}

#: A two-letter code is a state only where a state goes: immediately after the comma that
#: ends the city, at the end of the address or before another comma, optionally with a ZIP
#: between. Expanding it anywhere a standalone word happens to match is a FALSE-PASS
#: machine, and it was one — the expansion is many-to-one, so distinct producers collided
#: into the same normalized string and compared as an exact Tier-1 Match:
#:
#:     La Crema Winery, Windsor, CA      == Louisiana Crema Winery, Windsor, CA
#:     Casa de Campo, Ponce, PR          == Casa Delaware Campo, Ponce, PR
#:     Mo's Distillery, Bend, OR         == Missouri's Distillery, Bend, OR
#:     In-N-Out Spirits, Baltimore, MD   == Indiana-N-Out Spirits, Baltimore, MD
#:     Old Tom Distilling Co, ...        == Old Tom Distilling Colorado, ...
#:
#: Symmetry does not save this. Applying the same corruption to both sides prevents false
#: MISMATCHes; it does nothing about false MATCHes, because two different inputs can map
#: onto one output. That last row is the one that would actually have shipped — every
#: producer ending in "Co" or "Co." was becoming "colorado" (FIELD-5, and the asymmetry
#: law: a false flag costs seconds, a false pass costs a compliance failure).
_ZIP = r"\d{5}(?:-\d{4})?"
_STATE_AFTER_COMMA = re.compile(rf",(\s*)([a-z]{{2}})\b(?=\s*(?:{_ZIP})?\s*(?:,|$))")
_STATE_BEFORE_ZIP = re.compile(rf"\b([a-z]{{2}})\b(?=\s+{_ZIP}\b)")


def expand_state_abbreviations(address: str) -> str:
    """Rewrite two-letter state codes to full names so either form compares equal.

    `Frankfort, KY` and `Frankfort, Kentucky` are the same address. `Gin or Vodka` and
    `Gin Oregon Vodka` are not, which is why position is checked and not just the word.
    """
    folded = address.casefold()

    def after_comma(match: re.Match[str]) -> str:
        full = _STATE_ABBREVIATIONS.get(match.group(2))
        return match.group(0) if full is None else f",{match.group(1)}{full}"

    def before_zip(match: re.Match[str]) -> str:
        return _STATE_ABBREVIATIONS.get(match.group(1), match.group(1))

    return _STATE_BEFORE_ZIP.sub(before_zip, _STATE_AFTER_COMMA.sub(after_comma, folded))


def _missing(field: FieldName, expected: str | None, label: str) -> FieldResult:
    return FieldResult(
        field=field,
        verdict=Verdict.MISSING,
        extracted=None,
        expected=expected,
        confidence=1.0,
        rationale=f"The application states {label}, but none appears on the label.",
    )


def _unreadable(field: FieldName, expected: str | None, label: str) -> FieldResult:
    return FieldResult(
        field=field,
        verdict=Verdict.UNREADABLE,
        extracted=None,
        expected=expected,
        confidence=0.0,
        rationale=(
            f"The {label} could not be read on this image. It has not been checked — "
            f"request a clearer image."
        ),
    )


def _not_applicable(field: FieldName, reason: str) -> FieldResult:
    return FieldResult(
        field=field,
        verdict=Verdict.NOT_APPLICABLE,
        extracted=None,
        expected=None,
        confidence=1.0,
        rationale=reason,
    )


def compare_text(
    field: FieldName,
    extracted: ExtractedField | None,
    expected: str | None,
    *,
    required: bool,
    not_applicable_reason: str = "",
    label: str = "value",
    findings: list[Finding] | None = None,
    normalizer: Callable[[str], str] | None = None,
    allow_surrounding_text: bool = False,
) -> FieldResult:
    """Tier 1/2 comparison for a free-text field.

    `allow_surrounding_text` opts a field into accepting the application's value
    carried INSIDE a longer printed statement. Off by default and deliberately so —
    it is right for a producer or a country, whose regulated phrasing wraps the value
    ("Bottled by …", "Distilled in …"), and wrong for a brand name, where the brand
    buried in a longer string is not the same claim as the brand.
    """
    findings = findings or []
    evidence = (
        # image_index is a placeholder, NOT the answer. A comparator is handed one value
        # and cannot know which of up to four photographs it was read off; the merge does,
        # and `api.verify._apply_merge` overwrites this index with the real provenance
        # before the result leaves the pipeline (IMG-8). Read on its own this line is
        # wrong on every multi-image application.
        Evidence(image_index=0, bbox=extracted.bbox)
        if extracted and extracted.bbox
        else None
    )

    if extracted is not None and not extracted.legible:
        return _unreadable(field, expected, label)

    found = extracted.value if extracted else None

    if not found:
        if not required:
            return _not_applicable(field, not_applicable_reason or "Not required.")
        return _missing(field, expected, label)

    if not expected:
        # The label states something the application does not. Surfaced, not passed.
        return FieldResult(
            field=field,
            verdict=Verdict.MISMATCH,
            extracted=found,
            expected=None,
            confidence=extracted.confidence if extracted else 0.0,
            rationale=(
                f"The label states a {label} but the application does not include one."
            ),
            evidence=evidence,
            findings=findings,
        )

    left, right = (normalizer(found), normalizer(expected)) if normalizer else (found, expected)

    if (
        allow_surrounding_text
        and not equal_after_normalization(left, right)
        and contains_after_normalization(left, right)
    ):
        # The label carries the application's value inside a longer statement — "BOTTLED
        # BY …", "DISTILLED IN …". That is how labels are printed, and demanding equality
        # reported a mismatch on every compliant import (found on three real
        # photographs). Acceptable variation rather than Match, because the agent should
        # still see the extra words and decide they are innocuous.
        # Located on the NORMALIZED text, quoted from the ORIGINAL. The normalizer
        # exists to make two spellings compare equal, not to be shown to anyone —
        # `expand_state_abbreviations` rewrites "DISTILLED IN CANADA" to
        # "distilled indiana canada", which is fine to match on and absurd to print.
        context = surrounding_words(found, expected, matched_on=(left, right))
        return FieldResult(
            field=field,
            verdict=Verdict.ACCEPTABLE_VARIATION,
            extracted=found,
            expected=expected,
            confidence=extracted.confidence if extracted else 1.0,
            rationale=(
                f"The label states the {label} within a longer phrase"
                + (f' — it also reads "{context}".' if context else ".")
            ),
            tier=2,
            evidence=evidence,
            findings=findings,
        )

    if equal_after_normalization(left, right):
        variations = classify_variation(left, right)
        if not variations:
            return FieldResult(
                field=field,
                verdict=Verdict.MATCH,
                extracted=found,
                expected=expected,
                confidence=extracted.confidence if extracted else 1.0,
                rationale="The label matches the application.",
                tier=1,
                evidence=evidence,
                findings=findings,
            )
        return FieldResult(
            field=field,
            verdict=Verdict.ACCEPTABLE_VARIATION,
            extracted=found,
            expected=expected,
            confidence=extracted.confidence if extracted else 1.0,
            rationale=variation_note(variations),
            tier=2,
            evidence=evidence,
            findings=findings,
        )

    return FieldResult(
        field=field,
        verdict=Verdict.MISMATCH,
        extracted=found,
        expected=expected,
        confidence=extracted.confidence if extracted else 0.0,
        rationale=f'The label reads "{found}" but the application states "{expected}".',
        evidence=evidence,
        findings=findings,
    )


def compare_brand_name(extracted: ExtractedField | None, expected: str) -> FieldResult:
    return compare_text(
        FieldName.BRAND_NAME, extracted, expected, required=True, label="brand name"
    )


def compare_class_type(extracted: ExtractedField | None, expected: str) -> FieldResult:
    return compare_text(
        FieldName.CLASS_TYPE, extracted, expected, required=True,
        label="class or type designation",
    )


def compare_producer(
    extracted: ExtractedField | None, expected_name: str, expected_address: str
) -> FieldResult:
    """Producer name and address as one field, tolerant of address formatting.

    The brief lists "Name and address of bottler/producer" as a single element, so a
    difference in either is one finding rather than two.
    """
    expected = f"{expected_name}, {expected_address}".strip(", ")
    return compare_text(
        FieldName.PRODUCER,
        extracted,
        expected,
        required=True,
        label="bottler or producer name and address",
        normalizer=expand_state_abbreviations,
        allow_surrounding_text=True,
    )


def compare_country_of_origin(
    extracted: ExtractedField | None, expected: str | None, *, is_import: bool
) -> FieldResult:
    """TC-19 — required only for imports; absent on a domestic label is Not applicable."""
    return compare_text(
        FieldName.COUNTRY_OF_ORIGIN,
        extracted,
        expected,
        required=is_import,
        not_applicable_reason="Country of origin is required only for imported products.",
        label="country of origin",
        allow_surrounding_text=True,
    )


def compare_alcohol_content(
    extracted: ExtractedField | None,
    expected: float | None,
    commodity: Commodity,
    context: com.LabelContext,
) -> FieldResult:
    """Numeric comparison after parsing, with the delta shown on a mismatch (MATCH-8)."""
    required = com.is_required(commodity, FieldName.ALCOHOL_CONTENT, context)
    expected_text = f"{expected:g}%" if expected is not None else None
    evidence = (
        # image_index is a placeholder, NOT the answer. A comparator is handed one value
        # and cannot know which of up to four photographs it was read off; the merge does,
        # and `api.verify._apply_merge` overwrites this index with the real provenance
        # before the result leaves the pipeline (IMG-8). Read on its own this line is
        # wrong on every multi-image application.
        Evidence(image_index=0, bbox=extracted.bbox)
        if extracted and extracted.bbox
        else None
    )

    if extracted is not None and not extracted.legible:
        return _unreadable(FieldName.ALCOHOL_CONTENT, expected_text, "alcohol content")

    raw = extracted.value if extracted else None
    parsed = abv_rules.parse(raw)
    findings = abv_rules.check_internal_consistency(parsed) + abv_rules.check_format(
        raw, commodity
    )

    if parsed.abv is None:
        if not required:
            return _not_applicable(
                FieldName.ALCOHOL_CONTENT,
                com.not_applicable_reason(commodity, FieldName.ALCOHOL_CONTENT, context),
            )
        return _missing(FieldName.ALCOHOL_CONTENT, expected_text, "an alcohol content")

    if expected is None:
        return FieldResult(
            field=FieldName.ALCOHOL_CONTENT,
            verdict=Verdict.MISMATCH,
            extracted=raw,
            expected=None,
            confidence=extracted.confidence if extracted else 0.0,
            rationale=(
                "The label states an alcohol content but the application does not."
            ),
            evidence=evidence,
            findings=findings,
        )

    if abs(parsed.abv - expected) < 0.05:
        return FieldResult(
            field=FieldName.ALCOHOL_CONTENT,
            verdict=Verdict.MATCH,
            extracted=raw,
            expected=expected_text,
            confidence=extracted.confidence if extracted else 1.0,
            rationale="The label matches the application.",
            tier=1,
            evidence=evidence,
            findings=findings,
        )

    delta = parsed.abv - expected
    return FieldResult(
        field=FieldName.ALCOHOL_CONTENT,
        verdict=Verdict.MISMATCH,
        extracted=raw,
        expected=expected_text,
        confidence=extracted.confidence if extracted else 0.0,
        rationale=(
            f"The label states {parsed.abv:g}% but the application states "
            f"{expected:g}% — a difference of {abs(delta):g} percentage "
            f"{'point' if abs(delta) == 1 else 'points'}. "
            + abv_rules.tolerance_context(commodity, expected)
        ),
        evidence=evidence,
        findings=findings,
    )


def compare_net_contents(
    extracted: ExtractedField | None, expected: str, commodity: Commodity
) -> FieldResult:
    """Numeric volume comparison, plus an independent standards-of-fill check (TC-10)."""
    evidence = (
        # image_index is a placeholder, NOT the answer. A comparator is handed one value
        # and cannot know which of up to four photographs it was read off; the merge does,
        # and `api.verify._apply_merge` overwrites this index with the real provenance
        # before the result leaves the pipeline (IMG-8). Read on its own this line is
        # wrong on every multi-image application.
        Evidence(image_index=0, bbox=extracted.bbox)
        if extracted and extracted.bbox
        else None
    )

    if extracted is not None and not extracted.legible:
        return _unreadable(FieldName.NET_CONTENTS, expected, "net contents")

    raw = extracted.value if extracted else None
    if not raw:
        return _missing(FieldName.NET_CONTENTS, expected, "net contents")

    label_volume = fill_rules.parse(raw)
    app_volume = fill_rules.parse(expected)
    # Compliance is judged on what the LABEL says, independent of the comparison.
    findings = fill_rules.check_standards_of_fill(label_volume, commodity)

    if label_volume.ml is None:
        return _missing(FieldName.NET_CONTENTS, expected, "net contents")

    if fill_rules.equal(label_volume, app_volume):
        return FieldResult(
            field=FieldName.NET_CONTENTS,
            verdict=Verdict.MATCH,
            extracted=raw,
            expected=expected,
            confidence=extracted.confidence if extracted else 1.0,
            rationale="The label matches the application.",
            tier=1,
            evidence=evidence,
            findings=findings,
        )

    return FieldResult(
        field=FieldName.NET_CONTENTS,
        verdict=Verdict.MISMATCH,
        extracted=raw,
        expected=expected,
        confidence=extracted.confidence if extracted else 0.0,
        rationale=f'The label reads "{raw}" but the application states "{expected}".',
        evidence=evidence,
        findings=findings,
    )
