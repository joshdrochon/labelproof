"""Multi-image merge — LP-058, IMG-8, TC-16.

The three states an image can report about a field are the whole subject here, and the
tests are organised around keeping them apart:

- omitted from the extraction  = "I looked, it is not on this image"
- `legible=False`              = "it is here and I failed to read it"
- a value with a confidence    = "I read it"

Collapsing the first two produces Missing where Unreadable is true, or the reverse
(LP-067). Resolving a genuine disagreement by confidence produces a verdict nobody can
see is wrong, which is worse than either.
"""

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api.models import (
    BoundingBox,
    ExtractedField,
    Extraction,
    FieldName,
    WarningTypography,
)
from api.pipeline.merge import (
    CONFLICT_CODE,
    Conflict,
    Reading,
    ReadingKind,
    conflict_finding,
    conflict_rationale,
    materiality_key,
    merge,
    picture_number,
    readings_for,
)

WARNING = FieldName.GOVERNMENT_WARNING
BRAND = FieldName.BRAND_NAME
BOX = BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.2)


# --- builders ---------------------------------------------------------------------------


def read(value: str, confidence: float = 0.9, bbox: BoundingBox | None = None) -> ExtractedField:
    """The field is on this image and was read."""
    return ExtractedField(value=value, confidence=confidence, legible=True, bbox=bbox)


def illegible(bbox: BoundingBox | None = None) -> ExtractedField:
    """The field IS on this image but could not be read. Not the same as absent."""
    return ExtractedField(value=None, confidence=0.0, legible=False, bbox=bbox)


def blank() -> ExtractedField:
    """Present, judged legible, and yet no value — a provider reporting an empty string."""
    return ExtractedField(value=None, confidence=0.0, legible=True)


def image(
    index: int,
    fields: dict[FieldName, ExtractedField],
    *,
    warning_text: str | None = None,
    typography: WarningTypography | None = None,
) -> Extraction:
    return Extraction(
        image_index=index,
        fields=fields,
        warning_text=warning_text,
        warning_typography=typography or WarningTypography(),
    )


# --- the three states stay three --------------------------------------------------------


def test_a_field_no_image_carries_is_absent_from_the_merge() -> None:
    """Absent everywhere. The caller draws Missing; the merge does not pretend to know."""
    label = merge([image(0, {BRAND: read("OLD TOM")}), image(1, {})])
    assert WARNING not in label.fields
    assert label.provenance(WARNING) is None


def test_a_field_unreadable_on_every_image_is_unreadable_not_missing() -> None:
    label = merge([image(0, {WARNING: illegible()}), image(1, {WARNING: illegible()})])
    merged = label.fields[WARNING]
    assert merged.value is None
    assert merged.legible is False


def test_omission_never_becomes_unreadable() -> None:
    """The distinction LP-067 turns on, asserted in the direction that fabricates a defect.

    One image omits the warning because it is the front. That is not a failure to read it,
    and a merge that treated it as one would report Unreadable on a label whose back image
    shows the statement perfectly.
    """
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM")}),
            image(1, {WARNING: read("GOVERNMENT WARNING: ...")}),
        ]
    )
    assert label.fields[WARNING].legible is True
    assert label.fields[WARNING].value == "GOVERNMENT WARNING: ..."


def test_an_omitting_image_does_not_vote() -> None:
    assert readings_for([image(0, {}), image(1, {BRAND: read("X")})], BRAND) == (
        Reading(image_index=1, value="X", confidence=0.9, legible=True),
    )


def test_blank_ranks_below_illegible() -> None:
    """A blank reading must not turn an Unreadable field into a Missing one.

    `api.rules.compare` puts Unreadable above Missing for the same reason: reporting "it
    is not there" when the truth is "we could not read it" is a false finding on a
    compliant label.
    """
    label = merge([image(0, {BRAND: blank()}), image(1, {BRAND: illegible()})])
    assert label.fields[BRAND].legible is False
    assert label.fields[BRAND].image_index == 1


