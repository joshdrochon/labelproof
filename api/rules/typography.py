"""27 CFR 16.22 — how the government warning must *look*.

`warning.py` owns what the statement must **say**. This module owns what it must **look
like**: the heading in capitals and bold, the statement that follows not bold, and the
prominence requirements that stop a label from technically carrying the warning while
practically hiding it.

The tri-state contract
----------------------

Every signal on `WarningTypography` is `bool | None`, and the three states mean three
different things:

===========  ==================================================================
``True``     We checked and it complies.
``False``    We checked and it does **not** comply. A violation.
``None``     The extractor could not tell. **Not** a pass, and not a violation.
===========  ==================================================================

`None` is the common case, not the exception. Extraction runs on Claude Haiku 4.5 — the
only model that fits the five-second budget, and the weakest one in the family. Judging
stroke weight from a photograph of a printed label is exactly the kind of thing it will
decline to answer. So the abstention path is not an error path here; it is a normal
outcome, and it is tested as thoroughly as the answering path.

Bright lines versus heuristics
------------------------------

Two classes of signal, deliberately routed differently:

**Bright lines** — the heading is bold, the body is not bold. 16.22 states these as
requirements with a yes/no answer, and Jenny's specification names them out loud. An
abstention here is *unconfirmed*: the field cannot reach Match, and an agent is told to
check by eye. Fails closed, unconditionally.

**Heuristics** — relative size and contrast. The regulation's real line for size is an
absolute measurement in millimetres, and WARN-9 concedes that is unmeasurable from an
unscaled photograph. These signals detect the *evasion pattern* Jenny described, not the
regulation itself. A negative answer raises a finding; an abstention says so plainly and
does not, on its own, hold up a label — otherwise every label would be held up forever
and the honesty caveat would be the only thing anyone read.

No thresholds
-------------

Nothing here consults `api.rules.thresholds`. The warning statement is exempt from every
confidence knob in the system (WARN-6): a low-confidence read of the warning does not get
a relaxed rule, it gets a second look and then a human. The one ratio in this module,
`PROMINENCE_CONCERN_RATIO`, can only ever *add* a finding — no value of it can turn a
violation into a pass — which is why it lives here beside the check it serves rather than
in the tuning module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from api import canon
from api.models import BoundingBox, Finding, WarningTypography

# --------------------------------------------------------------------------------------
# Finding severities
# --------------------------------------------------------------------------------------
#
# Four levels, and the difference between the last two is the whole design:
#
#   critical    — the element is absent. Disqualifying on its own.
#   finding     — we checked and it does not comply.
#   unverified  — a bright line we could not resolve. Blocks Match, needs a human.
#   context     — something we never claimed to check. Never changes a verdict.

SEVERITY_CRITICAL: Final[str] = "critical"
SEVERITY_VIOLATION: Final[str] = "finding"
SEVERITY_UNVERIFIED: Final[str] = "unverified"
SEVERITY_CONTEXT: Final[str] = "context"

#: Severities that assert non-compliance. Anything else is an admission of ignorance.
ASSERTED_SEVERITIES: Final[frozenset[str]] = frozenset(
    {SEVERITY_CRITICAL, SEVERITY_VIOLATION}
)


# --------------------------------------------------------------------------------------
# Prominence (WARN-5)
# --------------------------------------------------------------------------------------

#: `relative_size` is the warning's character height divided by the height of ordinary
#: body text elsewhere on the label. 1.0 means "the same size as everything else".
#:
#: At or below this ratio the warning is meaningfully smaller than its surroundings —
#: Jenny's "smaller font ... burying it in tiny text". It is a detection heuristic for an
#: evasion pattern, not a restatement of the regulation: 16.22's actual size rule is in
#: millimetres and WARN-9 concedes that cannot be measured here.
#:
#: This constant can only ever raise a finding. There is no value of it that converts a
#: violation into a pass, which is why it is not a threshold.
PROMINENCE_CONCERN_RATIO: Final[float] = 0.80


# --------------------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TypographyAssessment:
    """What the typography signals establish, bucketed by what the caller must do.

    The buckets exist so routing is explicit rather than inferred from severity strings.
    A type-style violation and a prominence concern are both findings an agent must read,
    but they lead to different verdicts, and reading that distinction out of a string
    would be the kind of quiet coupling that eventually produces a false pass.
    """

    findings: tuple[Finding, ...] = ()

    #: Bold rules broken. The label does not comply with 16.22 → Mismatch.
    type_style_violations: tuple[str, ...] = ()

    #: Bright lines the extractor abstained on → cannot reach Match; a human looks.
    unconfirmed: tuple[str, ...] = ()

    #: Prominence problems we did detect → Needs review with the region shown (WARN-5).
    prominence_concerns: tuple[str, ...] = ()

    #: Heuristics never assessed. Reported for honesty; changes no verdict (WARN-9).
    unassessed: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        """Nothing broken and nothing left hanging. The only route to Match."""
        return not (
            self.type_style_violations or self.unconfirmed or self.prominence_concerns
        )


def check_header_bold(signals: WarningTypography) -> list[Finding]:
    """WARN-2 — 16.22 requires the words GOVERNMENT WARNING in bold type."""
    match signals.header_is_bold:
        case False:
            return [
                Finding(
                    code="warning_header_not_bold",
                    message=(
                        'The words "GOVERNMENT WARNING" must appear in bold type. '
                        "On this label they do not."
                    ),
                    citation=canon.CITATIONS["warning_format"],
                    severity=SEVERITY_VIOLATION,
                )
            ]
        case None:
            return [
                Finding(
                    code="warning_header_bold_unverified",
                    message=(
                        "Could not tell from this image whether the heading is bold. "
                        "It has not been checked — look at it yourself."
                    ),
                    citation=canon.CITATIONS["warning_format"],
                    severity=SEVERITY_UNVERIFIED,
                )
            ]
        case _:
            return []


def check_body_not_bold(signals: WarningTypography) -> list[Finding]:
    """WARN-7 — the inverse rule. 16.22 requires the remainder *not* be bold.

    Almost every checker gets the heading right and stops there. A label with the whole
    paragraph in bold satisfies "the heading is bold" and still violates 16.22.
    """
    match signals.body_is_bold:
        case True:
            return [
                Finding(
                    code="warning_body_is_bold",
                    message=(
                        'Only the words "GOVERNMENT WARNING" may be bold. The rest of '
                        "the statement must not be in bold type, and on this label it is."
                    ),
                    citation=canon.CITATIONS["warning_format"],
                    severity=SEVERITY_VIOLATION,
                )
            ]
        case None:
            return [
                Finding(
                    code="warning_body_bold_unverified",
                    message=(
                        "Could not tell from this image whether the rest of the "
                        "statement is bold. It has not been checked — look at it "
                        "yourself. Only the heading may be bold."
                    ),
                    citation=canon.CITATIONS["warning_format"],
                    severity=SEVERITY_UNVERIFIED,
                )
            ]
        case _:
            return []


def check_prominence(signals: WarningTypography) -> list[Finding]:
    """WARN-5 / LP-211 — is the warning shrunk relative to the rest of the label?"""
    ratio = signals.relative_size
    if ratio is None:
        return []
    if ratio > PROMINENCE_CONCERN_RATIO:
        return []
    percent = round((1.0 - ratio) * 100)
    return [
        Finding(
            code="warning_less_prominent",
            message=(
                f"The warning is printed about {percent}% smaller than the other text "
                f"on this label. It must be readily legible and set apart from the rest "
                f"of the information — compare it against the surrounding text yourself."
            ),
            citation=canon.CITATIONS["warning_format"],
            severity=SEVERITY_VIOLATION,
        )
    ]


def check_contrast(signals: WarningTypography) -> list[Finding]:
    """WARN-5 / LP-212 — buried text. 16.22 requires a contrasting background."""
    if signals.contrast_ok is False:
        return [
            Finding(
                code="warning_low_contrast",
                message=(
                    "The warning does not stand out from the background behind it. It "
                    "must be readily legible on a contrasting background — check the "
                    "outlined area on the picture."
                ),
                citation=canon.CITATIONS["warning_format"],
                severity=SEVERITY_VIOLATION,
            )
        ]
    return []


def _unassessed_note(names: tuple[str, ...]) -> Finding:
    """One line naming what the tool never looked at, rather than three (WARN-9)."""
    readable = {
        "relative_size": "how the warning's size compares with the rest of the label",
        "contrast_ok": "whether the warning stands out from its background",
    }
    listed = " and ".join(readable.get(n, n) for n in names)
    return Finding(
        code="warning_prominence_unassessed",
        message=(
            f"This check did not assess {listed}. Type size and prominence are not "
            f"measurable from a photograph — judge them by eye."
        ),
        citation=canon.CITATIONS["warning_format"],
        severity=SEVERITY_CONTEXT,
    )


def assess(signals: WarningTypography) -> TypographyAssessment:
    """Every 16.22 appearance rule, in one pass."""
    findings: list[Finding] = []
    violations: list[str] = []
    unconfirmed: list[str] = []
    prominence: list[str] = []
    unassessed: list[str] = []

    for finding in check_header_bold(signals) + check_body_not_bold(signals):
        findings.append(finding)
        if finding.severity == SEVERITY_UNVERIFIED:
            unconfirmed.append(finding.code)
        else:
            violations.append(finding.code)

    for finding in check_prominence(signals) + check_contrast(signals):
        findings.append(finding)
        prominence.append(finding.code)

    if signals.relative_size is None:
        unassessed.append("relative_size")
    if signals.contrast_ok is None:
        unassessed.append("contrast_ok")
    if unassessed:
        findings.append(_unassessed_note(tuple(unassessed)))

    return TypographyAssessment(
        findings=tuple(findings),
        type_style_violations=tuple(violations),
        unconfirmed=tuple(unconfirmed),
        prominence_concerns=tuple(prominence),
        unassessed=tuple(unassessed),
    )


# --------------------------------------------------------------------------------------
# Escalation — a second, stronger look at the warning region
# --------------------------------------------------------------------------------------
#
# The first extraction pass reads the whole label with the fast model. When it cannot
# resolve a bright line, the warning region is re-read on its own, cropped and at full
# resolution, by a stronger model. Two properties make this safe to bolt onto the
# highest-stakes field in the product:
#
#   1. **The trigger is not a threshold.** It fires on abstention or illegibility, not on
#      a confidence number. There is no knob that can quietly stop it firing (WARN-6).
#   2. **The merge cannot manufacture a pass.** See `adopt_reread` — a second opinion can
#      fill a blank, but it can never overwrite a recorded `False`, and two models that
#      disagree produce `None`, not the answer we would prefer.


@dataclass(frozen=True)
class WarningRereadRequest:
    """What the adapter is being asked for.

    `bbox` is the warning region from the first pass, normalized against the preprocessed
    image. When it is None the adapter must re-read the whole image rather than guess a
    crop — a crop that clips the warning is worse than no crop at all (LP-326).
    """

    image_index: int
    bbox: BoundingBox | None = None
    reason: str = ""
    wanted: tuple[str, ...] = ()


@dataclass(frozen=True)
class WarningReread:
    """What the adapter returns.

    Same tri-state contract as the first pass. A stronger model is still allowed — and
    expected — to say "I cannot tell", and it must be able to: an adapter that converts
    its own uncertainty into `True` to look helpful would defeat the entire design.
    """

    warning_text: str | None = None
    typography: WarningTypography | None = None
    model: str = ""


@runtime_checkable
class WarningRereader(Protocol):
    """The interface a provider adapter must implement to serve escalation.

    Deliberately separate from `ExtractionProvider`: this is one region of one image with
    one question attached, not a whole-label extraction, and conflating them would push a
    second full extraction through the five-second budget.
    """

    name: str

    def reread_warning(self, request: WarningRereadRequest) -> WarningReread:
        """Re-read the warning region. Raises `ProviderError` when unusable.

        Never invents a value. Signals it cannot judge come back `None`.
        """
        ...


#: The bright-line signals escalation exists to resolve.
ESCALATION_SIGNALS: Final[tuple[str, ...]] = ("header_is_bold", "body_is_bold")


def unresolved_signals(signals: WarningTypography) -> tuple[str, ...]:
    """Bright lines the extractor abstained on, in declaration order."""
    return tuple(name for name in ESCALATION_SIGNALS if getattr(signals, name) is None)


def needs_escalation(
    signals: WarningTypography,
    *,
    warning_text: str | None = None,
    legible: bool = True,
) -> bool:
    """Should the warning region get a second, stronger look?

    Fires when the warning could not be read at all, or when a bright line is
    unresolved. Not gated on confidence, and not gated on anything in
    `api.rules.thresholds` — the warning statement is exempt from all of it (WARN-6).
    """
    if not legible or warning_text is None or not warning_text.strip():
        return True
    return bool(unresolved_signals(signals))


def escalation_request(
    signals: WarningTypography,
    *,
    image_index: int,
    bbox: BoundingBox | None = None,
    legible: bool = True,
    warning_text: str | None = None,
) -> WarningRereadRequest:
    """Describe the second look, in terms the adapter and a log line can both use."""
    if not legible or warning_text is None or not warning_text.strip():
        reason = "the warning statement could not be read on the first pass"
        wanted: tuple[str, ...] = ("warning_text", *ESCALATION_SIGNALS)
    else:
        unresolved = unresolved_signals(signals)
        reason = "the first pass could not determine " + " or ".join(
            name.replace("_", " ") for name in unresolved
        )
        wanted = unresolved
    return WarningRereadRequest(
        image_index=image_index, bbox=bbox, reason=reason, wanted=wanted
    )


def _combine(first: bool | None, second: bool | None) -> tuple[bool | None, bool]:
    """Merge one tri-state signal. Returns (value, disagreed).

    The whole safety argument of escalation lives in these five lines:

    * A blank is filled by the second opinion.
    * A recorded answer is never overwritten by a blank.
    * Two answers that agree stand.
    * Two answers that **disagree** collapse to `None` — not to the second model's answer
      just because it came from a bigger model, and certainly not to whichever answer
      would let the label through. Two models disagreeing about whether a heading is bold
      is not evidence that it is; it is evidence that a person should look.
    """
    if first is None:
        return second, False
    if second is None or first == second:
        return first, False
    return None, True


def adopt_reread(
    first: WarningTypography,
    reread: WarningReread,
) -> tuple[WarningTypography, tuple[Finding, ...]]:
    """Fold a second look into the first pass, conservatively.

    Returns the merged signals and any findings the disagreement itself produced. A
    disagreement is worth telling the agent about — it is the tool admitting that two
    reads of the same pixels did not agree, which is more useful than either answer.
    """
    second = reread.typography or WarningTypography()
    merged: dict[str, bool | float | None] = {}
    disagreements: list[str] = []

    for name in ("header_is_all_caps", "header_is_bold", "body_is_bold", "contrast_ok"):
        value, disagreed = _combine(getattr(first, name), getattr(second, name))
        merged[name] = value
        if disagreed:
            disagreements.append(name)

    # A ratio is a measurement, not a judgement: keep the first reading unless it is
    # absent, and never average two numbers into one nobody measured.
    merged["relative_size"] = (
        first.relative_size if first.relative_size is not None else second.relative_size
    )

    findings = tuple(
        Finding(
            code="warning_typography_disputed",
            message=(
                f"Two readings of this label disagreed about "
                f"{name.replace('_', ' ')}, so it has not been settled. Look at the "
                f"warning yourself."
            ),
            citation=canon.CITATIONS["warning_format"],
            severity=SEVERITY_UNVERIFIED,
        )
        for name in disagreements
    )
    return WarningTypography(**merged), findings  # type: ignore[arg-type]
