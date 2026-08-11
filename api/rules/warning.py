"""Government health warning statement — presence, verbatim text, and typography.

Jenny's specification: *"It has to be exact. Like, word-for-word, and the 'GOVERNMENT
WARNING:' part has to be in all caps and bold."* She rejected a label for title-case
`Government Warning`. This module must catch everything she catches.

**This module deliberately does NOT use `normalize.normalize()`.** That function casefolds
and folds punctuation, which is right for brand names and catastrophic here — casefolding
would erase the exact violation Jenny caught. Warning comparison collapses whitespace and
nothing else, because line breaks are label layout while case and punctuation are the
regulation.

**Fail closed.** Uncertainty about the warning is Needs review or Unreadable, never Match
(PRD §Constraints). Every function here returns "cannot confirm" rather than a pass when
its input is missing or ambiguous.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from api import canon
from api.models import Finding, Verdict, WarningTypography

_WHITESPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"\S+")


def collapse_layout_whitespace(text: str) -> str:
    """Collapse line breaks and runs of spaces. Case and punctuation are untouched."""
    return _WHITESPACE.sub(" ", text).strip()


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(collapse_layout_whitespace(text))


@dataclass(frozen=True)
class DiffSegment:
    """One span of the word-level diff, for rendering as evidence (WARN-8)."""

    op: str  # "equal" | "replace" | "delete" | "insert"
    expected: list[str] = field(default_factory=list)
    found: list[str] = field(default_factory=list)

    @property
    def is_difference(self) -> bool:
        return self.op != "equal"


def tokenized_diff(found_text: str) -> list[DiffSegment]:
    """Word-level diff of the label's warning against the canonical statement.

    `delete` means the label omitted required words; `insert` means it added words that
    do not belong. Both are violations — the statement must appear word for word.
    """
    expected = tokenize(canon.CANONICAL_WARNING)
    found = tokenize(found_text)
    matcher = difflib.SequenceMatcher(a=expected, b=found, autojunk=False)

    segments: list[DiffSegment] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        segments.append(DiffSegment(op=op, expected=expected[i1:i2], found=found[j1:j2]))
    return segments


def diff_summary(segments: list[DiffSegment]) -> str:
    """One plain-language line describing the first substantive difference."""
    for seg in segments:
        if not seg.is_difference:
            continue
        match seg.op:
            case "replace":
                return (
                    f'The label reads "{" ".join(seg.found)}" where the required text '
                    f'reads "{" ".join(seg.expected)}".'
                )
            case "delete":
                return f'The label is missing the words "{" ".join(seg.expected)}".'
            case "insert":
                return f'The label adds the words "{" ".join(seg.found)}", which are not part of the required statement.'
    return "The warning statement matches the required text word for word."


def is_verbatim(found_text: str) -> bool:
    """Does the label carry the statement exactly, ignoring only layout whitespace?"""
    return collapse_layout_whitespace(found_text) == collapse_layout_whitespace(
        canon.CANONICAL_WARNING
    )


# --------------------------------------------------------------------------------------
# Typography — 27 CFR 16.22
# --------------------------------------------------------------------------------------


def header_as_written(found_text: str) -> str | None:
    """The label's own rendering of the header, however it was capitalized.

    Matched case-insensitively so a title-case header is *found* and can then be judged,
    rather than being missed and reported as a text mismatch.
    """
    collapsed = collapse_layout_whitespace(found_text)
    m = re.match(r"government\s+warning\s*[:,]?", collapsed, re.IGNORECASE)
    return m.group(0) if m else None


def check_header_caps(found_text: str) -> list[Finding]:
    """WARN-2 / WARN-3 — Jenny's catch. Title case is a violation."""
    header = header_as_written(found_text)
    if header is None:
        return [
            Finding(
                code="warning_header_missing",
                message='The label does not begin with "GOVERNMENT WARNING:".',
                citation=canon.CITATIONS["warning_format"],
            )
        ]

    words_only = header.rstrip(":, ").strip()
    if words_only.isupper():
        return []

    return [
        Finding(
            code="warning_header_not_all_caps",
            message=(
                f'The words "GOVERNMENT WARNING" must appear in capital letters. '
                f'This label reads "{words_only}".'
            ),
            citation=canon.CITATIONS["warning_format"],
        )
    ]