def test_reading_kinds_rank_read_highest() -> None:
    assert ReadingKind.READ > ReadingKind.ILLEGIBLE > ReadingKind.BLANK


# --- best confidence wins, when they agree ----------------------------------------------


def test_best_confidence_wins_when_both_images_agree() -> None:
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM DISTILLERY", 0.60)}),
            image(1, {BRAND: read("OLD TOM DISTILLERY", 0.95)}),
        ]
    )
    merged = label.fields[BRAND]
    assert merged.value == "OLD TOM DISTILLERY"
    assert merged.confidence == 0.95
    assert merged.image_index == 1
    assert merged.conflict is None


def test_a_tie_on_confidence_goes_to_the_earliest_picture() -> None:
    """Ties must resolve the same way every run, or the same application verifies twice.

    Earliest rather than latest is arbitrary; being pinned is not.
    """
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM DISTILLERY", 0.9)}),
            image(1, {BRAND: read("Old Tom Distillery", 0.9)}),
        ]
    )
    assert label.fields[BRAND].image_index == 0
    assert label.fields[BRAND].value == "OLD TOM DISTILLERY"


def test_agreement_is_material_not_literal() -> None:
    """TC-02's difference across two photographs is not a disagreement about the label.

    `STONE'S THROW` and `Stone's Throw` are the same brand — that is what Tier 1
    normalization is for, and the merge reuses it rather than inventing a second answer
    to the same question.
    """
    label = merge(
        [
            image(0, {BRAND: read("STONE’S THROW", 0.70)}),
            image(1, {BRAND: read("Stone's Throw", 0.95)}),
        ]
    )
    assert label.fields[BRAND].conflict is None
    assert label.fields[BRAND].value == "Stone's Throw"


def test_a_reading_beats_an_unreadable_and_is_not_a_conflict() -> None:
    """TC-12 across two images: glare on one does not make the field unreadable."""
    label = merge(
        [
            image(0, {WARNING: illegible()}),
            image(1, {WARNING: read("GOVERNMENT WARNING: ...", 0.5)}),
        ]
    )
    merged = label.fields[WARNING]
    assert merged.value == "GOVERNMENT WARNING: ..."
    assert merged.conflict is None
    assert merged.image_index == 1


def test_a_low_confidence_reading_still_beats_an_unreadable() -> None:
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM", 0.05)}),
            image(1, {BRAND: illegible()}),
        ]
    )
    assert label.fields[BRAND].value == "OLD TOM"


# --- conflicts --------------------------------------------------------------------------


def test_two_different_readings_are_a_conflict() -> None:
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM DISTILLERY", 0.55)}),
            image(1, {BRAND: read("OLDE TOWNE DISTILLERY", 0.95)}),
        ]
    )
    merged = label.fields[BRAND]
    assert merged.conflict is not None
    assert merged.conflict.values == ("OLD TOM DISTILLERY", "OLDE TOWNE DISTILLERY")


def test_a_conflict_is_not_resolved_by_confidence() -> None:
    """The heart of LP-058. The 0.99 reading does not quietly become the answer."""
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM DISTILLERY", 0.10)}),
            image(1, {BRAND: read("OLDE TOWNE DISTILLERY", 0.99)}),
        ]
    )
    merged = label.fields[BRAND]
    assert merged.value is None
    assert merged.confidence == 0.0


def test_a_conflicted_field_fails_closed_for_a_caller_that_ignores_the_conflict() -> None:
    """Belt and braces: the merged field alone routes to Unreadable, not to a match.

    A consumer that never looks at `.conflict` still cannot pass a contradictory field,
    because what it receives says "not legible" rather than a value.
    """
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM", 0.9)}),
            image(1, {BRAND: read("NEW TOM", 0.9)}),
        ]
    )
    extracted = label.extracted()[BRAND]
    assert extracted.legible is False
    assert extracted.value is None


