"""Multi-image merge — several photographs, one label (LP-058, IMG-8, TC-16).

A label routinely spans two images: the brand on the front, the government warning on the
back. Every image is extracted independently, and this module folds those readings into
one view before any comparison happens. Declaring the warning Missing without searching
the back image would be a false finding on a compliant label.

**Three states, and they must stay three.** The extractor distinguishes them and so does
this module (LP-067):

| What the image said | How it arrives | What it merges to |
|---|---|---|
| It is here and I read it | a value with a confidence | a reading that can win |
| It is not on this image | the key is **omitted** entirely | nothing — it does not vote |
| It is here and I failed | `value=None, legible=False` | loses to any real reading |

Collapsing the last two produces Missing where Unreadable is true, or the reverse. One is
a false rejection of a compliant label; the other is a compliant-looking verdict on a
label nobody actually read. Both are the kind of invisible wrong answer this product
cannot afford.

**Conflicts are not resolved by confidence.** When two images read the same field and
give materially different answers, the merge refuses to pick. Silently preferring the more
confident of two contradictory readings would bury the one fact an agent most needs to
know — that we do not actually know what the label says. A conflicted field merges to
"could not be established", carries both readings, and routes to Needs review. It never
routes to Ready to approve and it never routes to Return for correction, because a reading
we do not trust is no basis for rejecting an application either.

**Provenance is per field, not per label.** Each winning value records the image it came
from, so the evidence overlay points at the right photograph. On a front/back application
the brand name and the warning legitimately come from different pictures.

**Order does not matter.** Nothing here depends on the order the extractions arrive in;
every tie is broken by image index. Asserted directly by a property test, because an
ordering bug in a merge is invisible in any hand-picked example set.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import IntEnum

from api.models import (
    BoundingBox,
    ExtractedField,
    Extraction,
    FieldName,
    Finding,
    WarningTypography,
)
from api.rules.normalize import normalize
from api.rules.warning import collapse_layout_whitespace

#: Human-readable field names for the conflict message. Written for an agent reading a
#: checklist, not for an engineer reading a log (UX-6).
_FIELD_LABELS: dict[FieldName, str] = {
    FieldName.BRAND_NAME: "brand name",
    FieldName.CLASS_TYPE: "class or type designation",
    FieldName.ALCOHOL_CONTENT: "alcohol content",
    FieldName.NET_CONTENTS: "net contents",
    FieldName.PRODUCER: "bottler or producer name and address",
    FieldName.COUNTRY_OF_ORIGIN: "country of origin",
    FieldName.GOVERNMENT_WARNING: "government warning statement",
}

#: The finding code a conflicted field raises. Stable — the eval harness and the UI key
#: off it.
CONFLICT_CODE = "images_disagree"


def picture_number(image_index: int) -> int:
    """The number an agent sees on screen for a zero-based image index.

    The API is zero-based and the UI reads "Label picture 1". Prose produced here is read
    next to the pictures, so it counts the way the pictures are labelled.
    """
    return image_index + 1


class ReadingKind(IntEnum):
    """What one image had to say about one field. Higher beats lower.

    `BLANK` is the odd one: present on the image, judged legible, and yet no value. The
    extractor should not produce it, and a provider that reports an empty string does. It
    ranks below `ILLEGIBLE` because "I could not read it" is the more cautious of the two
    claims, which is the same precedence `api.rules.compare` applies — Unreadable outranks
    Missing.
    """

    BLANK = 0
    ILLEGIBLE = 1
    READ = 2


@dataclass(frozen=True)
class Reading:
    """One image's account of one field."""

    image_index: int
    value: str | None = None
    confidence: float = 0.0
    legible: bool = True
    bbox: BoundingBox | None = None

    @property
    def kind(self) -> ReadingKind:
        if not self.legible:
            return ReadingKind.ILLEGIBLE
        return ReadingKind.READ if self.value else ReadingKind.BLANK

    def as_extracted(self) -> ExtractedField:
        return ExtractedField(
            value=self.value,
            confidence=self.confidence,
            legible=self.legible,
            bbox=self.bbox,
        )


@dataclass(frozen=True)
class Conflict:
    """Two or more images that read the same field and did not agree.

    `readings` holds one representative per distinct answer — the most confident reading
    of each — in image order, so the UI can show both sides without repeating a value that
    three images agreed on.
    """

    field: FieldName
    readings: tuple[Reading, ...]

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(r.value for r in self.readings if r.value is not None)


