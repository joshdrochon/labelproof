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

**No verdict here is Acceptable variation.** Five of the six verdicts can apply to the
warning statement; that one cannot. A brand name has acceptable variations — STONE'S
THROW is Stone's Throw. The statement required by 27 CFR 16.21 does not: it is exact or
it is wrong, and a row reading "Acceptable variation" against the government warning
would be the tool telling an agent that a variation was fine. Where the wording is right
but the appearance could not be confirmed, the verdict is Unreadable — which means
exactly what happened, and can never be misread as a pass.

Appearance rules (bold, capitals, prominence) live in `typography.py`; this module owns
the text and the verdict.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from api import canon
from api.models import Finding, Verdict, WarningTypography
from api.rules import typography

_WHITESPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"\S+")

#: Characters that are pure layout artefacts of printing and OCR, carrying no meaning.
_INVISIBLE = str.maketrans({c: None for c in "­​‌‍﻿"})

#: A hyphen immediately followed by a line break is word wrapping, not punctuation.
#: Rejoining is safe in this one module because the canonical statement contains no
#: hyphen at all — so no real difference in the required words can hide behind it.
_HYPHEN_WRAP = re.compile("[-\u2010\u2011]\\s*\\n\\s*")


def collapse_layout_whitespace(text: str) -> str:
    """Reduce the text to its words, changing nothing about how they are written.

    Line breaks, runs of spaces, soft hyphens and end-of-line hyphenation are label
    layout — the same statement set in a narrower column. Case and punctuation are the
    regulation and are never touched here. This is the entire reason this module does
    not call `normalize.normalize()`.
    """
    return _WHITESPACE.sub(" ", _HYPHEN_WRAP.sub("", text.translate(_INVISIBLE))).strip()


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
                return (
                    f'The label adds the words "{" ".join(seg.found)}", which are not '
                    f"part of the required statement."
                )
    return "The warning statement matches the required text word for word."


def is_verbatim(found_text: str) -> bool:
    """Does the label carry the statement exactly, ignoring only layout whitespace?"""
    return collapse_layout_whitespace(found_text) == collapse_layout_whitespace(
        canon.CANONICAL_WARNING
    )


# --------------------------------------------------------------------------------------
# What kind of difference is it? (WARN-4 / LP-209, LP-210)
# --------------------------------------------------------------------------------------
#
# "Mismatch" is the verdict for all of these, and it is not enough on its own. An agent
# returning an application has to write down what the applicant must fix, and "the
# warning does not match" is not that. A statement that stops halfway, a statement with a
# paraphrased clause, and a statement with a marketing line bolted on are three different
# corrections. So the diff is classified, once, into the kind of thing that went wrong.

VERBATIM: Final[str] = "verbatim"
TRUNCATED: Final[str] = "truncated"
OMISSION: Final[str] = "omission"
ADDITION: Final[str] = "addition"
REORDERING: Final[str] = "reordering"
CASING: Final[str] = "casing"
PUNCTUATION: Final[str] = "punctuation"
REWORDING: Final[str] = "rewording"

#: Plain-language name per kind, for the finding an agent reads.
_KIND_MESSAGE: Final[dict[str, str]] = {
    TRUNCATED: (
        "The warning starts correctly and then stops. The whole statement is required, "
        "including the second numbered part."
    ),
    OMISSION: "Words required by the regulation are missing from the warning.",
    ADDITION: (
        "The warning carries the required wording plus extra words. The statement must "
        "appear on its own, exactly as written."
    ),
    REORDERING: (
        "The warning uses the required words in a different order. The statement must "
        "appear word for word, in order."
    ),
    CASING: (
        "The warning is worded correctly but capitalised differently from the required "
        "statement."
    ),
    PUNCTUATION: (
        "The warning is worded correctly but punctuated differently from the required "
        "statement."
    ),
    REWORDING: (
        "The warning has been reworded. It must appear word for word as written in the "
        "regulation — no paraphrasing, however reasonable it reads."
    ),
}

_PUNCTUATION_CHARS: Final[str] = ".,;:!?()[]\"'"


def _bare(token: str) -> str:
    return token.strip(_PUNCTUATION_CHARS)


def _is_truncation(expected: list[str], found: list[str]) -> bool:
    """Does the label carry the opening of the statement and then stop?

    The last surviving word usually picks up a full stop the printer added when the text
    was cut, so the final token is compared without its punctuation. Everything before it
    must be word-for-word identical — a truncation is a statement that ends early, not a
    statement that also says something else.
    """
    if not found or len(found) >= len(expected):
        return False
    if found == expected[: len(found)]:
        return True
    return (
        found[:-1] == expected[: len(found) - 1]
        and _bare(found[-1]) == _bare(expected[len(found) - 1])
    )