def test_both_readings_are_kept_for_display() -> None:
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM", 0.9)}),
            image(1, {BRAND: read("NEW TOM", 0.8)}),
        ]
    )
    merged = label.fields[BRAND]
    assert [r.image_index for r in merged.readings] == [0, 1]
    assert merged.conflict is not None
    assert [r.image_index for r in merged.conflict.readings] == [0, 1]


def test_agreeing_majority_still_conflicts_with_one_dissenter() -> None:
    """Two against one is not a vote. Three photographs, two answers, no winner."""
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM", 0.9)}),
            image(1, {BRAND: read("OLD TOM", 0.9)}),
            image(2, {BRAND: read("NEW TOM", 0.4)}),
        ]
    )
    merged = label.fields[BRAND]
    assert merged.conflict is not None
    assert merged.conflict.values == ("OLD TOM", "NEW TOM")


def test_conflict_representatives_are_the_most_confident_of_each_answer() -> None:
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM", 0.30)}),
            image(1, {BRAND: read("OLD TOM", 0.80)}),
            image(2, {BRAND: read("NEW TOM", 0.60)}),
        ]
    )
    conflict = label.fields[BRAND].conflict
    assert conflict is not None
    assert [(r.image_index, r.confidence) for r in conflict.readings] == [(1, 0.8), (2, 0.6)]


def test_an_unreadable_image_does_not_join_a_conflict() -> None:
    """Only readings that actually read something can disagree about what it says."""
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM", 0.9)}),
            image(1, {BRAND: illegible()}),
            image(2, {BRAND: read("OLD TOM", 0.5)}),
        ]
    )
    assert label.fields[BRAND].conflict is None
    assert label.fields[BRAND].value == "OLD TOM"


def test_the_conflict_evidence_box_belongs_to_the_picture_it_names() -> None:
    other = BoundingBox(x0=0.2, y0=0.5, x1=0.8, y1=0.6)
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM", 0.4, bbox=BOX)}),
            image(1, {BRAND: read("NEW TOM", 0.9, bbox=other)}),
        ]
    )
    merged = label.fields[BRAND]
    assert (merged.image_index, merged.bbox) == (0, BOX)


# --- the warning gets no leniency (WARN-6) ----------------------------------------------

CANONICAL = "GOVERNMENT WARNING: According to the Surgeon General, women should not drink"
TITLE_CASE = "Government Warning: According to the Surgeon General, women should not drink"


def test_header_case_is_a_conflict_on_the_warning() -> None:
    """Jenny's catch must survive the merge.

    Tier 1 normalization casefolds, so on any other field these two readings are the same
    text. On the warning, case *is* the regulation (WARN-3) — one of these photographs is
    showing a violation and the other is not, and preferring the more confident of them
    would decide a compliance question by a legibility score.
    """
    label = merge(
        [
            image(0, {WARNING: read(TITLE_CASE, 0.60)}),
            image(1, {WARNING: read(CANONICAL, 0.99)}),
        ]
    )
    assert label.fields[WARNING].conflict is not None
    assert label.fields[WARNING].value is None


def test_the_same_case_difference_is_not_a_conflict_on_a_brand_name() -> None:
    """The complement, so the strictness is shown to be specific to the warning."""
    label = merge(
        [
            image(0, {BRAND: read("Old Tom", 0.60)}),
            image(1, {BRAND: read("OLD TOM", 0.99)}),
        ]
    )
    assert label.fields[BRAND].conflict is None


def test_line_wrapping_in_the_warning_is_not_a_conflict() -> None:
    """Layout is not the regulation. Two photographs wrapping differently agree."""
    label = merge(
        [
            image(0, {WARNING: read(CANONICAL, 0.6)}),
            image(1, {WARNING: read(CANONICAL.replace(" ", "\n  ", 2), 0.9)}),
        ]
    )
    assert label.fields[WARNING].conflict is None


