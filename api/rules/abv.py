"""Alcohol content parsing, proof cross-check, and format rules.

Three distinct jobs that must not be conflated:

1. **Parsing** — `45% Alc./Vol. (90 Proof)`, `Alcohol 45% by volume`, and `alc. 45% by
   vol.` all denote the same value.
2. **Internal consistency** — proof must equal twice ABV. `40% Alc./Vol. (90 Proof)` is
   wrong *on its own terms*, independent of what the application says (TC-09).
3. **Format compliance** — spirits labels may use only `alc.`/`vol.`; a bare "ABV" is a
   format finding (TC-22).

None of these is the label-vs-application comparison. That lives in `compare.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from api import canon
from api.models import Commodity, Finding

# `45%`, `45.5 %`, `45 percent`
_PERCENT = re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s*(?:%|percent\b)", re.IGNORECASE)
# `(90 Proof)`, `90 proof`, `90°`
_PROOF = re.compile(r"(\d{1,3}(?:\.\d{1,2})?)\s*(?:proof\b|°)", re.IGNORECASE)
# A bare number followed by an alcohol word, e.g. `Alcohol 45 by volume`
_BARE_WITH_CONTEXT = re.compile(
    r"(?:alc(?:ohol)?\.?)\s*(\d{1,2}(?:\.\d{1,2})?)\s*(?:by\s*vol|vol)", re.IGNORECASE
)
_ABV_TOKEN = re.compile(r"\bABV\b", re.IGNORECASE)


@dataclass(frozen=True)
class AlcoholContent:
    """A parsed alcohol statement."""

    abv: float | None = None
    proof: float | None = None
    raw: str = ""
    findings: list[Finding] = field(default_factory=list)

    @property
    def is_readable(self) -> bool:
        return self.abv is not None


def parse(text: str | None) -> AlcoholContent:
    """Parse an alcohol statement off a label. Returns abv=None when unreadable."""
    if not text or not text.strip():
        return AlcoholContent(raw=text or "")

    abv: float | None = None
    proof: float | None = None

    if (m := _PERCENT.search(text)) or (m := _BARE_WITH_CONTEXT.search(text)):
        abv = float(m.group(1))

    if m := _PROOF.search(text):
        proof = float(m.group(1))

    # A proof-only label still states alcohol content: proof implies ABV exactly.
    if abv is None and proof is not None:
        abv = proof / canon.PROOF_PER_ABV_POINT

    return AlcoholContent(abv=abv, proof=proof, raw=text)


def check_internal_consistency(parsed: AlcoholContent) -> list[Finding]:
    """Proof must be exactly twice ABV (TC-09).

    This is a property of the label alone. A label reading `40% Alc./Vol. (90 Proof)` is
    internally inconsistent whether or not the application says 40, 45, or nothing — so
    it is reported as its own finding rather than folded into the comparison verdict.
    """
    if parsed.abv is None or parsed.proof is None:
        return []

    expected_proof = parsed.abv * canon.PROOF_PER_ABV_POINT
    if abs(parsed.proof - expected_proof) < 0.05:
        return []

    implied_abv = parsed.proof / canon.PROOF_PER_ABV_POINT
    return [
        Finding(
            code="proof_abv_inconsistent",
            message=(
                f"The label states {parsed.abv:g}% alcohol by volume and "
                f"{parsed.proof:g} proof. Proof is twice the alcohol content, so "
                f"{parsed.proof:g} proof means {implied_abv:g}%. These do not agree."
            ),
            citation=canon.CITATIONS["spirits_abv"],
            severity="finding",
        )
    ]


def check_format(text: str | None, commodity: Commodity) -> list[Finding]:
    """Commodity-specific format rules for the alcohol statement (TC-22)."""
    if not text:
        return []

    findings: list[Finding] = []
    if commodity is Commodity.SPIRITS and _ABV_TOKEN.search(text):
        findings.append(
            Finding(
                code="spirits_abv_abbreviation",
                message=(
                    'Distilled spirits labels may use only "alc." and "vol." '
                    'abbreviations. This label uses "ABV".'
                ),
                # Not CITATIONS["spirits_abv"]. 27 CFR 5.65 authorizes four
                # abbreviations and never mentions "ABV"; the prohibition is TTB
                # guidance, and citing the regulation for it overstates its authority
                # to the applicant this finding is written for (LP-328).
                citation=canon.CITATIONS["spirits_abv_abbreviation"],
                severity="finding",
            )
        )
    return findings


def tolerance_context(commodity: Commodity, abv: float) -> str:
    """The regulatory tolerance, phrased so it can never read as an excuse.

    MATCH-8: tolerances govern the liquid versus the label. This tool compares the label
    against the application and cannot measure liquid, so the tolerance is context for
    the agent and never a reason to pass a mismatch.
    """
    pp = canon.abv_tolerance_pp(commodity.value, abv)
    return (
        f"For reference, {commodity.value} labels may differ from the actual liquid by "
        f"±{pp:g} percentage points. That tolerance applies to the liquid in the "
        f"bottle, which this tool cannot measure — it does not excuse a difference "
        f"between the label and the application."
    )