@dataclass(frozen=True)
class TextComparison:
    """The label's warning text measured against 27 CFR 16.21."""

    kind: str
    segments: list[DiffSegment] = field(default_factory=list)
    missing_words: list[str] = field(default_factory=list)
    added_words: list[str] = field(default_factory=list)

    @property
    def is_verbatim(self) -> bool:
        return self.kind == VERBATIM


def classify(found_text: str) -> TextComparison:
    """Diff the label's warning and say what kind of difference it is."""
    expected = tokenize(canon.CANONICAL_WARNING)
    found = tokenize(found_text)
    segments = tokenized_diff(found_text)

    missing = [w for s in segments if s.op in ("delete", "replace") for w in s.expected]
    added = [w for s in segments if s.op in ("insert", "replace") for w in s.found]

    def result(kind: str) -> TextComparison:
        return TextComparison(
            kind=kind, segments=segments, missing_words=missing, added_words=added
        )

    if expected == found:
        return result(VERBATIM)
    if _is_truncation(expected, found):
        return result(TRUNCATED)

    ops = {s.op for s in segments if s.is_difference}
    replacements = [s for s in segments if s.op == "replace"]

    if ops == {"insert"}:
        return result(ADDITION)
    if ops == {"delete"}:
        return result(OMISSION)
    if sorted(expected) == sorted(found):
        return result(REORDERING)
    if replacements and ops == {"replace"}:
        pairs = [(s.expected, s.found) for s in replacements]
        if all(
            [w.casefold() for w in exp] == [w.casefold() for w in got]
            for exp, got in pairs
        ):
            return result(CASING)
        if all(
            [_bare(w) for w in exp] == [_bare(w) for w in got] for exp, got in pairs
        ):
            return result(PUNCTUATION)
    return result(REWORDING)


def text_findings(comparison: TextComparison) -> list[Finding]:
    """The finding an agent acts on, named for the kind of difference (WARN-4)."""
    if comparison.is_verbatim:
        return []
    return [
        Finding(
            code=f"warning_text_{comparison.kind}",
            message=_KIND_MESSAGE[comparison.kind],
            citation=canon.CITATIONS["warning_text"],
            severity=typography.SEVERITY_VIOLATION,
        )
    ]


# --------------------------------------------------------------------------------------
# Typography — 27 CFR 16.22
# --------------------------------------------------------------------------------------


_HEADER = re.compile(r"government\s+warning\s*[:;,.]?", re.IGNORECASE)


def header_as_written(found_text: str) -> str | None:
    """The label's own rendering of the heading, however it was capitalized.

    Matched case-insensitively so a title-case heading is *found* and can then be judged,
    rather than being missed and reported as a text mismatch. Searched rather than
    anchored: an extractor that returns the warning together with a line of surrounding
    text should still have its heading examined, not skipped.
    """
    m = _HEADER.search(collapse_layout_whitespace(found_text))
    return m.group(0) if m else None


def check_header_caps(
    found_text: str, signals: WarningTypography | None = None
) -> list[Finding]:
    """WARN-2 / WARN-3 — Jenny's catch. Title case is a violation.

    Capitalization is read off the text itself rather than off a model signal, because
    the text *is* the evidence: if the label says `Government Warning:` the characters
    say so and nothing needs to be inferred.

    The `header_is_all_caps` signal is used only as a contradiction check. An extractor
    that tidied the statement into canonical form before returning it would hand us a
    perfect string and erase the violation silently, and that is the one failure mode
    text-only checking cannot see. When the signal says the heading was not in capitals
    and the returned text says it was, we believe neither and route to a human.
    """
    header = header_as_written(found_text)
    if header is None:
        return [
            Finding(
                code="warning_header_missing",
                message=(
                    'The warning does not carry the heading "GOVERNMENT WARNING:". '
                    "The statement must begin with it."
                ),
                citation=canon.CITATIONS["warning_format"],
                severity=typography.SEVERITY_VIOLATION,
            )
        ]

    words_only = header.rstrip(":;,. ").strip()
    if not words_only.isupper():
        return [
            Finding(
                code="warning_header_not_all_caps",
                message=(
                    f'The words "GOVERNMENT WARNING" must appear in capital letters. '
                    f'This label reads "{words_only}".'
                ),
                citation=canon.CITATIONS["warning_format"],
                severity=typography.SEVERITY_VIOLATION,
            )
        ]

    # The colon is the only punctuation the regulation actually prescribes. 16.22 quotes
    # the phrase as `"GOVERNMENT WARNING,"` when stating the bold rule, but that comma
    # sits inside the closing quotation mark as American typography — it belongs to the
    # sentence in the regulation, not to the required phrase. The statement itself, in
    # 16.21, ends the heading with a colon. Verified 2026-08-11; see LP-328.
    if header.rstrip().endswith(canon.WARNING_HEADER[-1]) is False:
        written = header.strip()
        return [
            Finding(
                code="warning_header_punctuation",
                message=(
                    f'The heading must read "{canon.WARNING_HEADER}" with a colon. '
                    f'This label reads "{written}".'
                ),
                citation=canon.CITATIONS["warning_text"],
                severity=typography.SEVERITY_VIOLATION,
            )
        ]

    if signals is not None and signals.header_is_all_caps is False:
        return [
            Finding(
                code="warning_header_caps_disputed",
                message=(
                    "The wording came back in capital letters, but the reading of the "
                    "image says the heading is not capitalised. The two disagree, so "
                    "this has not been settled — look at the heading yourself."
                ),
                citation=canon.CITATIONS["warning_format"],
                severity=typography.SEVERITY_UNVERIFIED,
            )
        ]

    return []


