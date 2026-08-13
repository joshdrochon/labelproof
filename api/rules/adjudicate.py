"""Tier 3 — the judgment call, and everything that stops it being one (MATCH-4, MATCH-6).

Tier 1 normalizes. Tier 2 names an explainable variation. What is left is the genuinely
gray case: `Old Tom Distillery` against `Distillery of Old Tom`, `Co.` against `Company`,
a DBA against a registered name. Those are not string problems, and until now they fell
through to Mismatch — the safe direction, and a false rejection every time an applicant
wrote their producer the way the label prints it.

**Four rules, and every one of them exists to stop this tier being a way to pass things.**

**It only ever moves a verdict toward Match, and only from Mismatch.** `adjudicate()`
returns either an ACCEPTABLE_VARIATION or nothing at all. It cannot manufacture a
Mismatch, cannot touch Missing or Unreadable — a value that was not read is not a value to
judge — and cannot upgrade anything to Match. The most it can say is "these differ, and a
person familiar with the field would call the difference immaterial", which is exactly
what Acceptable variation means and is why the agent still sees the row.

**It never runs on the government warning.** 27 CFR 16.21 fixes that text exactly. There
is nothing to adjudicate, and a model asked to judge whether a reworded warning is
"close enough" would eventually say yes. `ADJUDICABLE_FIELDS` is an allowlist, not a
denylist, so a field added to `FieldName` is excluded until someone deliberately adds it.

**It is bounded in time and in money before it is called.** An adjudication that would
overrun the request budget does not start, and one that would take the verification past
its cost cap does not either. Both return the ungraded verdict unchanged rather than
waiting or spending.

**A failure is not a pass.** Any error, timeout, malformed response, or low-confidence
judgement leaves the original Mismatch standing. There is no path through this module
where something going wrong makes a label look better.

The trigger rate is logged on every verification, because a tier that quietly starts
running on everything is a cost problem and a latency problem before anyone notices it is
also an accuracy problem.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from api.models import FieldName, FieldResult, Verdict
from api.rules import thresholds as T

#: Fields a model may be asked to judge. An ALLOWLIST — a new field is excluded until
#: someone adds it here on purpose.
#:
#: The government warning is absent and must stay absent. Its text is fixed by 27 CFR
#: 16.21 and its appearance by 16.22, so there is no judgement to make; asking a model
#: whether a reworded warning is close enough is asking it to eventually say yes.
#: Alcohol content and net contents are absent too — those are numbers with regulated
#: tolerances already computed in `abv.py` and `fills.py`, and a model's opinion about a
#: number we can compare exactly is worse than the comparison.
ADJUDICABLE_FIELDS: Final[frozenset[FieldName]] = frozenset(
    {
        FieldName.BRAND_NAME,
        FieldName.CLASS_TYPE,
        FieldName.PRODUCER,
        FieldName.COUNTRY_OF_ORIGIN,
    }
)

#: Verdicts a gray case can arrive with. Only Mismatch — the others are not gray.
#:
#: Missing and Unreadable are excluded for the same reason: there is no extracted value
#: to judge, and inviting a model to reason about one it cannot see is inviting it to
#: supply one. Match and Acceptable variation are already resolved, and re-opening a
#: settled row can only make it worse.
ADJUDICABLE_VERDICTS: Final[frozenset[Verdict]] = frozenset({Verdict.MISMATCH})

#: Minimum confidence a judgement needs before it is allowed to change anything.
#: Below this the original Mismatch stands — an uncertain "these are the same" is exactly
#: the shape of a false pass.
JUDGEMENT_FLOOR: Final[float] = 0.80


@dataclass(frozen=True)
class AdjudicationRequest:
    """What the judge is told. Deliberately not the whole label.

    The image is absent, and so is every other field. This tier answers one question —
    are these two strings the same thing — and handing it the artwork would invite it to
    re-read the label instead, which is Tier 0's job and is already done.
    """

    field: FieldName
    expected: str
    extracted: str
    commodity: str


@dataclass(frozen=True)
class Judgement:
    """What comes back. `same_thing=False` leaves the Mismatch alone."""

    same_thing: bool
    confidence: float
    rationale: str


class Adjudicator(Protocol):
    """The seam. `api/provider/` implements it; the rules engine only knows this."""

    name: str

    def judge(self, request: AdjudicationRequest) -> Judgement:
        """Decide whether two values name the same thing. Raises on failure."""
        ...


@dataclass(frozen=True)
class Budget:
    """What is left to spend, checked BEFORE a call rather than regretted after.

    `remaining_ms` is the request's own deadline minus what it has already used.
    `remaining_usd` is the per-verification cost cap minus what extraction cost. Either
    at or below zero means the tier does not run, and the ungraded verdict stands.
    """

    remaining_ms: int
    remaining_usd: float

    #: Rough cost of one judgement — a few hundred tokens on the cheap model. Compared
    #: against `remaining_usd` before spending it, so the cap cannot be exceeded by the
    #: call that discovers it.
    estimated_usd: float = 0.0008

    #: Rough wall clock for one judgement. Text-only on Haiku, measured at ~700ms; the
    #: allowance is generous because overrunning the request budget is a 503 and being
    #: conservative here only costs an adjudication nobody was promised.
    estimated_ms: int = 1500

    def allows_one(self) -> bool:
        return (
            self.remaining_ms >= self.estimated_ms
            and self.remaining_usd >= self.estimated_usd
        )


def is_adjudicable(result: FieldResult) -> bool:
    """Whether this row is a gray case at all (LP-221).

    Every condition is a reason NOT to call a model, which is the point: the trigger rate
    is a cost line, and the cheapest adjudication is the one that does not happen. The
    STONE'S THROW case resolves at Tier 2 and must never reach here — a test asserts it.
    """
    if result.field not in ADJUDICABLE_FIELDS:
        return False
    if result.verdict not in ADJUDICABLE_VERDICTS:
        return False
    # Both sides must be present. Judging against an absent value is judging nothing.
    if not (result.expected or "").strip() or not (result.extracted or "").strip():
        return False
    # A confident Mismatch on two obviously different strings is not gray. The trigger is
    # the band where the comparison itself was unsure.
    return result.confidence < T.TIER3_TRIGGER


@dataclass(frozen=True)
class AdjudicationOutcome:
    """The result, plus why it turned out that way. Both halves get logged."""

    results: list[FieldResult]
    considered: int
    judged: int
    changed: int
    skipped_reason: str = ""
    elapsed_ms: int = 0
    usd: float = 0.0


def adjudicate(
    results: list[FieldResult],
    *,
    adjudicator: Adjudicator | None,
    budget: Budget,
    commodity: str,
    clock: Callable[[], float] = time.perf_counter,
) -> AdjudicationOutcome:
    """Run Tier 3 over the gray rows. Never raises, never worsens a verdict.

    `adjudicator=None` — the default everywhere until an adapter is wired — returns the
    results untouched with `skipped_reason="no adjudicator"`, which is what the pipeline
    has been doing implicitly and now says out loud.
    """
    started = clock()

    gray = [r for r in results if is_adjudicable(r)]
    if not gray:
        return AdjudicationOutcome(results=results, considered=0, judged=0, changed=0)
    if adjudicator is None:
        return AdjudicationOutcome(
            results=results,
            considered=len(gray),
            judged=0,
            changed=0,
            skipped_reason="no adjudicator",
        )

    by_field = {r.field: r for r in results}
    judged = changed = 0
    spent = 0.0
    remaining = budget

    for row in gray:
        if not remaining.allows_one():
            return AdjudicationOutcome(
                results=[by_field.get(r.field, r) for r in results],
                considered=len(gray),
                judged=judged,
                changed=changed,
                skipped_reason=(
                    "out of time" if remaining.remaining_ms < remaining.estimated_ms
                    else "over cost cap"
                ),
                elapsed_ms=int((clock() - started) * 1000),
                usd=spent,
            )

        try:
            judgement = adjudicator.judge(
                AdjudicationRequest(
                    field=row.field,
                    expected=row.expected or "",
                    extracted=row.extracted or "",
                    commodity=commodity,
                )
            )
        except Exception:  # noqa: BLE001 - a failed judgement must never become a pass
            judged += 1
            spent += remaining.estimated_usd
            remaining = Budget(
                remaining_ms=remaining.remaining_ms - remaining.estimated_ms,
                remaining_usd=remaining.remaining_usd - remaining.estimated_usd,
            )
            continue

        judged += 1
        spent += remaining.estimated_usd
        remaining = Budget(
            remaining_ms=remaining.remaining_ms - remaining.estimated_ms,
            remaining_usd=remaining.remaining_usd - remaining.estimated_usd,
        )

        if judgement.same_thing and judgement.confidence >= JUDGEMENT_FLOOR:
            by_field[row.field] = _promoted(row, judgement)
            changed += 1

    return AdjudicationOutcome(
        results=[by_field.get(r.field, r) for r in results],
        considered=len(gray),
        judged=judged,
        changed=changed,
        elapsed_ms=int((clock() - started) * 1000),
        usd=spent,
    )


def _promoted(row: FieldResult, judgement: Judgement) -> FieldResult:
    """Mismatch to Acceptable variation, carrying the reasoning (MATCH-5, HITL-4).

    Never to Match. The agent has to see that a model made this call and what it said,
    because the whole product is advisory and this is the one tier where the advice came
    from a judgement rather than from a rule.
    """
    return FieldResult(
        field=row.field,
        verdict=Verdict.ACCEPTABLE_VARIATION,
        extracted=row.extracted,
        expected=row.expected,
        confidence=judgement.confidence,
        rationale=(
            f"Checked by a second model because the wording differs: {judgement.rationale}"
        ),
        tier=3,
        evidence=row.evidence,
        findings=row.findings,
    )
