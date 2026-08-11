"""Label specifications — what to render, including what to render *wrong*.

Every canonical test case that turns on text content or typography is generated from one
of these. A diffusion model cannot reliably render 50 words of legalese verbatim, and it
certainly cannot render them verbatim-except-the-header-is-title-case on demand. This can.

The defect knobs are the point. `warning_header_case="title"` produces exactly the label
Jenny rejected (TC-03), reproducibly, byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from api import canon

HeaderCase = Literal["upper", "title", "lower", "mixed"]


@dataclass(frozen=True)
class LabelSpec:
    """A label to render. Defaults produce a fully compliant spirits label."""

    # --- identity -----------------------------------------------------------------
    name: str = "old_tom_clean"
    commodity: Literal["spirits", "wine", "malt"] = "spirits"

    # --- content ------------------------------------------------------------------
    brand_name: str = "OLD TOM DISTILLERY"
    class_type: str = "Kentucky Straight Bourbon Whiskey"
    alcohol_text: str = "45% Alc./Vol. (90 Proof)"
    net_contents: str = "750 mL"
    producer: str = "Old Tom Distillery, Bardstown, Kentucky"
    country_of_origin: str | None = None

    # --- government warning, and the ways it goes wrong ---------------------------
    include_warning: bool = True
    warning_text: str | None = None
    """Override the statement body. None uses the canonical text (27 CFR 16.21)."""

    warning_header_case: HeaderCase = "upper"
    """`title` is TC-03 — the violation Jenny caught on a real label."""

    warning_header_bold: bool = True
    warning_body_bold: bool = False
    """True is TC-04 — 16.22's inverse rule, that the body must NOT be bold."""

    warning_scale: float = 1.0
    """Warning text size relative to normal body text. Below ~0.6 is TC-06."""

    warning_contrast: float = 1.0
    """1.0 is black on the label ground. Lower values bury it (TC-06)."""

    # --- layout -------------------------------------------------------------------
    face: Literal["front", "back", "single"] = "single"
    """`front`/`back` produce a two-image application; the warning lives on the back."""

    width: int = 1000
    height: int = 1400
    background: tuple[int, int, int] = (250, 248, 242)

    # --- the application side ------------------------------------------------------
    application_overrides: dict[str, object] = field(default_factory=dict)
    """Where the APPLICATION deliberately differs from what the label says.

    Most fixtures pair a label with an application stating the same values — the defect
    is on the label. A few invert that: TC-02's application reads `Stone's Throw` while
    the label shouts `STONE'S THROW`, and TC-08's application says 45% where the label
    says 40%. Without this the golden set could only ever express label defects."""

    # --- expected verdicts, for the golden set ------------------------------------
    expect: dict[str, str] = field(default_factory=dict)
    """field name -> expected verdict. Empty means "all match"."""

    expect_findings: dict[str, list[str]] = field(default_factory=dict)
    """field name -> finding codes that must be raised.

    Verdicts alone cannot express several canonical cases. TC-09's verdict is Match —
    the label agrees with the application — and the whole point of the case is the
    finding riding alongside it. An eval that checked only verdicts would score TC-09
    as passing while the proof inconsistency went undetected."""

    pending: str = ""
    """Ticket that must land before this fixture's expectation can hold.

    TC-06 expects a buried warning to be caught, but prominence heuristics are LP-211
    and do not exist. The expectation is right; the capability is missing. Marking it
    pending keeps the gap visible in the report without leaving the release gate
    permanently red — which would train everyone to ignore it."""

    notes: str = ""
    """Why this fixture exists. Ends up in golden/set.json for a human reader."""

    def rendered_warning(self) -> str:
        """The exact warning string this spec puts on the label."""
        body = self.warning_text if self.warning_text is not None else canon.WARNING_BODY
        header = _apply_case(canon.WARNING_HEADER, self.warning_header_case)
        return f"{header} {body}"

    def application(self) -> dict[str, object]:
        """The application record this label is verified against."""
        producer_name, _, producer_address = self.producer.partition(", ")
        record: dict[str, object] = {
            "commodity": self.commodity,
            "brand_name": self.brand_name,
            "class_type": self.class_type,
            "alcohol_content": _abv_of(self.alcohol_text),
            "net_contents": self.net_contents,
            "producer_name": producer_name,
            "producer_address": producer_address,
            "country_of_origin": self.country_of_origin,
            "is_import": False,
        }
        record.update(self.application_overrides)
        return record

    def with_(self, **changes: object) -> LabelSpec:
        """A copy with fields replaced — for building variants off a base spec."""
        return replace(self, **changes)  # type: ignore[arg-type]


def _abv_of(text: str) -> float | None:
    """The ABV an application would carry for this label. None when the label omits it."""
    from api.rules.abv import parse

    return parse(text).abv if text else None


def _apply_case(header: str, case: HeaderCase) -> str:
    stem = header.rstrip(":")
    match case:
        case "upper":
            out = stem.upper()
        case "title":
            out = stem.title()
        case "lower":
            out = stem.lower()
        case "mixed":
            out = "".join(
                c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(stem)
            )
    return f"{out}:"