def check_typography(signals: WarningTypography) -> list[Finding]:
    """Every 16.22 appearance rule. Delegates to `typography.assess` (WARN-2, WARN-7)."""
    return list(typography.assess(signals).findings)


def type_size_context(net_contents_ml: float | None) -> str:
    """WARN-9 — state the applicable minimum, and admit it cannot be verified here.

    Two sentences, and the order is deliberate. The number comes first because it is
    useful — an agent holding the bottle can act on "at least 2 mm". The disclaimer comes
    second because without it the number reads as a measurement the tool took, and that
    would be the tool claiming precision it does not have.
    """
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


def type_size_finding(net_contents_ml: float | None) -> Finding:
    """The same honesty, as a finding rather than a sentence bolted onto a rationale.

    It rides on every warning result, including a Match. A clean label is exactly where
    this matters most: the row says Match, and the agent has to know that "match" covered
    the wording and the type style and did *not* cover the millimetres.
    """
    return Finding(
        code="warning_type_size_not_verified",
        message=type_size_context(net_contents_ml),
        citation=canon.CITATIONS["warning_format"],
        severity=typography.SEVERITY_CONTEXT,
    )


# --------------------------------------------------------------------------------------
# One application, several images (IMG-8 / LP-217 / TC-16)
# --------------------------------------------------------------------------------------
#
# A front and a back photograph are one label, and the warning usually lives on the back.
# Declaring it Missing without looking at every image would be a false finding on a
# perfectly compliant application — and it is the finding that returns the application to
# the applicant, so it has to be right.


@dataclass(frozen=True)
class WarningSighting:
    """The warning as read off one image. One per image, including the images with none."""

    image_index: int
    text: str | None = None
    legible: bool = True
    confidence: float = 0.0
    typography: WarningTypography = field(default_factory=WarningTypography)

    @property
    def has_text(self) -> bool:
        return bool(self.text and self.text.strip())


def completeness(text: str | None) -> int:
    """How many words of the required statement this reading actually carries.

    Used to choose between images, not to judge compliance. A front label with a
    decorative fragment and a back label with the whole statement are both "a warning";
    only one of them is the statement, and word count is the cheapest honest way to tell
    them apart.
    """
    if not text or not text.strip():
        return 0
    return sum(len(seg.expected) for seg in tokenized_diff(text) if seg.op == "equal")


def select_sighting(sightings: Sequence[WarningSighting]) -> WarningSighting | None:
    """The reading to judge the application on, or None if no image showed a warning.

    Preference order: a legible reading over an illegible one, then the reading that
    carries most of the required statement, then the reading the extractor was most sure
    of. Returning None is the only route to Missing, and it requires every image to have
    come back with nothing.
    """
    with_text = [s for s in sightings if s.has_text]
    if with_text:
        return max(
            with_text,
            key=lambda s: (s.legible, completeness(s.text), s.confidence, -s.image_index),
        )
    # Nothing readable anywhere. An image that could not be read is not an image with no
    # warning on it, so an illegible sighting still wins over silence.
    illegible = [s for s in sightings if not s.legible]
    return illegible[0] if illegible else None


def conflicting_sightings_note(
    sightings: Sequence[WarningSighting], chosen: WarningSighting | None
) -> Finding | None:
    """Say so when two images showed different warning text.

    Choosing the most complete reading is right, and it can also hide a defective warning
    printed elsewhere on the same label. The tool cannot resolve that from here, so it
    says what it did rather than quietly picking a winner.
    """
    if chosen is None:
        return None
    others = {
        collapse_layout_whitespace(s.text or "")
        for s in sightings
        if s.has_text and s.image_index != chosen.image_index
    }
    others.discard(collapse_layout_whitespace(chosen.text or ""))
    if not others:
        return None
    return Finding(
        code="warning_differs_between_images",
        message=(
            "More than one image carries a government warning and the wording is not the "
            "same on each. The most complete reading was checked; look at the others "
            "yourself."
        ),
        citation=canon.CITATIONS["warning_text"],
        severity=typography.SEVERITY_CONTEXT,
    )


