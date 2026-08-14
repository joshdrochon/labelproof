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

**Bright lines** — the heading is bold (16.22(a)(2)), the body is not bold (a)(2), and
the statement sits on a contrasting background (16.22(a)(1)). Each is stated in the
regulation as a requirement with a yes/no answer. An abstention on any of them is
*unconfirmed*: the field cannot reach Match, and an agent is told to look. Fails closed,
unconditionally.

Contrast reached that list the hard way. It began here as a heuristic on the argument
that abstentions would be common and failing closed on all of them would flood the
Needs-review queue. The argument was checkable and wrong: the spike measured zero
abstentions in sixty signals, so failing closed on contrast holds up approximately
nothing — and in the meantime a verbatim warning with `contrast_ok=None` reached Ready
to approve, which is precisely the evasion the PRD describes as burying the statement in
the artwork.

**Heuristic** — relative size, and only that. The regulation's line for size is an
absolute measurement in millimetres, WARN-9 concedes that is unmeasurable from an
unscaled photograph, and the ratio is a proxy for an evasion pattern rather than a
restatement of any rule. A concerning ratio raises a finding; an abstention says so
plainly and holds nothing up, because the tool never claimed to measure it.

A note on `adopt_reread` and violations. `_combine` collapses a recorded `False` and a
rereader's `True` to `None`, which turns a Mismatch into an Unreadable — a softening, not
a pass. It is unreachable today because `escalation_reason` refuses to fire once a
violation is established, and that refusal is tested; recorded here so the two facts are
known to depend on each other.

A note on the asymmetry in contrast's two answers. `None` blocks Match, while `False`
routes to Needs review rather than to Mismatch. That is not timidity: exposure, white
balance and compression all change how much a background appears to contrast, so a
*detected* contrast failure is not something this tool should assert against an
applicant — PRD TC-06 says as much, asking for Needs review. Both answers end in front of
a person, which is where a question a photograph cannot settle belongs.

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

#: Below THIS, the finding asserts non-compliance and changes the verdict. Between the
#: two it is reported and decides nothing.
#:
#: Two bands, because `relative_size` is a MODEL'S ESTIMATE and not a measurement, and 23
#: real labels made that impossible to ignore. The same photograph scored 0.5 on one run
#: and 0.6 on the next; a compliant label moved 0.6 to 0.8. Across the set, warnings
#: confirmed by eye as genuinely buried — one rotated ninety degrees in tiny type —
#: landed in the same range as warnings that were perfectly legible and simply smaller
#: than the brand name, which is true of every label ever printed.
#:
#: A single cut therefore cannot separate them, and moving it would be fitting a
#: compliance verdict to noise. So the band where the estimate is unreliable now informs
#: the agent without demoting the row, and only an unambiguous reading still asserts.
#: 16.22's real rule is in millimetres, WARN-9 already concedes those cannot be measured
#: from an unscaled photograph, and `warning_type_size_not_verified` has always been
#: context for exactly that reason. This is the same admission applied to the same
#: regulation.
#:
#: Neither band can turn a violation into a pass: below the floor the finding asserts as
#: it always did, and above it the row keeps whatever verdict its WORDING and TYPOGRAPHY
#: earned — caps, bold and text are bright lines and are untouched by this.
PROMINENCE_ASSERTS_RATIO: Final[float] = 0.50


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
    if not size_was_measured(signals):
        return []
    ratio = signals.relative_size
    assert ratio is not None  # noqa: S101 - narrowing only; size_was_measured ruled None out
    if ratio > PROMINENCE_CONCERN_RATIO:
        return []
    percent = round((1.0 - ratio) * 100)
    asserts = ratio <= PROMINENCE_ASSERTS_RATIO
    return [
        Finding(
            code="warning_less_prominent",
            # Says only what the ratio supports. An earlier draft added "and set apart
            # from the rest of the information", which is 16.21's separate-and-apart
            # rule — a requirement `warning.LIMITS` explicitly declares unchecked. A
            # message must not claim a check the same module disclaims.
            message=(
                f"The warning is printed about {percent}% smaller than the other text "
                f"on this label. It must be readily legible — compare it against the "
                f"surrounding text yourself."
            ),
            citation=canon.CITATIONS["warning_format"],
            severity=SEVERITY_VIOLATION if asserts else SEVERITY_CONTEXT,
        )
    ]


