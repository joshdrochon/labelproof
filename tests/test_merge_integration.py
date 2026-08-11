"""The seam between the two merges — pinned, because it was a merge conflict once.

Two branches rewrote how several photographs become one label, and both changes had to
survive. `api.pipeline.merge` folds the ordinary readings: per-field provenance, best
confidence where the pictures agree, a refusal to pick where they materially disagree.
`api.rules.warning` folds the government warning's typography across every picture that
carried the statement, so a violation detected on one photograph is not thrown away when
another photograph is the one quoted.

Both are false-pass fixes for defects that actually shipped, and each one is invisible in
the other's tests. This module holds the four scenarios that distinguish a resolution
which kept both from one which quietly kept only one, plus the seam itself: the warning
row is judged once, by one mechanism, and its evidence box and its text come off the same
photograph.

Everything here goes through `verify()` unless the behaviour is unreachable from it —
escalation is, because nothing wires a rereader yet, so that one test enters at
`evaluate_across_images` with sightings built by the pipeline's own helper.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from api import canon
from api.models import (
    Application,
    BoundingBox,
    ExtractedField,
    Extraction,
    FieldName,
    FieldResult,
    Recommendation,
    Verdict,
    VerificationResult,
    WarningTypography,
)
from api.pipeline import merge as merge_images
from api.provider.base import (
    ExtractionRequest,
    ExtractionResponse,
    ImageInput,
    ProviderUsage,
)
from api.rules import typography
from api.rules import warning as warn
from api.verify import verify, warning_sightings

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "golden" / "set.json").read_text())

WARNING = FieldName.GOVERNMENT_WARNING
BRAND = FieldName.BRAND_NAME

CANONICAL = canon.CANONICAL_WARNING

#: A reading with nothing wrong with it: heading bold and capitalised, body not bold, good
#: contrast, the statement the same size as the text around it.
GOOD = WarningTypography(
    header_is_all_caps=True,
    header_is_bold=True,
    body_is_bold=False,
    contrast_ok=True,
    relative_size=1.0,
)

#: The same label, seen by a picture that noticed the paragraph is set in bold — WARN-7.
BOLD_BODY = GOOD.model_copy(update={"body_is_bold": True})

FRONT_BOX = BoundingBox(x0=0.05, y0=0.05, x1=0.40, y1=0.20)
BACK_BOX = BoundingBox(x0=0.10, y0=0.70, x1=0.90, y1=0.95)
BRAND_BOX = BoundingBox(x0=0.20, y0=0.10, x1=0.80, y1=0.30)
PRODUCER_BOX = BoundingBox(x0=0.15, y0=0.40, x1=0.85, y1=0.55)


def application() -> Application:
    """The two-image application the golden set uses for TC-16."""
    entry = next(f for f in GOLDEN["fixtures"] if f["name"] == "tc16_front_back")
    return Application(**entry["application"])


def images() -> list[ImageInput]:
    return [
        ImageInput(index=0, data=b"", role="front"),
        ImageInput(index=1, data=b"", role="back"),
    ]


def label_fields() -> dict[FieldName, ExtractedField]:
    """Every ordinary field, read exactly as the application declares it.

    So that nothing but the thing under test can move the recommendation. A test that
    asserts "not Ready to approve" is worthless if the label was never going to be ready.
    """
    return {
        BRAND: ExtractedField(
            value="OLD TOM DISTILLERY", confidence=0.95, bbox=BRAND_BOX
        ),
        FieldName.CLASS_TYPE: ExtractedField(
            value="Kentucky Straight Bourbon Whiskey", confidence=0.95
        ),
        FieldName.ALCOHOL_CONTENT: ExtractedField(value="45% ALC/VOL", confidence=0.95),
        FieldName.NET_CONTENTS: ExtractedField(value="750 mL", confidence=0.95),
        FieldName.PRODUCER: ExtractedField(
            value="Old Tom Distillery, Bardstown, Kentucky",
            confidence=0.95,
            bbox=PRODUCER_BOX,
        ),
    }


class StubProvider:
    """Hands back exactly the extractions it was given."""

    name = "fake:stub"

    def __init__(self, extractions: list[Extraction]) -> None:
        self.extractions = extractions

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        return ExtractionResponse(
            extractions=list(self.extractions),
            usage=ProviderUsage(model=self.name),
        )


def run(extractions: list[Extraction]) -> VerificationResult:
    return verify(application(), images(), StubProvider(extractions))


def row(result: VerificationResult, field: FieldName) -> FieldResult:
    return next(f for f in result.fields if f.field is field)


def codes(result: FieldResult) -> set[str]:
    return {f.code for f in result.findings}


def asserted(result: FieldResult) -> set[str]:
    """Findings that accuse the label, as opposed to admitting what we could not check."""
    return {
        f.code
        for f in result.findings
        if f.severity in typography.ASSERTED_SEVERITIES
    }


def _two_panels(
    *,
    front_typography: WarningTypography,
    back_typography: WarningTypography,
    front_text: str = CANONICAL,
    back_text: str = CANONICAL,
) -> list[Extraction]:
    """Two photographs of one label, both showing the whole statement.

    The realistic cause is mundane: a wrap-around back label photographed twice, or a flat
    artwork sheet shot from two angles. The words are the same on both; only the reading of
    how they are printed differs.
    """
    return [
        Extraction(
            image_index=0,
            is_label=True,
            fields={
                **label_fields(),
                WARNING: ExtractedField(
                    value=front_text, confidence=0.90, bbox=FRONT_BOX
                ),
            },
            warning_text=front_text,
            warning_typography=front_typography,
        ),
        Extraction(
            image_index=1,
            is_label=True,
            fields={
                **label_fields(),
                WARNING: ExtractedField(
                    value=back_text, confidence=0.95, bbox=BACK_BOX
                ),
            },
            warning_text=back_text,
            warning_typography=back_typography,
        ),
    ]


# --- the baseline, so every "not approved" assertion below means something -------------


def test_the_baseline_two_panel_label_is_ready_to_approve() -> None:
    """Both pictures clean. If this ever stops passing, every test below is vacuous."""
    result = run(_two_panels(front_typography=GOOD, back_typography=GOOD))
    assert row(result, WARNING).verdict is Verdict.MATCH
    assert result.aggregate.recommendation is Recommendation.READY_TO_APPROVE


# --- fix 1: a violation seen on one photograph is a violation (LP-217) ------------------
#
# The confirmed false pass: two photographs of one label, identical text, the bold-body
# violation correctly detected on image 1 — and `match` / `ready_to_approve` returned,
# because the chosen sighting's signals were the only ones consulted and an image-index
# tie-break chose the other one. Flipping the upload order returned `unreadable`. One
# label, two answers, neither of them the right one.


@pytest.mark.tc("TC-16")
@pytest.mark.parametrize("violation_on", [0, 1])
def test_a_bold_body_on_either_picture_is_a_violation(violation_on: int) -> None:
    signals = [GOOD, GOOD]
    signals[violation_on] = BOLD_BODY
    result = run(
        _two_panels(front_typography=signals[0], back_typography=signals[1])
    )
    warning_row = row(result, WARNING)

    assert warning_row.verdict is Verdict.MISMATCH
    assert "warning_body_is_bold" in asserted(warning_row)
    assert result.aggregate.recommendation is Recommendation.RETURN_FOR_CORRECTION


@pytest.mark.tc("TC-16")
def test_the_answer_does_not_depend_on_which_picture_carried_the_violation() -> None:
    """The property the false pass violated. Two photographs, one label, one answer."""
    on_front = run(_two_panels(front_typography=BOLD_BODY, back_typography=GOOD))
    on_back = run(_two_panels(front_typography=GOOD, back_typography=BOLD_BODY))

    assert row(on_front, WARNING).verdict is row(on_back, WARNING).verdict
    assert on_front.aggregate.recommendation is on_back.aggregate.recommendation
    assert codes(row(on_front, WARNING)) == codes(row(on_back, WARNING))


@pytest.mark.tc("TC-16")
def test_a_contested_bright_line_is_neither_passed_nor_asserted() -> None:
    """The other branch's half of the same rule, and it is not the same case.

    `body_is_bold=True` is a *sighting* — a picture reporting it saw bold ink — and a
    picture that did not see it has not rebutted that. `header_is_bold` is the other way
    round: the concerning value is the failure to see the heading in bold, so two pictures
    contradicting each other means one of them is wrong and neither answer may be taken.
    Clearing the label would be a pass on a contested signal; asserting the violation
    would return the application on a reading another photograph contradicts.
    """
    result = run(
        _two_panels(
            front_typography=GOOD.model_copy(update={"header_is_bold": False}),
            back_typography=GOOD,
        )
    )
    warning_row = row(result, WARNING)

    assert warning_row.verdict is not Verdict.MATCH
    assert "warning_header_bold_unverified" in codes(warning_row)
    assert asserted(warning_row) == set(), "a contested reading may not accuse the label"
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


# --- fix 1b: a second look that measures the warning smaller (WARN-5) ------------------
#
# Escalation is not reachable from `verify()` — no adapter implements the protocol yet —
# so this enters one level down, on sightings built by the pipeline's own helper. The
# seam being tested is that `warning_sightings` still produces what
# `evaluate_across_images` needs, including for the escalation path.


class _Rereader:
    """A stub stronger model. Records what it was asked, returns what it was told to."""

    name = "stub"

    def __init__(self, reply: typography.WarningReread) -> None:
        self.reply = reply
        self.requests: list[typography.WarningRereadRequest] = []

    def reread_warning(
        self, request: typography.WarningRereadRequest
    ) -> typography.WarningReread:
        self.requests.append(request)
        return self.reply


@pytest.mark.tc("TC-06")
def test_a_second_reading_that_finds_the_warning_smaller_is_never_a_match() -> None:
    """First pass clean, second look measures the statement at 30% of the body text."""
    stub = _Rereader(
        typography.WarningReread(
            warning_text=CANONICAL,
            typography=GOOD.model_copy(update={"relative_size": 0.3}),
        )
    )
    sightings = warning_sightings(
        _two_panels(front_typography=GOOD, back_typography=GOOD)
    )
    result = warn.evaluate_across_images(sightings, rereader=stub)

    assert stub.requests, "escalation did not fire"
    assert result.verdict is not Verdict.MATCH
    codes_seen = {f.code for f in result.findings}
    assert "warning_less_prominent" in codes_seen
    # The size reaches the verdict twice — through the merged value and through a finding
    # of its own. Deleting either path used to leave a 70%-smaller warning at Match.
    assert "warning_size_disputed" in codes_seen


@pytest.mark.tc("TC-06")
def test_the_smaller_of_two_size_readings_is_the_one_that_counts() -> None:
    """The same asymmetry one level up: two photographs, not two passes.

    A measurement, not a claim, and the only rule that consumes it asks whether the
    statement is too small (WARN-5). Rounding a buried warning up to the roomiest
    photograph of it is the failure the fold exists to prevent.
    """
    result = run(
        _two_panels(
            front_typography=GOOD.model_copy(update={"relative_size": 0.3}),
            back_typography=GOOD,
        )
    )
    warning_row = row(result, WARNING)

    assert warning_row.verdict is not Verdict.MATCH
    assert "warning_less_prominent" in codes(warning_row)
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


# --- fix 2: two pictures that read a field differently (LP-058) ------------------------


def _disagreeing_brand() -> list[Extraction]:
    """One picture reads the brand off the application; the other reads a different one.

    The confident reading is the *wrong* one on purpose. Taking it would be a false
    rejection; taking the other would be Ready to approve on a label nobody established
    the contents of. Neither is available.
    """
    extractions = _two_panels(front_typography=GOOD, back_typography=GOOD)
    extractions[0].fields[BRAND] = ExtractedField(
        value="OLDE TOWNE DISTILLERY", confidence=0.99
    )
    extractions[1].fields[BRAND] = ExtractedField(
        value="OLD TOM DISTILLERY", confidence=0.60
    )
    return extractions


@pytest.mark.tc("TC-16")
def test_two_pictures_reading_different_brands_establish_nothing() -> None:
    result = run(_disagreeing_brand())
    brand = row(result, BRAND)

    assert brand.verdict is Verdict.UNREADABLE
    assert brand.extracted is None, "the more confident reading must not be adopted"
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


@pytest.mark.tc("TC-16")
def test_a_conflicted_field_is_neither_approved_nor_returned() -> None:
    """Flag, never block: a reading we do not trust is no basis for a rejection either."""
    result = run(_disagreeing_brand())
    assert result.aggregate.recommendation is not Recommendation.READY_TO_APPROVE
    assert result.aggregate.recommendation is not Recommendation.RETURN_FOR_CORRECTION


@pytest.mark.tc("TC-16")
def test_the_agent_is_shown_both_readings_and_which_picture_each_came_from() -> None:
    brand = row(run(_disagreeing_brand()), BRAND)
    assert merge_images.CONFLICT_CODE in codes(brand)
    assert "OLD TOM DISTILLERY" in brand.rationale
    assert "OLDE TOWNE DISTILLERY" in brand.rationale
    assert "picture 1" in brand.rationale and "picture 2" in brand.rationale


@pytest.mark.tc("TC-16")
def test_two_pictures_that_merely_style_the_brand_differently_are_not_in_conflict() -> None:
    """The other half of the judgement: not every difference is a disagreement.

    Materiality is Tier-1 normalization, so `Old Tom Distillery` and `OLD TOM DISTILLERY`
    are the same answer. The pictures agree, best confidence wins, and the row is judged
    against the application on its merits — here an acceptable variation (TC-02), not the
    "we could not establish it" a real conflict produces.
    """
    extractions = _two_panels(front_typography=GOOD, back_typography=GOOD)
    extractions[0].fields[BRAND] = ExtractedField(
        value="Old Tom Distillery", confidence=0.99, bbox=BRAND_BOX
    )
    brand = row(run(extractions), BRAND)

    assert merge_images.CONFLICT_CODE not in codes(brand)
    assert brand.extracted == "Old Tom Distillery", "best confidence wins where they agree"
    assert brand.verdict is Verdict.ACCEPTABLE_VARIATION


# --- fix 3: a picture of something else does not answer for the label (TC-15) ----------


@pytest.mark.tc("TC-15")
def test_a_warning_on_a_non_label_image_does_not_answer_for_the_label() -> None:
    """The quiet version of TC-15, and the worst outcome the product can produce.

    The artwork carries no warning. A marketing one-sheet in the same upload carries a
    perfect one, and the extractor flagged it `is_label=False`. The response looks
    completely normal, which is what makes it dangerous.
    """
    artwork, stray = _two_panels(front_typography=GOOD, back_typography=GOOD)
    artwork.fields.pop(WARNING)
    artwork.warning_text = None
    artwork.warning_typography = WarningTypography()
    stray.is_label = False

    result = run([artwork, stray])
    warning_row = row(result, WARNING)

    assert warning_row.verdict is Verdict.MISSING
    assert warning_row.extracted is None
    assert result.aggregate.recommendation is not Recommendation.READY_TO_APPROVE
    assert result.aggregate.driving_field is WARNING


@pytest.mark.tc("TC-15")
def test_a_statement_nothing_read_is_not_a_statement() -> None:
    """LP-067's three states, on the field where collapsing two of them is worst.

    The provider reported `warning_text` and omitted the `government_warning` field. An
    omitted field is "I looked, it is not on this image"; the loose text is a second copy
    of a statement no reading actually established. Taking it would satisfy the check on
    the strength of a channel with no confidence, no region, and no legibility judgement
    attached — a pass on the one field that must fail closed (WARN-6).
    """
    artwork, _ = _two_panels(front_typography=GOOD, back_typography=GOOD)
    artwork.fields.pop(WARNING)

    result = run([artwork])
    assert artwork.warning_text == CANONICAL, "the loose copy is still there"
    assert row(result, WARNING).verdict is Verdict.MISSING
    assert result.aggregate.recommendation is not Recommendation.READY_TO_APPROVE


def test_the_public_merge_helper_refuses_a_conflict_too() -> None:
    """`merge_extractions` is public and `verify()` is not its only possible caller.

    It has to carry both halves: the conflict refusal from one branch and the typography
    fold from the other. A caller that got the confident reading of a contested field, or
    the chosen sighting's signals instead of the folded ones, would inherit a false pass
    each branch already removed.
    """
    from api.verify import merge_extractions

    # The violation is on the picture that does *not* win the sighting, so returning the
    # chosen sighting's signals — the pre-fix behaviour — cannot pass by accident.
    extractions = _disagreeing_brand()
    extractions[0].warning_typography = BOLD_BODY

    merged, warning_image, signals, provenance = merge_extractions(extractions)

    assert merged[BRAND].value is None and merged[BRAND].legible is False
    assert signals.body_is_bold is True
    assert warning_image == 1
    assert provenance[FieldName.PRODUCER] in (0, 1)


@pytest.mark.tc("TC-15")
def test_a_non_label_picture_supplies_no_ordinary_readings_either() -> None:
    """The same rule, one field over. A brand read off the wrong picture is the same bug."""
    artwork, stray = _two_panels(front_typography=GOOD, back_typography=GOOD)
    artwork.fields.pop(BRAND)
    stray.is_label = False
    stray.fields[BRAND] = ExtractedField(value="OLD TOM DISTILLERY", confidence=0.99)

    assert row(run([artwork, stray]), BRAND).verdict is Verdict.MISSING


# --- the seam: the warning row is judged once, by one mechanism -------------------------


@pytest.mark.tc("TC-16")
def test_the_warning_row_is_not_merged_twice() -> None:
    """Two panels carrying materially different statements.

    Both merges have something to say about this, and only one of them may say it. The
    sighting path demotes the row off Match and names the disagreement; the generic merge
    is skipped for this field, so the row does not report the same fact twice under two
    codes with two rationales.
    """
    result = run(
        _two_panels(
            front_typography=GOOD,
            back_typography=GOOD,
            front_text=CANONICAL.replace("GOVERNMENT WARNING", "Government Warning"),
        )
    )
    warning_row = row(result, WARNING)

    assert warning_row.verdict is Verdict.UNREADABLE
    assert "warning_differs_between_images" in codes(warning_row)
    assert merge_images.CONFLICT_CODE not in codes(warning_row)
    assert result.aggregate.recommendation is Recommendation.NEEDS_REVIEW


@pytest.mark.tc("TC-16")
def test_the_warning_box_and_the_quoted_text_come_off_the_same_picture() -> None:
    """IMG-8 on the row the PRD most wants outlined.

    The front carries a decorative fragment and the back carries the whole statement, so
    the reading judged is the back's. The rectangle has to be the back's too — an overlay
    filters by image, so image 0's box on image 1's photograph highlights nothing at all.
    """
    fragment = "GOVERNMENT WARNING: (1) According to the Surgeon General"
    result = run(
        _two_panels(
            front_typography=GOOD, back_typography=GOOD, front_text=fragment
        )
    )
    warning_row = row(result, WARNING)

    assert warning_row.extracted == CANONICAL
    assert warning_row.evidence is not None
    assert warning_row.evidence.image_index == 1
    assert warning_row.evidence.bbox == BACK_BOX


@pytest.mark.tc("TC-16")
def test_the_ordinary_rows_still_carry_the_picture_their_value_came_from() -> None:
    """The other merge's job, and the reason it runs at all. Front brand, back producer."""
    front, back = _two_panels(front_typography=GOOD, back_typography=GOOD)
    front.fields.pop(FieldName.PRODUCER)
    back.fields.pop(BRAND)

    result = run([front, back])
    brand_evidence = row(result, BRAND).evidence
    producer_evidence = row(result, FieldName.PRODUCER).evidence

    assert brand_evidence is not None and brand_evidence.image_index == 0
    assert producer_evidence is not None and producer_evidence.image_index == 1