def check_typography(signals: WarningTypography) -> list[Finding]:
    """Bold requirements from the extractor's typography signals.

    Every signal is tri-state. `None` means the extractor could not determine it, which
    produces a *cannot-confirm* finding rather than silence — silence would read as a
    pass, and the warning statement fails closed.
    """
    findings: list[Finding] = []

    match signals.header_is_bold:
        case False:
            findings.append(
                Finding(
                    code="warning_header_not_bold",
                    message=(
                        'The words "GOVERNMENT WARNING" must appear in bold type. '
                        "On this label they do not."
                    ),
                    citation=canon.CITATIONS["warning_format"],
                )
            )
        case None:
            findings.append(
                Finding(
                    code="warning_header_bold_unverified",
                    message=(
                        "Could not determine whether the warning heading is bold. "
                        "Check this by eye."
                    ),
                    citation=canon.CITATIONS["warning_format"],
                    severity="unverified",
                )
            )

    # WARN-7 — the inverse rule. 16.22 requires the remainder NOT be bold.
    match signals.body_is_bold:
        case True:
            findings.append(
                Finding(
                    code="warning_body_is_bold",
                    message=(
                        "Only the words \"GOVERNMENT WARNING\" may be bold. The rest of "
                        "the statement must not be in bold type, and on this label it is."
                    ),
                    citation=canon.CITATIONS["warning_format"],
                )
            )
        case None:
            findings.append(
                Finding(
                    code="warning_body_bold_unverified",
                    message=(
                        "Could not determine whether the body of the warning is bold. "
                        "Check this by eye."
                    ),
                    citation=canon.CITATIONS["warning_format"],
                    severity="unverified",
                )
            )

    return findings


def type_size_context(net_contents_ml: float | None) -> str:
    """WARN-9 — state the applicable minimum, and admit it cannot be verified here."""
    if net_contents_ml is None:
        return (
            "Type size cannot be verified from a photograph, and the container size is "
            "unknown, so the applicable minimum could not be determined."
        )
    min_mm, max_cpi = canon.warning_type_size_for(net_contents_ml)
    return (
        f"For this container size the warning must be at least {min_mm:g} mm tall with "
        f"no more than {max_cpi} characters per inch. Type size is not verifiable from "
        f"an unscaled photograph — this is context for your own eye, not a check the "
        f"tool performed."
    )


# --------------------------------------------------------------------------------------
# Top-level verdict
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WarningResult:
    verdict: Verdict
    rationale: str
    diff: list[DiffSegment] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def evaluate(
    found_text: str | None,
    signals: WarningTypography | None = None,
    *,
    legible: bool = True,
) -> WarningResult:
    """Full warning verdict.

    Order matters. Illegibility is reported before absence, because "we could not read
    it" and "it is not there" are different findings and confusing them is exactly the
    false-pass this product must never produce.
    """
    signals = signals or WarningTypography()

    if not legible:
        return WarningResult(
            verdict=Verdict.UNREADABLE,
            rationale=(
                "The warning statement could not be read on this image. It has not been "
                "checked — request a clearer image."
            ),
        )

    if found_text is None or not found_text.strip():
        # TC-07. Callers must search every image before reaching this (IMG-8).
        return WarningResult(
            verdict=Verdict.MISSING,
            rationale=(
                "No government warning statement was found on any of the supplied "
                "images. It is required on all alcohol beverage labels."
            ),
            findings=[
                Finding(
                    code="warning_missing",
                    message="No government warning statement found.",
                    citation=canon.CITATIONS["warning_text"],
                    severity="critical",
                )
            ],
        )

    diff = tokenized_diff(found_text)
    findings = check_header_caps(found_text) + check_typography(signals)
    text_matches = is_verbatim(found_text)

    if not text_matches:
        return WarningResult(
            verdict=Verdict.MISMATCH,
            rationale=diff_summary(diff),
            diff=diff,
            findings=findings,
        )

    hard_findings = [f for f in findings if f.severity != "unverified"]
    if hard_findings:
        return WarningResult(
            verdict=Verdict.MISMATCH,
            rationale=hard_findings[0].message,
            diff=diff,
            findings=findings,
        )

    if findings:
        # Text is verbatim but something could not be confirmed. Fails closed.
        return WarningResult(
            verdict=Verdict.ACCEPTABLE_VARIATION,
            rationale=(
                "The wording is exactly right, but some formatting could not be "
                "confirmed from this image. Check it by eye."
            ),
            diff=diff,
            findings=findings,
        )

    return WarningResult(
        verdict=Verdict.MATCH,
        rationale="The warning statement matches the required text word for word.",
        diff=diff,
    )
