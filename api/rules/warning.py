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
would be the tool telling an agent that a variation was fine.

**Unreadable is doing two jobs, and one of them does not fit. Recorded, not hidden.**
Where the wording is right but the appearance could not be settled — bold unresolved,
contrast unjudged, the statement shrunk, two panels disagreeing — the verdict is
Unreadable. That drives the aggregate to Needs review, which is what the PRD asks for on
TC-06, and it can never be misread as a pass. But the PRD's own taxonomy defines
Unreadable as *"image quality prevents verification of this field"*, and on TC-06 the
warning is read perfectly: `extracted` carries the full verbatim statement.

So the field verdict contradicts the taxonomy table while the aggregate matches the test
case. That is a real gap in the six-value taxonomy, not a clever use of it: there is no
verdict meaning "read fine, complies as far as we can tell, and something about it needs
a human". Match is a lie, Mismatch is an accusation, Acceptable variation is the worst of
both. Unreadable is the least wrong of four ill-fitting options, chosen because it fails
in the safe direction.

Adding a seventh verdict is a product decision (MATCH-1) and is not taken here. What is
recorded here is that the gap exists and which way it was resolved, so the next person
sees a decision rather than an accident.

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
from api.models import BoundingBox, Finding, Verdict, WarningTypography
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
    if found[:-1] != expected[: len(found) - 1]:
        return False

    # The last surviving word is compared loosely, because a cut lands wherever the
    # artwork ran out rather than politely between words. Two shapes count: the printer
    # added a full stop where the text stopped, or the cut fell mid-word and left a
    # prefix of it ("...operate mach"). Both are a statement that ends early, and the
    # correction the applicant needs is the same one.
    last, expected_last = _bare(found[-1]), _bare(expected[len(found) - 1])
    return last == expected_last or (bool(last) and expected_last.startswith(last))


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