def test_materiality_key_is_stricter_for_the_warning_than_for_other_fields() -> None:
    assert materiality_key(BRAND, "Old Tom") == materiality_key(BRAND, "OLD TOM")
    assert materiality_key(WARNING, TITLE_CASE) != materiality_key(WARNING, CANONICAL)


# --- warning text and typography travel together ----------------------------------------


def test_typography_comes_from_the_image_the_statement_was_read_on() -> None:
    """Pairing one picture's text with another picture's bold judgement is a fabrication."""
    front = WarningTypography(header_is_bold=False, body_is_bold=True)
    back = WarningTypography(header_is_bold=True, body_is_bold=False)
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM")}, warning_text=None, typography=front),
            image(1, {WARNING: read(CANONICAL, 0.95)}, warning_text=CANONICAL, typography=back),
        ]
    )
    assert label.warning_image_index == 1
    assert label.warning_typography == back


def test_a_conflicted_warning_drops_its_typography() -> None:
    label = merge(
        [
            image(0, {WARNING: read(TITLE_CASE, 0.9)},
                  warning_text=TITLE_CASE,
                  typography=WarningTypography(header_is_bold=True)),
            image(1, {WARNING: read(CANONICAL, 0.9)},
                  warning_text=CANONICAL,
                  typography=WarningTypography(header_is_bold=False)),
        ]
    )
    assert label.warning_text is None
    assert label.warning_typography == WarningTypography()


def test_an_unreadable_warning_drops_its_typography() -> None:
    label = merge(
        [image(0, {WARNING: illegible()}, typography=WarningTypography(header_is_bold=True))]
    )
    assert label.warning_typography == WarningTypography()


def test_warning_text_survives_a_provider_that_omits_the_field() -> None:
    """Defensive: a hand-written recorded fixture can supply the text and not the field.

    Dropping the text would manufacture a Missing verdict on a label that has a warning.
    """
    label = merge([image(0, {}, warning_text=CANONICAL)])
    assert label.warning_text == CANONICAL
    assert label.warning_image_index == 0


# --- provenance -------------------------------------------------------------------------


@pytest.mark.tc("TC-16")
def test_provenance_is_per_field_not_per_label() -> None:
    """The front/back case. Brand and warning legitimately come from different pictures."""
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM DISTILLERY")}),
            image(1, {WARNING: read(CANONICAL)}),
        ]
    )
    assert label.provenance(BRAND) == 0
    assert label.provenance(WARNING) == 1


def test_provenance_follows_the_winning_reading() -> None:
    label = merge(
        [
            image(0, {BRAND: read("OLD TOM", 0.4, bbox=BOX)}),
            image(1, {BRAND: read("OLD TOM", 0.9, bbox=None)}),
        ]
    )
    assert label.fields[BRAND].image_index == 1
    assert label.fields[BRAND].bbox is None


def test_picture_numbers_are_what_the_agent_sees() -> None:
    """The API is zero-based; the screen reads "Label picture 1"."""
    assert picture_number(0) == 1


def test_the_conflict_rationale_names_both_pictures_and_both_readings() -> None:
    conflict = Conflict(
        field=BRAND,
        readings=(
            Reading(image_index=0, value="OLD TOM", confidence=0.4),
            Reading(image_index=1, value="NEW TOM", confidence=0.9),
        ),
    )
    text = conflict_rationale(conflict)
    assert 'picture 1 reads "OLD TOM"' in text
    assert 'picture 2 reads "NEW TOM"' in text
    assert "has not been checked" in text
    assert conflict_finding(conflict).code == CONFLICT_CODE


# --- degenerate inputs ------------------------------------------------------------------


def test_no_images_merges_to_nothing() -> None:
    label = merge([])
    assert label.fields == {}
    assert label.warning_text is None