def evaluate_across_images(
    sightings: Sequence[WarningSighting],
    *,
    net_contents_ml: float | None = None,
) -> WarningResult:
    """The whole application's warning verdict, from every image at once (LP-217)."""
    chosen = select_sighting(sightings)
    result = evaluate(
        chosen.text if chosen else None,
        chosen.typography if chosen else None,
        legible=chosen.legible if chosen else True,
        net_contents_ml=net_contents_ml,
    )
    note = conflicting_sightings_note(sightings, chosen)
    if note is None:
        return result
    return WarningResult(
        verdict=result.verdict,
        rationale=result.rationale,
        diff=result.diff,
        findings=[*result.findings, note],
        comparison=result.comparison,
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
    comparison: TextComparison | None = None
    """The text classification, when there was text to classify (LP-209, LP-210)."""

    @property
    def kind(self) -> str | None:
        """What kind of text difference this was, if any."""
        return self.comparison.kind if self.comparison else None


def evaluate(
    found_text: str | None,
    signals: WarningTypography | None = None,
    *,
    legible: bool = True,
    net_contents_ml: float | None = None,
) -> WarningResult:
    """Full warning verdict.

    Order matters. Illegibility is reported before absence, because "we could not read
    it" and "it is not there" are different findings and confusing them is exactly the
    false-pass this product must never produce.

    `net_contents_ml` only ever adds context. It selects the type-size minimum quoted to
    the agent (WARN-9) and it can never change a verdict — no container size makes a
    wrong warning right.
    """
    signals = signals or WarningTypography()
    honesty = [type_size_finding(net_contents_ml)]

    if not legible:
        return WarningResult(
            verdict=Verdict.UNREADABLE,
            rationale=(
                "The warning statement could not be read on this image. It has not been "
                "checked — request a clearer image."
            ),
            findings=honesty,
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
                    severity=typography.SEVERITY_CRITICAL,
                ),
                *honesty,
            ],
        )

    comparison = classify(found_text)
    diff = comparison.segments
    look = typography.assess(signals)
    findings = (
        check_header_caps(found_text, signals)
        + text_findings(comparison)
        + list(look.findings)
        + honesty
    )

    # 1. The words themselves. Everything else is secondary to "does it say the thing".
    if not comparison.is_verbatim:
        return WarningResult(
            verdict=Verdict.MISMATCH,
            rationale=diff_summary(diff),
            diff=diff,
            findings=findings,
            comparison=comparison,
        )

    # 2. The label does not comply with 16.22's type-style rules. It says the right
    #    words in the wrong type, which is a correction the applicant has to make.
    violations = [
        f for f in findings if f.severity in typography.ASSERTED_SEVERITIES
    ]
    hard_style = [f for f in violations if f.code not in look.prominence_concerns]
    if hard_style:
        return WarningResult(
            verdict=Verdict.MISMATCH,
            rationale=hard_style[0].message,
            diff=diff,
            findings=findings,
            comparison=comparison,
        )

    # 3. Prominence. WARN-5 puts these in front of a human rather than returning the
    #    application: "smaller than the rest of the label" is a judgement about a
    #    photograph, and the regulation's own line is in millimetres we cannot measure.
    if look.prominence_concerns:
        prominence = next(
            f for f in findings if f.code in look.prominence_concerns
        )
        return WarningResult(
            verdict=Verdict.UNREADABLE,
            rationale=prominence.message,
            diff=diff,
            findings=findings,
            comparison=comparison,
        )

    # 4. The wording is right and nothing is broken, but a bright line was left
    #    unresolved. Not a match, and not an accusation either.
    unresolved = [f for f in findings if f.severity == typography.SEVERITY_UNVERIFIED]
    if unresolved:
        return WarningResult(
            verdict=Verdict.UNREADABLE,
            rationale=(
                "The wording is exactly right, but the type styling could not be "
                "confirmed from this image, so the warning has not been fully "
                "checked. Look at it with your own eye."
            ),
            diff=diff,
            findings=findings,
            comparison=comparison,
        )

    return WarningResult(
        verdict=Verdict.MATCH,
        rationale="The warning statement matches the required text word for word.",
        diff=diff,
        findings=findings,
        comparison=comparison,
    )