def required_wording_note() -> Finding:
    """Say where the left-hand side of the warning diff comes from.

    The warning row sends the canonical statement as `expected`, because that is what
    the diff has to compare against. The column it lands in is captioned "The application
    says", and a TTB application does not carry the warning statement — no applicant ever
    types it. So the column, read literally, asserts something false about what was
    filed.

    The real fix is a per-field caption in the UI, which is not this module's to make.
    This note closes the gap in the copy an agent actually reads, and it is worth having
    even once the caption is fixed: it tells them the comparison is against the
    regulation rather than against a filing, which is the more useful fact.
    """
    return Finding(
        code="warning_expected_is_the_regulation",
        message=(
            "The wording shown for comparison is the statement required by 27 CFR "
            "16.21, not something the applicant filed — applications do not carry the "
            "warning text."
        ),
        citation=canon.CITATIONS["warning_text"],
        severity=typography.SEVERITY_CONTEXT,
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
# What this module checks, and what it does not (DEL-6 / LP-218)
# --------------------------------------------------------------------------------------
#
# Documentation as data rather than prose, for one reason: prose about checks goes stale
# the first time someone adds a check, and nobody notices. A test asserts that this list
# and the finding codes the code can actually emit are the same set, so the two cannot
# drift. The UI and the README render it; an agent asking "what did this tool actually
# look at" gets an answer that is true by construction.


@dataclass(frozen=True)
class Check:
    """One thing this module looks for, and what happens when it finds it."""

    code: str
    checks: str
    citation: str
    evidence: str
    outcome: str


@dataclass(frozen=True)
class Limit:
    """Something the regulation requires that this tool does not verify.

    WARN-9 in list form. A checker that silently omits a requirement is telling an agent
    it checked more than it did, and on the government warning that is the failure this
    whole module exists to prevent.
    """

    requirement: str
    citation: str
    why_not: str


CHECK_MANIFEST: Final[tuple[Check, ...]] = (
    Check(
        code="warning_missing",
        checks="the statement appears on at least one of the supplied images",
        citation="27 CFR 16.21",
        evidence="the text read off every image",
        outcome="Missing — recommend returning the application",
    ),
    Check(
        code="warning_text_truncated",
        checks="the statement is complete rather than stopping part-way",
        citation="27 CFR 16.21",
        evidence="the words on the label",
        outcome="Mismatch",
    ),
    Check(
        code="warning_text_omission",
        checks="no required words have been dropped",
        citation="27 CFR 16.21",
        evidence="the words on the label",
        outcome="Mismatch",
    ),
    Check(
        code="warning_text_addition",
        checks="nothing has been added to the required statement",
        citation="27 CFR 16.21",
        evidence="the words on the label",
        outcome="Mismatch",
    ),
    Check(
        code="warning_text_reordering",
        checks="the required words appear in the required order",
        citation="27 CFR 16.21",
        evidence="the words on the label",
        outcome="Mismatch",
    ),
    Check(
        code="warning_text_casing",
        checks="the statement is capitalised as written in the regulation",
        citation="27 CFR 16.21",
        evidence="the words on the label",
        outcome="Mismatch",
    ),
    Check(
        code="warning_text_punctuation",
        checks="the statement is punctuated as written in the regulation",
        citation="27 CFR 16.21",
        evidence="the words on the label",
        outcome="Mismatch",
    ),
    Check(
        code="warning_text_rewording",
        checks="the statement is word for word, with no paraphrase",
        citation="27 CFR 16.21",
        evidence="the words on the label",
        outcome="Mismatch",
    ),
    Check(
        code="warning_header_missing",
        checks='the statement begins with the heading "GOVERNMENT WARNING:"',
        citation="27 CFR 16.22",
        evidence="the words on the label",
        outcome="Mismatch",
    ),
    Check(
        code="warning_header_not_all_caps",
        checks="the heading is in capital letters",
        citation="27 CFR 16.22",
        evidence="the words on the label",
        outcome="Mismatch",
    ),
    Check(
        code="warning_header_punctuation",
        checks="the heading ends in a colon",
        citation="27 CFR 16.21",
        evidence="the words on the label",
        outcome="Mismatch",
    ),
    Check(
        code="warning_header_caps_disputed",
        checks="the wording and the image agree about the heading's capitals",
        citation="27 CFR 16.22",
        evidence="the words on the label, against the typography signal",
        outcome="Unreadable — not settled, a person must look",
    ),
    Check(
        code="warning_header_not_bold",
        checks="the heading is in bold type",
        citation="27 CFR 16.22",
        evidence="a typography signal from the image",
        outcome="Mismatch",
    ),
    Check(
        code="warning_header_bold_unverified",
        checks="the heading is in bold type",
        citation="27 CFR 16.22",
        evidence="not established — the reading could not tell",
        outcome="Unreadable — a person must look",
    ),
    Check(
        code="warning_body_is_bold",
        checks="the rest of the statement is NOT in bold type",
        citation="27 CFR 16.22",
        evidence="a typography signal from the image",
        outcome="Mismatch",
    ),
    Check(
        code="warning_body_bold_unverified",
        checks="the rest of the statement is NOT in bold type",
        citation="27 CFR 16.22",
        evidence="not established — the reading could not tell",
        outcome="Unreadable — a person must look",
    ),
    Check(
        code="warning_text_disputed",
        checks="two readings of the label agree about what the warning says",
        citation="27 CFR 16.21",
        evidence="two readings that disagreed",
        outcome="Unreadable — not settled, a person must look",
    ),
    Check(
        code="warning_typography_disputed",
        checks="two readings of the same label agree about the type styling",
        citation="27 CFR 16.22",
        evidence="two readings that disagreed",
        outcome="Unreadable — not settled, a person must look",
    ),
    Check(
        code="warning_less_prominent",
        checks="the warning is not printed much smaller than the rest of the label",
        citation="27 CFR 16.22",
        evidence="a size ratio estimated from the image",
        outcome="Unreadable — a person must judge it against the label",
    ),
    Check(
        code="warning_low_contrast",
        checks="the warning stands out from the background behind it",
        citation="27 CFR 16.22",
        evidence="a contrast signal from the image",
        outcome="Unreadable — a person must judge it against the label",
    ),
    Check(
        code="warning_contrast_unverified",
        checks="the warning stands out from the background behind it",
        citation="27 CFR 16.22",
        evidence="not established — the reading could not tell",
        outcome="Unreadable — a person must look",
    ),
    Check(
        code="warning_prominence_unassessed",
        checks="nothing — it reports that size and contrast were not assessed",
        citation="27 CFR 16.22",
        evidence="not established",
        outcome="context only, never changes a verdict",
    ),
    Check(
        code="warning_expected_is_the_regulation",
        checks="nothing — it says where the wording shown for comparison comes from",
        citation="27 CFR 16.21",
        evidence="the regulation itself",
        outcome="context only, never changes a verdict",
    ),
    Check(
        code="warning_type_size_not_verified",
        checks="nothing — it states the minimum type size that applies to this container",
        citation="27 CFR 16.22",
        evidence="the container size on the application",
        outcome="context only, never changes a verdict",
    ),
    Check(
        code="warning_differs_between_images",
        checks="the images agree about what the warning says",
        citation="27 CFR 16.21",
        evidence="the text read off every image",
        outcome="Unreadable — the other panels have not been checked, a person must look",
    ),
)

#: Every finding code this module and `typography.py` can produce. A test asserts it
#: matches what the source actually emits, so the manifest cannot fall behind the code.
FINDING_CODES: Final[frozenset[str]] = frozenset(c.code for c in CHECK_MANIFEST)


LIMITS: Final[tuple[Limit, ...]] = (
    Limit(
        requirement="Minimum type size in millimetres, and maximum characters per inch",
        # Two subsections, not one: 16.22(b) maps container volume to millimetres in
        # prose, and 16.22(a)(4) carries the millimetres-to-characters-per-inch table.
        citation="27 CFR 16.22(a)(4), (b)",
        why_not=(
            "A photograph has no scale. Without knowing the physical size of the "
            "container in the frame, millimetres cannot be recovered from pixels. The "
            "applicable minimum is quoted as context so an agent can measure the real "
            "label, and the tool never claims to have measured it."
        ),
    ),
    Limit(
        requirement="The statement must be separate and apart from other information",
        citation="27 CFR 16.21",
        why_not=(
            "Judging separation needs the layout of the whole label, and what the "
            "extractor returns is the warning's own region. Text found around the "
            "warning is more often an artefact of how the region was read than a real "
            "crowding problem, and flagging it would train agents to ignore the row."
        ),
    ),
    Limit(
        requirement="Characters may not be compressed so as to impair legibility",
        citation="27 CFR 16.22(a)(3)",
        why_not=(
            "No signal in the extraction schema reports horizontal compression, so "
            "this is not checked at all. It is listed here rather than left out, "
            "because a requirement quietly missing from a checker reads as a "
            "requirement that was met."
        ),
    ),
    Limit(
        requirement="Whether the warning is bold, when the reading could not tell",
        citation="27 CFR 16.22(a)(2)",
        why_not=(
            "Stroke weight is hard to judge from a photograph of printed matter and "
            "the extraction model is allowed to decline. It declines often. That "
            "outcome is reported as Unreadable and routed to a person; it is never "
            "resolved in the label's favour."
        ),
    ),
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
    bbox: BoundingBox | None = None
    """The warning's region *on this image*. Carried with the sighting rather than
    looked up separately, so a region can never be paired with another photograph."""

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


def _with_findings(result: WarningResult, extra: list[Finding]) -> WarningResult:
    """Re-run routing with extra findings folded in.

    Escalation can only make the picture worse or leave it alone, so a result that was
    Match and now carries an unverified finding must stop being Match.
    """
    findings = [*result.findings, *extra]
    verdict = result.verdict
    rationale = result.rationale
    if verdict is Verdict.MATCH and any(
        f.severity != typography.SEVERITY_CONTEXT for f in extra
    ):
        verdict = Verdict.UNREADABLE
        rationale = next(
            f.message for f in extra if f.severity != typography.SEVERITY_CONTEXT
        )
    return WarningResult(
        verdict=verdict,
        rationale=rationale,
        diff=result.diff,
        findings=findings,
        comparison=result.comparison,
    )


def _escalate(
    rereader: typography.WarningRereader,
    chosen: WarningSighting | None,
    signals: WarningTypography,
    *,
    text: str | None,
    legible: bool,
) -> typography.MergedReading | None:
    """Ask a stronger model to re-read the warning region. Never fatal.

    A provider that is down must degrade to the first pass's answer rather than take the
    whole verification with it (NET-3, TC-21). The first pass already fails closed, so
    losing the second look costs certainty, never safety.
    """
    request = typography.escalation_request(
        signals,
        image_index=chosen.image_index if chosen else 0,
        bbox=chosen.bbox if chosen else None,
        legible=legible,
        warning_text=text,
    )
    try:
        reread = rereader.reread_warning(request)
    except Exception:  # any adapter failure degrades to the first pass (NET-3)
        return None
    return typography.adopt_reread(signals, reread, first_text=text)


def merge_sighting_typography(
    sightings: Sequence[WarningSighting],
) -> WarningTypography:
    """Fold every image's typography signals into one reading of one physical label.

    **Selecting a sighting must not discard what the other images established.** The
    front and the back are two photographs of one label; if the reading of image 1 says
    the statement is set in bold and image 0's reading is silent, the label is still set
    in bold. Judging only the chosen sighting's signals threw that answer away, and
    which sighting got chosen came down to an image-index tie-break — the same label
    passed or failed depending on the order the photographs were uploaded.

    So each signal takes its most concerning answer across the images that actually
    carried a warning. A detected violation on any image is a violation. An abstention
    never overrides an answer, and two images that both answered can only agree — a
    signal has one true value per label, and if the readings disagree, the concerning
    one is the one a person needs to see.
    """
    relevant = [s for s in sightings if s.has_text] or list(sightings)

    def fold(name: str, unsafe: bool) -> bool | None:
        values = [
            getattr(s.typography, name)
            for s in relevant
            if getattr(s.typography, name) is not None
        ]
        if not values:
            return None
        return unsafe if unsafe in values else values[0]

    sizes = [
        s.typography.relative_size
        for s in relevant
        if s.typography.relative_size is not None
    ]
    return WarningTypography(
        header_is_all_caps=fold("header_is_all_caps", unsafe=False),
        header_is_bold=fold("header_is_bold", unsafe=False),
        body_is_bold=fold("body_is_bold", unsafe=True),
        contrast_ok=fold("contrast_ok", unsafe=False),
        relative_size=min(sizes) if sizes else None,
    )


def conflicting_sightings_note(
    sightings: Sequence[WarningSighting], chosen: WarningSighting | None
) -> Finding | None:
    """Say so when two images showed different warning text.

    Choosing the most complete reading is right, and on its own it hides a defective
    warning printed on another panel: a reworded statement on the front and a correct one
    on the back would be reported as a clean match. This cannot be resolved from the
    images — the tool does not know whether the second reading is a second warning or a
    bad read of the first — so it does the one honest thing and refuses to call the
    label clean until a person has looked.
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
            "same on each. The most complete reading was checked and the others have "
            "not been — look at every panel yourself before approving this."
        ),
        citation=canon.CITATIONS["warning_text"],
        severity=typography.SEVERITY_UNVERIFIED,
    )


def evaluate_across_images(
    sightings: Sequence[WarningSighting],
    *,
    net_contents_ml: float | None = None,
    rereader: typography.WarningRereader | None = None,
) -> WarningResult:
    """The whole application's warning verdict, from every image at once (LP-217).

    The text comes from the best single reading; the typography comes from all of them.
    Those are different questions: "what does the statement say" has one answer that one
    photograph can give best, while "is any of it set in bold" is answered by whichever
    image could see it.

    `rereader` is the escalation hook. Passing None — the default, and what the pipeline
    passes until an adapter implements the protocol — simply skips the second look; it
    can never change a verdict from what the first pass established, because the merge
    refuses to clear a violation.
    """
    chosen = select_sighting(sightings)
    signals = merge_sighting_typography(sightings)
    text = chosen.text if chosen else None
    legible = chosen.legible if chosen else True
    escalation_findings: list[Finding] = []

    if rereader is not None and typography.needs_escalation(
        signals, warning_text=text, legible=legible
    ):
        merged = _escalate(rereader, chosen, signals, text=text, legible=legible)
        if merged is not None:
            signals = merged.typography
            text = merged.warning_text
            legible = legible or bool(text and text.strip())
            escalation_findings = list(merged.findings)

    result = evaluate(
        text,
        signals,
        legible=legible,
        net_contents_ml=net_contents_ml,
    )
    if escalation_findings:
        result = _with_findings(result, escalation_findings)
    note = conflicting_sightings_note(sightings, chosen)
    if note is None:
        return result

    # A disagreement between panels can never leave the label reported as clean.
    verdict = result.verdict
    rationale = result.rationale
    if verdict is Verdict.MATCH:
        verdict = Verdict.UNREADABLE
        rationale = note.message

    return WarningResult(
        verdict=verdict,
        rationale=rationale,
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
    honesty = [type_size_finding(net_contents_ml), required_wording_note()]

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