@dataclass(frozen=True)
class MergedField:
    """One field, as established across every image, with the picture it came from."""

    field: FieldName
    value: str | None
    confidence: float
    legible: bool
    bbox: BoundingBox | None
    image_index: int
    readings: tuple[Reading, ...]
    conflict: Conflict | None = None

    def as_extracted(self) -> ExtractedField:
        """The shape `api.rules.compare` consumes. Provenance rides alongside, not inside."""
        return ExtractedField(
            value=self.value,
            confidence=self.confidence,
            legible=self.legible,
            bbox=self.bbox,
        )


@dataclass(frozen=True)
class MergedLabel:
    """Everything read off the label, once every image has had its say."""

    fields: dict[FieldName, MergedField]
    warning_text: str | None = None
    warning_image_index: int | None = None
    warning_typography: WarningTypography = dataclass_field(
        default_factory=WarningTypography
    )

    @property
    def conflicts(self) -> dict[FieldName, Conflict]:
        return {
            name: field.conflict
            for name, field in self.fields.items()
            if field.conflict is not None
        }

    def extracted(self) -> dict[FieldName, ExtractedField]:
        """The merged view in the shape the rules engine takes."""
        return {name: field.as_extracted() for name, field in self.fields.items()}

    def provenance(self, name: FieldName) -> int | None:
        """Which image the value for `name` came from, or None if no image had it."""
        field = self.fields.get(name)
        return field.image_index if field else None


# --------------------------------------------------------------------------------------
# Materiality — what counts as two images disagreeing
# --------------------------------------------------------------------------------------


def materiality_key(name: FieldName, value: str) -> str:
    """The form two readings must share to count as the same answer.

    For ordinary fields this is Tier-1 normalization (`api.rules.normalize`), which is
    what "materially different" already means everywhere else in the system —
    `STONE'S THROW` and `Stone's Throw` are the same brand, so two images reporting one
    each are not in conflict.

    The government warning is the exception, and it is stricter rather than looser.
    Normalization casefolds, and case is the regulation there: a title-case
    `Government Warning:` is precisely the violation WARN-3 exists to catch. Two images
    disagreeing about the header's case is a genuine disagreement about whether this label
    complies, so warning readings must match on everything except layout whitespace.
    Nothing about the warning gets resolved by preferring the more confident answer
    (WARN-6, fail closed).

    Using a key rather than a pairwise predicate is deliberate: equality of keys is an
    equivalence relation, so grouping readings by it cannot depend on the order they are
    compared in.
    """
    if name is FieldName.GOVERNMENT_WARNING:
        return collapse_layout_whitespace(value)
    return normalize(value)


def _best(readings: list[Reading]) -> Reading:
    """Highest confidence wins; ties go to the earliest image so the result is stable."""
    return min(readings, key=lambda r: (-r.confidence, r.image_index))


def _group_by_answer(name: FieldName, readings: list[Reading]) -> list[list[Reading]]:
    """Partition readings into distinct answers, in image order."""
    groups: dict[str, list[Reading]] = {}
    for reading in readings:
        if reading.value is None:  # only READ readings reach here
            continue
        groups.setdefault(materiality_key(name, reading.value), []).append(reading)
    return sorted(groups.values(), key=lambda g: min(r.image_index for r in g))


# --------------------------------------------------------------------------------------
# The merge
# --------------------------------------------------------------------------------------


def readings_for(extractions: list[Extraction], name: FieldName) -> tuple[Reading, ...]:
    """Every image that had something to say about `name`, in image order.

    An image that omitted the field said nothing and does not appear here. That is the
    whole of "I looked, it is not on this one" — it neither wins nor votes.
    """
    found = [
        Reading(
            image_index=extraction.image_index,
            value=field.value,
            confidence=field.confidence,
            legible=field.legible,
            bbox=field.bbox,
        )
        for extraction in extractions
        if (field := extraction.fields.get(name)) is not None
    ]
    return tuple(sorted(found, key=lambda r: r.image_index))


