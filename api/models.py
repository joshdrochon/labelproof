"""Domain types shared by every layer.

The verdict taxonomy is fixed at exactly six values (MATCH-1). Adding a seventh is a
product decision, not an implementation convenience — if a case does not fit, it belongs
in `findings`, not in a new verdict.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Commodity(StrEnum):
    SPIRITS = "spirits"
    WINE = "wine"
    MALT = "malt"


class Verdict(StrEnum):
    """Per-field outcome. Exactly six values — see PRD §Verdict taxonomy."""

    MATCH = "match"
    ACCEPTABLE_VARIATION = "acceptable_variation"
    MISMATCH = "mismatch"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    NOT_APPLICABLE = "not_applicable"


class Recommendation(StrEnum):
    """Aggregate advice. The agent decides; this only ever recommends (HITL-1)."""

    READY_TO_APPROVE = "ready_to_approve"
    NEEDS_REVIEW = "needs_review"
    RETURN_FOR_CORRECTION = "return_for_correction"


class FieldName(StrEnum):
    """The seven mandatory label elements, per the brief."""

    BRAND_NAME = "brand_name"
    CLASS_TYPE = "class_type"
    ALCOHOL_CONTENT = "alcohol_content"
    NET_CONTENTS = "net_contents"
    PRODUCER = "producer"
    COUNTRY_OF_ORIGIN = "country_of_origin"
    GOVERNMENT_WARNING = "government_warning"


class Application(BaseModel):
    """What the applicant filed. The reference side of every comparison."""

    commodity: Commodity
    brand_name: str
    class_type: str
    alcohol_content: float | None = None
    net_contents: str
    producer_name: str
    producer_address: str
    country_of_origin: str | None = None
    is_import: bool = False


class BoundingBox(BaseModel):
    """Evidence region, normalized 0..1 against the PREPROCESSED image.

    Deskew changes geometry, so a box drawn over the original upload drifts. The UI
    displays the preprocessed image for exactly this reason.
    """

    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)


class Evidence(BaseModel):
    image_index: int
    bbox: BoundingBox | None = None


class Finding(BaseModel):
    """A compliance or format observation, independent of whether the field matched.

    A label can state net contents that exactly matches the application and still be
    non-compliant because 733 mL is not an authorized size (TC-10). Findings carry that;
    the verdict carries the comparison.
    """

    code: str
    message: str
    citation: str | None = None
    severity: str = "finding"


class FieldResult(BaseModel):
    field: FieldName
    verdict: Verdict
    extracted: str | None
    expected: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    tier: int | None = None
    evidence: Evidence | None = None
    findings: list[Finding] = Field(default_factory=list)


class Aggregate(BaseModel):
    recommendation: Recommendation
    rationale: str
    driving_field: FieldName | None = None


class ImageQuality(BaseModel):
    """Deterministic scores, 0..1 where 1 is best. Computed without any model call."""

    blur: float
    exposure: float
    glare: float
    skew_deg: float
    resolution_ok: bool
    verdict: str  # "ok" | "degraded" | "hopeless"
    reason: str | None = None


class ImageReport(BaseModel):
    index: int
    role: str | None = None
    quality: ImageQuality


class Timings(BaseModel):
    ingest: int = 0
    quality: int = 0
    preprocess: int = 0
    extract: int = 0
    compare: int = 0
    adjudicate: int = 0
    total: int = 0


class Cost(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0


class VerificationResult(BaseModel):
    request_id: str
    aggregate: Aggregate
    fields: list[FieldResult]
    images: list[ImageReport]
    timings_ms: Timings
    cost: Cost


class ExtractedField(BaseModel):
    """One field as read off the label by the extractor.

    `value` is None when the extractor could not read it. There is deliberately no
    "best guess" channel — a field that cannot be read is Unreadable, never a fabricated
    value, and that is asserted by test (LP-067).
    """

    value: str | None = None
    confidence: float = 0.0
    bbox: BoundingBox | None = None
    legible: bool = True


class WarningTypography(BaseModel):
    """Typography signals for the warning statement (WARN-2, WARN-7).

    Every field is optional because the extractor may be unable to judge it. `None`
    means "could not determine" and must route to Needs review — never to Match.
    """

    header_is_all_caps: bool | None = None
    header_is_bold: bool | None = None
    body_is_bold: bool | None = None
    relative_size: float | None = None
    contrast_ok: bool | None = None


class Extraction(BaseModel):
    """Everything read from one image. Produced by the provider, consumed by the rules."""

    image_index: int
    is_label: bool = True
    fields: dict[FieldName, ExtractedField] = Field(default_factory=dict)
    warning_text: str | None = None
    warning_typography: WarningTypography = Field(default_factory=WarningTypography)