@pytest.mark.tc("TC-16")
def test_the_warning_row_still_diffs_against_the_regulation_itself() -> None:
    """WARN-8. The left-hand side of the diff is the statute's words, not a description."""
    reworded = CANONICAL.replace("birth defects", "health risks")
    result = run(
        _two_panels(
            front_typography=GOOD,
            back_typography=GOOD,
            front_text=reworded,
            back_text=reworded,
        )
    )
    warning_row = row(result, WARNING)

    assert warning_row.expected == CANONICAL
    assert warning_row.extracted == reworded
    assert warning_row.verdict is Verdict.MISMATCH


@pytest.mark.tc("TC-16")
@pytest.mark.parametrize(
    "case",
    ["clean", "bold_body", "contested_header", "conflicted_brand", "reworded_front"],
)
def test_the_upload_order_never_changes_the_answer(case: str) -> None:
    """One property over every scenario in this module, both merges included.

    Images are extracted concurrently and a provider may return them in whatever order its
    thread pool finished in. An ordering bug in a merge is invisible in any hand-picked
    example, which is why this is asserted across the set rather than once.
    """
    scenarios: dict[str, list[Extraction]] = {
        "clean": _two_panels(front_typography=GOOD, back_typography=GOOD),
        "bold_body": _two_panels(front_typography=GOOD, back_typography=BOLD_BODY),
        "contested_header": _two_panels(
            front_typography=GOOD.model_copy(update={"header_is_bold": False}),
            back_typography=GOOD,
        ),
        "conflicted_brand": _disagreeing_brand(),
        "reworded_front": _two_panels(
            front_typography=GOOD,
            back_typography=GOOD,
            front_text=CANONICAL.replace("birth defects", "health risks"),
        ),
    }
    extractions = scenarios[case]

    forward = run(extractions)
    backward = run(list(reversed(extractions)))

    assert _answers(forward) == _answers(backward)
    assert forward.aggregate.recommendation is backward.aggregate.recommendation


def _answers(result: VerificationResult) -> dict[FieldName, Any]:
    """Everything a reviewer sees per row, other than free prose."""
    return {
        f.field: (
            f.verdict,
            f.extracted,
            f.evidence.image_index if f.evidence else None,
            f.evidence.bbox if f.evidence else None,
            frozenset(x.code for x in f.findings),
        )
        for f in result.fields
    }