def test_one_image_is_the_ordinary_case() -> None:
    label = merge([image(0, {BRAND: read("OLD TOM", 0.9, bbox=BOX)})])
    assert label.fields[BRAND].value == "OLD TOM"
    assert label.fields[BRAND].image_index == 0
    assert label.fields[BRAND].bbox == BOX


# --- properties -------------------------------------------------------------------------

#: A tiny value alphabet, chosen so collisions and near-collisions are common: two
#: normalization-equal spellings, one genuinely different value, and None.
_VALUES = ["OLD TOM", "Old Tom", "NEW TOM"]


@st.composite
def _extractions(draw: st.DrawFn) -> list[Extraction]:
    """Random front/back-ish extractions over three fields and four images."""
    names = [BRAND, FieldName.NET_CONTENTS, WARNING]
    count = draw(st.integers(min_value=0, max_value=4))
    out: list[Extraction] = []
    for index in range(count):
        fields: dict[FieldName, ExtractedField] = {}
        for name in names:
            state = draw(st.sampled_from(["absent", "illegible", "blank", "read"]))
            if state == "absent":
                continue
            if state == "illegible":
                fields[name] = illegible()
            elif state == "blank":
                fields[name] = blank()
            else:
                fields[name] = read(
                    draw(st.sampled_from(_VALUES)),
                    draw(st.sampled_from([0.1, 0.5, 0.9])),
                )
        text = draw(st.sampled_from([None, "GOVERNMENT WARNING: x"]))
        out.append(image(index, fields, warning_text=text))
    return out


@settings(max_examples=300, deadline=None)
@given(_extractions(), st.integers(min_value=0, max_value=2**32))
def test_merge_order_does_not_change_the_outcome(
    extractions: list[Extraction], seed: int
) -> None:
    """The property a hand-picked example set hides.

    Images arrive from a thread pool. If the answer depended on which call returned first,
    the same application would verify differently on two identical runs — and the bug
    would surface as a flake nobody could reproduce.
    """
    shuffled = list(extractions)
    random.Random(seed).shuffle(shuffled)
    assert merge(shuffled) == merge(extractions)


@settings(max_examples=200, deadline=None)
@given(_extractions())
def test_merge_never_invents_a_value(extractions: list[Extraction]) -> None:
    """LP-067 at the merge boundary: every value out came from some image."""
    label = merge(extractions)
    for name, merged in label.fields.items():
        if merged.value is None:
            continue
        assert merged.value in {
            r.value for r in readings_for(extractions, name) if r.value is not None
        }


@settings(max_examples=200, deadline=None)
@given(_extractions())
def test_provenance_points_at_an_image_that_reported_the_field(
    extractions: list[Extraction],
) -> None:
    """An evidence box on a picture that never saw the field is worse than no box."""
    label = merge(extractions)
    for name, merged in label.fields.items():
        reported = {r.image_index for r in readings_for(extractions, name)}
        assert merged.image_index in reported
        if merged.value is not None:
            source = next(e for e in extractions if e.image_index == merged.image_index)
            assert source.fields[name].value == merged.value


@settings(max_examples=200, deadline=None)
@given(_extractions())
def test_a_field_absent_from_every_image_is_absent_from_the_merge(
    extractions: list[Extraction],
) -> None:
    label = merge(extractions)
    for name in FieldName:
        assert (name in label.fields) == bool(readings_for(extractions, name))


@settings(max_examples=200, deadline=None)
@given(_extractions())
def test_a_value_survives_only_when_every_reading_agrees(
    extractions: list[Extraction],
) -> None:
    """Restated as an invariant: no value out of a field two images read differently."""
    label = merge(extractions)
    for name, merged in label.fields.items():
        answers = {
            materiality_key(name, r.value)
            for r in readings_for(extractions, name)
            if r.value is not None
        }
        if len(answers) > 1:
            assert merged.value is None
            assert merged.conflict is not None