def merge_field(name: FieldName, readings: tuple[Reading, ...]) -> MergedField | None:
    """Fold every image's account of one field into one.

    Returns None when no image reported the field at all — absent from every image is
    Missing, and that is the caller's verdict to draw.
    """
    if not readings:
        return None

    best_kind = max(reading.kind for reading in readings)
    candidates = [reading for reading in readings if reading.kind is best_kind]

    if best_kind is not ReadingKind.READ:
        # Illegible everywhere, or blank everywhere. Either way there is nothing to
        # disagree about; take the earliest image so the evidence box points somewhere.
        winner = min(candidates, key=lambda r: r.image_index)
        return MergedField(
            field=name,
            value=None,
            confidence=0.0,
            legible=best_kind is not ReadingKind.ILLEGIBLE,
            bbox=winner.bbox,
            image_index=winner.image_index,
            readings=readings,
        )

    groups = _group_by_answer(name, candidates)

    if len(groups) == 1:
        # Every image that read it agrees. Best confidence wins, and it brings its
        # picture with it.
        winner = _best(groups[0])
        return MergedField(
            field=name,
            value=winner.value,
            confidence=winner.confidence,
            legible=True,
            bbox=winner.bbox,
            image_index=winner.image_index,
            readings=readings,
        )

    # Genuine disagreement. Confidence does not break this tie — see the module docstring.
    # The merged field says "not established": no value, not legible, so every downstream
    # consumer that ignores the conflict metadata still fails closed rather than passing
    # the more confident of two contradictory readings.
    disputants = tuple(_best(group) for group in groups)
    first = disputants[0]
    return MergedField(
        field=name,
        value=None,
        confidence=0.0,
        legible=False,
        # The evidence pair stays coherent: the box belongs to the picture named beside
        # it. Both readings are listed in the conflict, and the UI can walk them.
        bbox=first.bbox,
        image_index=first.image_index,
        readings=readings,
        conflict=Conflict(field=name, readings=disputants),
    )


def _merge_warning(
    extractions: list[Extraction], warning: MergedField | None
) -> tuple[str | None, int | None, WarningTypography]:
    """The statement and its typography, taken from the image the statement was read on.

    Typography signals describe how one photograph renders the statement. Pairing image
    2's bold judgement with image 1's text would be a determination nobody made, so both
    travel together from the winning image (WARN-6).
    """
    by_index = {extraction.image_index: extraction for extraction in extractions}

    if warning is not None and warning.value is not None:
        source = by_index.get(warning.image_index)
        return (
            warning.value,
            warning.image_index,
            source.warning_typography if source else WarningTypography(),
        )

    if warning is not None:
        # Unreadable, blank, or conflicted. Typography read off a statement we could not
        # establish is not a determination — drop it rather than let it be treated as one.
        return None, None, WarningTypography()

    # No image reported the field. A provider that supplies the statement text without
    # the field is inconsistent, but the text is still evidence the warning exists, and
    # dropping it would manufacture a Missing verdict. Earliest image wins.
    for extraction in sorted(extractions, key=lambda e: e.image_index):
        if extraction.warning_text:
            return (
                extraction.warning_text,
                extraction.image_index,
                extraction.warning_typography,
            )
    return None, None, WarningTypography()


def merge(extractions: list[Extraction]) -> MergedLabel:
    """Combine per-image extractions into one view of the label (IMG-8, TC-16)."""
    fields: dict[FieldName, MergedField] = {}
    for name in FieldName:
        merged = merge_field(name, readings_for(extractions, name))
        if merged is not None:
            fields[name] = merged

    text, image_index, typography = _merge_warning(
        extractions, fields.get(FieldName.GOVERNMENT_WARNING)
    )
    return MergedLabel(
        fields=fields,
        warning_text=text,
        warning_image_index=image_index,
        warning_typography=typography,
    )


# --------------------------------------------------------------------------------------
# Reporting a conflict to the agent
# --------------------------------------------------------------------------------------


def conflict_rationale(conflict: Conflict) -> str:
    """The one-line explanation shown on the field's row (MATCH-5).

    Says what each picture reads and says plainly that neither was accepted. An agent who
    reads only this line should come away knowing to look at both photographs.
    """
    label = _FIELD_LABELS[conflict.field]
    quoted = " and ".join(
        f'picture {picture_number(r.image_index)} reads "{r.value}"'
        for r in conflict.readings
    )
    return (
        f"The pictures disagree about the {label} — {quoted}. Neither reading has been "
        f"accepted, so this field has not been checked. Compare the pictures yourself."
    )


def conflict_finding(conflict: Conflict) -> Finding:
    """The structured finding that rides alongside the verdict."""
    return Finding(
        code=CONFLICT_CODE,
        message=conflict_rationale(conflict),
        severity="finding",
    )