def size_is_concerning(ratio: float | None) -> bool:
    """Is this a measured ratio that says the warning is meaningfully smaller?"""
    return ratio is not None and 0.0 < ratio <= PROMINENCE_CONCERN_RATIO


def size_was_measured(signals: WarningTypography) -> bool:
    """Did the reading actually produce a size ratio?

    `None` is the obvious no. So is a ratio outside a plausible range: a warning cannot
    be a negative size, and a hundredfold difference is not something a label does, so
    those are broken readings rather than measurements. `0.0` matters most — it is a
    plausible "could not measure" output from a model asked for a number, and treating it
    as a measurement meant a label reached Match with neither a prominence finding nor
    the note admitting size went unassessed. Silence in both directions.
    """
    ratio = signals.relative_size
    return ratio is not None and 0.0 < ratio < 100.0


def check_contrast(signals: WarningTypography) -> list[Finding]:
    """WARN-5 / LP-212 — buried text. 16.22(a)(1) requires a contrasting background.

    A stated requirement with a yes/no answer, so an abstention blocks Match. A detected
    failure goes to a person rather than back to the applicant, because exposure and
    compression both change how much a background appears to contrast — see the module
    docstring, and PRD TC-06.
    """
    match signals.contrast_ok:
        case False:
            return [
                Finding(
                    code="warning_low_contrast",
                    message=(
                        "The warning does not stand out from the background behind it. "
                        "It must be readily legible on a contrasting background — check "
                        "the outlined area on the picture."
                    ),
                    citation=canon.CITATIONS["warning_format"],
                    severity=SEVERITY_VIOLATION,
                )
            ]
        case None:
            return [
                Finding(
                    code="warning_contrast_unverified",
                    message=(
                        "Could not tell from this image whether the warning stands out "
                        "from the background behind it. It has not been checked — look "
                        "at it yourself."
                    ),
                    citation=canon.CITATIONS["warning_format"],
                    severity=SEVERITY_UNVERIFIED,
                )
            ]
        case _:
            return []


