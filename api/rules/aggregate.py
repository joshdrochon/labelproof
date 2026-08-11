"""Aggregate recommendation — rolling per-field verdicts into one piece of advice.

Two rules govern this module:

**Worst-of.** The recommendation is driven by the most serious field verdict. One bad
field is enough; nine good ones do not dilute it.

**The warning statement ranks first.** When several fields are equally serious, the
government warning is the one named and the one shown at the top. It is the only field
whose absence is, on its own, disqualifying (WARN-6, MATCH-10).

The app recommends and never decides (HITL-1, SCOPE-3). Every string produced here is
phrased as advice to an agent who will make the actual determination.
"""

from __future__ import annotations

from api.models import (
    Aggregate,
    FieldName,
    FieldResult,
    Recommendation,
    Verdict,
)

#: How serious each verdict is, ascending. Used only for comparison, never displayed.
_SEVERITY: dict[Verdict, int] = {
    Verdict.NOT_APPLICABLE: 0,
    Verdict.MATCH: 0,
    Verdict.ACCEPTABLE_VARIATION: 1,
    Verdict.UNREADABLE: 2,
    Verdict.MISMATCH: 3,
    Verdict.MISSING: 4,
}

#: Verdicts on the government warning that force Return for correction. A warning that is
#: absent or wrong is disqualifying on its own; the rest of the label does not matter.
_WARNING_DISQUALIFYING: frozenset[Verdict] = frozenset(
    {Verdict.MISSING, Verdict.MISMATCH}
)

#: Verdicts on any required field that force Return for correction when the element is
#: simply not on the label at all.
_ABSENT: frozenset[Verdict] = frozenset({Verdict.MISSING})


def _warning(results: list[FieldResult]) -> FieldResult | None:
    return next(
        (r for r in results if r.field is FieldName.GOVERNMENT_WARNING), None
    )


def triage_order(results: list[FieldResult]) -> list[FieldResult]:
    """Sort for display: warning first, then most serious, then canonical field order.

    Used by both the single-verification checklist and the batch triage table, so a row
    means the same thing in both places.
    """
    field_order = {f: i for i, f in enumerate(FieldName)}
    return sorted(
        results,
        key=lambda r: (
            r.field is not FieldName.GOVERNMENT_WARNING,
            -_SEVERITY[r.verdict],
            field_order[r.field],
        ),
    )


def recommend(results: list[FieldResult]) -> Aggregate:
    """Roll per-field verdicts into one recommendation."""
    if not results:
        return Aggregate(
            recommendation=Recommendation.NEEDS_REVIEW,
            rationale="No fields were checked. Nothing has been verified.",
            driving_field=None,
        )

    warning = _warning(results)

    # 1. The warning statement, on its own terms.
    if warning is not None and warning.verdict in _WARNING_DISQUALIFYING:
        detail = (
            "no government warning statement was found"
            if warning.verdict is Verdict.MISSING
            else "the government warning statement does not match the required text"
        )
        return Aggregate(
            recommendation=Recommendation.RETURN_FOR_CORRECTION,
            rationale=(
                f"Recommend returning this application for correction because {detail}. "
                f"The final decision is yours."
            ),
            driving_field=FieldName.GOVERNMENT_WARNING,
        )

    # 2. A required element simply absent from the label.
    if absent := [r for r in results if r.verdict in _ABSENT]:
        first = triage_order(absent)[0]
        return Aggregate(
            recommendation=Recommendation.RETURN_FOR_CORRECTION,
            rationale=(
                "Recommend returning this application for correction because a "
                "required element is not on the label. The final decision is yours."
            ),
            driving_field=first.field,
        )

    # 3. Anything that needs a human to look, in descending seriousness.
    attention = [r for r in results if _SEVERITY[r.verdict] > 0]
    if attention:
        first = triage_order(attention)[0]
        count = len(attention)
        noun = "row needs" if count == 1 else "rows need"
        return Aggregate(
            recommendation=Recommendation.NEEDS_REVIEW,
            rationale=(
                f"{count} {noun} your eyes. Everything else checks out. "
                f"The final decision is yours."
            ),
            driving_field=first.field,
        )

    # 4. Everything matched or did not apply.
    return Aggregate(
        recommendation=Recommendation.READY_TO_APPROVE,
        rationale=(
            "Every required field on the label matches the application. "
            "The final decision is yours."
        ),
        driving_field=None,
    )


def attention_fields(results: list[FieldResult]) -> list[FieldResult]:
    """The subset an agent actually has to look at, in triage order.

    Lets the UI quiet the rows that need nothing — five Match rows carrying the same
    visual weight as the one Mismatch is what buries a finding.
    """
    return triage_order([r for r in results if _SEVERITY[r.verdict] > 0])
