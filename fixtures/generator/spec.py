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

    # --- expected verdicts, for the golden set ------------------------------------
    expect: dict[str, str] = field(default_factory=dict)
    """field name -> expected verdict. Empty means "all match"."""

    notes: str = ""
    """Why this fixture exists. Ends up in golden/set.json for a human reader."""

    def rendered_warning(self) -> str:
        """The exact warning string this spec puts on the label."""
        body = self.warning_text if self.warning_text is not None else canon.WARNING_BODY
        header = _apply_case(canon.WARNING_HEADER, self.warning_header_case)
        return f"{header} {body}"

    def with_(self, **changes: object) -> LabelSpec:
        """A copy with fields replaced — for building variants off a base spec."""
        return replace(self, **changes)  # type: ignore[arg-type]


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