def _unassessed_note() -> Finding:
    """WARN-9, in one line: the tool did not judge the warning's size."""
    return Finding(
        code="warning_prominence_unassessed",
        message=(
            "This check did not assess how the warning's size compares with the rest of "
            "the label. Type size and prominence are not measurable from a photograph — "
            "judge them by eye."
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

    for finding in check_contrast(signals):
        findings.append(finding)
        # A detected failure is a prominence concern (Needs review); an abstention is an
        # unresolved bright line. Both block Match; only one asserts anything.
        if finding.severity == SEVERITY_UNVERIFIED:
            unconfirmed.append(finding.code)
        else:
            prominence.append(finding.code)

    for finding in check_prominence(signals):
        findings.append(finding)
        prominence.append(finding.code)

    if not size_was_measured(signals):
        unassessed.append("relative_size")
        findings.append(_unassessed_note())

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
#   1. **The trigger is not a threshold.** There is no knob that can quietly stop it
#      firing (WARN-6).
#   2. **The merge cannot manufacture a pass.** See `adopt_reread` — a second opinion can
#      fill a blank, but it can never overwrite a recorded `False`, and two models that
#      disagree produce `None`, not the answer we would prefer.
#
# **What it fires on, and why that changed.** The first version escalated on abstention
# and on unreadable text. That aimed the safety net at the wrong failure. The spike
# measured the fast model abstaining zero times in sixty signals and answering wrongly
# several times, in the direction of compliance — so a net strung across abstentions
# would have caught nothing at all, while the confident wrong answers it exists to catch
# sailed underneath it.
#
# So escalation fires on the *pass*. If the warning is about to be reported as compliant
# on the strength of a model's opinion about pixels, that opinion gets a second reading
# from a stronger model. If a violation has already been established, it does not fire:
# the verdict cannot get better (the merge refuses to clear a violation) so the call
# would buy nothing.
#
# This costs one extra cropped-region call per otherwise-clean warning, and that is the
# point rather than a regrettable side effect. It is the only field in the product with a
# zero-false-pass release gate, and the cost is bounded — one small region, not a second
# whole-label extraction.


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


#: The bright-line signals escalation exists to resolve — the three 16.22(a) rules whose
#: answers can decide a pass. `relative_size` is not among them: a stronger model cannot
#: measure millimetres either, so re-asking would buy an opinion, not an answer (WARN-9).
ESCALATION_SIGNALS: Final[tuple[str, ...]] = (
    "header_is_bold",
    "body_is_bold",
    "contrast_ok",
)


def unresolved_signals(signals: WarningTypography) -> tuple[str, ...]:
    """Bright lines the extractor abstained on, in declaration order."""
    return tuple(name for name in ESCALATION_SIGNALS if getattr(signals, name) is None)


def escalation_reason(
    signals: WarningTypography,
    *,
    warning_text: str | None = None,
    legible: bool = True,
) -> str | None:
    """Why this warning needs a second, stronger reading — or None if it does not.

    The returned string is written to the log and is the audit trail for why the money
    was spent, so it has to be true. An earlier version returned "about to be reported as
    compliant" for a label with an established text mismatch, which is the opposite of
    what was happening.

    None of the triggers is a confidence number, and none is reachable from
    `api.rules.thresholds`; the warning statement is exempt from all of it (WARN-6).
    """
    # Imported at call time: `warning` imports this module. Same reason as `_merge_text`.
    from api.rules.warning import is_verbatim

    if not legible or warning_text is None or not warning_text.strip():
        return "the warning statement could not be read on the first pass"

    if not is_verbatim(warning_text):
        # The words already settle it. This label is going back to the applicant however
        # bold its heading turns out to be, and no reading of the type styling changes
        # that — so the call buys nothing but cost and a misleading log line. Checking
        # only the typography here meant every return-for-correction application paid
        # for a second look it could not use.
        return None

    look = assess(signals)
    if look.type_style_violations or look.prominence_concerns:
        # Also already established. `adopt_reread` refuses to overwrite a recorded
        # violation, so a second reading cannot clear this either.
        return None

    if unresolved := unresolved_signals(signals):
        return "the first pass could not determine " + " or ".join(
            name.replace("_", " ") for name in unresolved
        )

    if signals.header_is_all_caps is False:
        # The returned text reads in capitals — it is verbatim — and the reading of the
        # image says the heading is not capitalised. Exactly the case where an extractor
        # may have tidied the statement before handing it back, and the one dispute a
        # stronger look can actually settle.
        return (
            "the reading of the image disagrees with the returned wording about the "
            "heading's capitals"
        )

    return (
        "the warning is about to be reported as compliant on the strength of the type "
        "styling read from the image"
    )


def needs_escalation(
    signals: WarningTypography,
    *,
    warning_text: str | None = None,
    legible: bool = True,
) -> bool:
    """Should the warning region get a second, stronger look?

    Fires on the pass, not only on the abstention. See the section comment above: the
    measured failure of the fast model is confident wrongness in the direction of
    compliance, so a net strung across abstentions alone would catch nothing.
    """
    return (
        escalation_reason(signals, warning_text=warning_text, legible=legible) is not None
    )


def escalation_request(
    signals: WarningTypography,
    *,
    image_index: int,
    bbox: BoundingBox | None = None,
    legible: bool = True,
    warning_text: str | None = None,
) -> WarningRereadRequest:
    """Describe the second look, in terms the adapter and a log line can both use.

    `wanted` has to be able to answer the question `reason` asks. It used to be the three
    bright lines regardless, so a request fired over a disputed `header_is_all_caps`
    asked for three signals, none of them the one in dispute — a call that structurally
    could not settle what it was made for.
    """
    reason = escalation_reason(signals, warning_text=warning_text, legible=legible) or ""
    if not legible or warning_text is None or not warning_text.strip():
        wanted: tuple[str, ...] = ("warning_text", *ESCALATION_SIGNALS)
    elif signals.header_is_all_caps is False:
        wanted = ("header_is_all_caps", *unresolved_signals(signals))
    else:
        # Re-ask for everything still open, and for everything the pass is resting on.
        wanted = unresolved_signals(signals) or ESCALATION_SIGNALS
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


@dataclass(frozen=True)
class MergedReading:
    """A first pass and a second look, folded together."""

    typography: WarningTypography
    warning_text: str | None = None
    findings: tuple[Finding, ...] = ()


def adopt_reread(
    first: WarningTypography,
    reread: WarningReread,
    *,
    first_text: str | None = None,
) -> MergedReading:
    """Fold a second look into the first pass, conservatively.

    Returns the merged signals, the text to judge the label on, and any findings the
    disagreement itself produced. A disagreement is worth telling the agent about — it is
    the tool admitting that two reads of the same pixels did not agree, which is more
    useful than either answer.
    """
    second = reread.typography or WarningTypography()
    merged: dict[str, bool | float | None] = {}
    disagreements: list[str] = []

    for name in ("header_is_all_caps", "header_is_bold", "body_is_bold", "contrast_ok"):
        value, disagreed = _combine(getattr(first, name), getattr(second, name))
        merged[name] = value
        if disagreed:
            disagreements.append(name)

    # A ratio is a measurement, not a judgement, so the two are not averaged into a
    # number nobody took. The smaller one wins: the same asymmetry the booleans use,
    # pointing the same way. Keeping the first reading would have let a stronger model's
    # 0.4 be discarded in favour of a weaker model's 1.0, which runs permissive.
    sizes = [
        size
        for size in (first.relative_size, second.relative_size)
        if size is not None
    ]
    merged["relative_size"] = min(sizes) if sizes else None

    findings = [
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
    ]

    # **Size needs a finding of its own, and this is why.** Every other merged signal
    # reaches the verdict twice: once through the merged value, and once through a
    # finding raised here. `relative_size` reached it only through the merged value, so
    # the single assignment that adopts the merge was the only thing holding the
    # prominence check up — delete that one line and a second look measuring the warning
    # 70% smaller came back Match. A second, independent path is the fix; another test
    # would only have pinned the one instance.
    if size_is_concerning(second.relative_size) and not size_is_concerning(
        first.relative_size
    ):
        findings.append(
            Finding(
                code="warning_size_disputed",
                message=(
                    "A second reading measured the warning as noticeably smaller than "
                    "the rest of the label, and the first did not. Its size has not "
                    "been settled — compare it against the surrounding text yourself."
                ),
                citation=canon.CITATIONS["warning_format"],
                severity=SEVERITY_UNVERIFIED,
            )
        )

    text, text_finding = _merge_text(first_text, reread.warning_text)
    if text_finding is not None:
        findings.append(text_finding)

    return MergedReading(
        typography=WarningTypography(**merged),  # type: ignore[arg-type]
        warning_text=text,
        findings=tuple(findings),
    )


def _merge_text(first: str | None, second: str | None) -> tuple[str | None, Finding | None]:
    """Which reading of the words to judge the label on.

    The case escalation exists for: the first pass could not read the statement, the
    stronger model could, and that reading turns an Unreadable into a real verdict.

    Everything else is conservative. A second reading never replaces a first one that
    already had words in it, because "the stronger model saw different words" is not
    evidence about the label, it is evidence that one of the two reads is wrong — and
    that goes to a person.
    """
    # Imported at call time: `warning` imports this module, and the collapse rule has one
    # home. Duplicating a normalizer this load-bearing is how the two quietly diverge.
    from api.rules.warning import collapse_layout_whitespace

    if first is None or not first.strip():
        return second, None
    if second is None or not second.strip():
        return first, None
    if collapse_layout_whitespace(first) == collapse_layout_whitespace(second):
        return first, None
    return first, Finding(
        code="warning_text_disputed",
        message=(
            "Two readings of this label produced different wording for the warning "
            "statement, so what it says has not been settled. Read the warning on the "
            "picture yourself before deciding."
        ),
        citation=canon.CITATIONS["warning_text"],
        severity=SEVERITY_UNVERIFIED,
    )
