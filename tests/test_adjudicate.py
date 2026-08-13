"""Tier 3 (MATCH-4, MATCH-6, LP-219 … LP-232).

This tier is the one place in the product where a verdict comes from a judgement rather
than from a rule, so most of what is asserted here is what it CANNOT do. A tier that can
only move a row toward Match is a tier that can only produce false passes if it is wrong,
which is why every guard below is written as a refusal.
"""

from __future__ import annotations

import pytest

from api.models import Evidence, FieldName, FieldResult, Verdict
from api.provider.fake import FailingAdjudicator, ScriptedAdjudicator, SlowAdjudicator
from api.rules import adjudicate as adj
from api.rules import thresholds as T

GENEROUS = adj.Budget(remaining_ms=10_000, remaining_usd=1.0)


def row(
    field: FieldName = FieldName.PRODUCER,
    verdict: Verdict = Verdict.MISMATCH,
    *,
    expected: str = "Old Tom Distillery",
    extracted: str = "Distillery of Old Tom",
    confidence: float = 0.60,
) -> FieldResult:
    return FieldResult(
        field=field,
        verdict=verdict,
        extracted=extracted,
        expected=expected,
        confidence=confidence,
        rationale="",
        evidence=Evidence(image_index=0, bbox=None),
    )


def scripted(same: bool = True, confidence: float = 0.95) -> ScriptedAdjudicator:
    return ScriptedAdjudicator(
        {
            ("Old Tom Distillery", "Distillery of Old Tom"): (
                same,
                confidence,
                "Both name the same distillery; the label reverses the word order.",
            )
        }
    )


def run(results: list[FieldResult], **kw: object) -> adj.AdjudicationOutcome:
    options: dict[str, object] = {
        "adjudicator": scripted(),
        "budget": GENEROUS,
        "commodity": "spirits",
    }
    options.update(kw)
    return adj.adjudicate(results, **options)  # type: ignore[arg-type]


# --- what it does -----------------------------------------------------------------------


def test_a_reordered_producer_becomes_an_acceptable_variation() -> None:
    """LP-227. The case the tier exists for.

    `Old Tom Distillery` against `Distillery of Old Tom` is not a string problem. Tiers 1
    and 2 correctly refuse it, and before this module it fell through to Mismatch — a
    false rejection every time an applicant wrote the producer the way the label prints it.
    """
    outcome = run([row()])

    assert outcome.results[0].verdict is Verdict.ACCEPTABLE_VARIATION
    assert outcome.results[0].tier == 3
    assert outcome.changed == 1


def test_the_reasoning_reaches_the_agent(   ) -> None:
    """MATCH-5, HITL-4. A judged row must say a model judged it, and what it said.

    This is the only tier whose answer came from an opinion, and the product is advisory.
    An agent who cannot tell a judged row from a computed one cannot weigh it.
    """
    rationale = run([row()]).results[0].rationale

    assert "second model" in rationale
    assert "reverses the word order" in rationale


def test_it_promotes_to_acceptable_variation_and_never_to_match() -> None:
    """The ceiling. Match means the label and the application say the same thing.

    The most this tier can honestly say is that the difference looks immaterial, which is
    what Acceptable variation means — and it keeps the row in front of the agent, which
    Match would not.
    """
    for confidence in (0.80, 0.95, 1.0):
        outcome = run([row()], adjudicator=scripted(confidence=confidence))
        assert outcome.results[0].verdict is Verdict.ACCEPTABLE_VARIATION


# --- what it refuses to do ---------------------------------------------------------------


def test_the_government_warning_is_never_adjudicated() -> None:
    """The single most important assertion in this file.

    27 CFR 16.21 fixes the text exactly, so there is nothing to judge — and a model asked
    whether a reworded warning is close enough will eventually say yes. `ADJUDICABLE_FIELDS`
    is an allowlist so that a field added to `FieldName` is excluded until someone adds it
    deliberately, rather than included until someone remembers to exclude it.
    """
    judge = scripted()
    outcome = adj.adjudicate(
        [row(field=FieldName.GOVERNMENT_WARNING)],
        adjudicator=judge,
        budget=GENEROUS,
        commodity="spirits",
    )

    assert FieldName.GOVERNMENT_WARNING not in adj.ADJUDICABLE_FIELDS
    assert judge.calls == [], "the warning was sent to a model"
    assert outcome.results[0].verdict is Verdict.MISMATCH


@pytest.mark.parametrize(
    "field", [FieldName.ALCOHOL_CONTENT, FieldName.NET_CONTENTS, FieldName.GOVERNMENT_WARNING]
)
def test_fields_with_computable_answers_are_not_sent_to_a_model(field: FieldName) -> None:
    """A model's opinion about a number we can compare exactly is worse than the comparison.

    ABV has a regulated tolerance in `abv.py` and net contents has a standards-of-fill
    table in `fills.py`. Both produce an exact answer, and asking a model to second-guess
    one is how a tolerance becomes negotiable.
    """
    assert not adj.is_adjudicable(row(field=field))


@pytest.mark.parametrize("verdict", [Verdict.MISSING, Verdict.UNREADABLE])
def test_a_row_with_no_value_read_is_never_judged(verdict: Verdict) -> None:
    """Missing and Unreadable have nothing to adjudicate.

    There is no extracted value, so inviting a model to reason about the row is inviting
    it to supply one — which is the fabrication path the whole product is built to close.
    """
    judge = scripted()
    adj.adjudicate(
        [row(verdict=verdict, extracted="")],
        adjudicator=judge,
        budget=GENEROUS,
        commodity="spirits",
    )
    assert judge.calls == []


@pytest.mark.parametrize("verdict", [Verdict.MATCH, Verdict.ACCEPTABLE_VARIATION])
def test_a_settled_row_is_not_reopened(verdict: Verdict) -> None:
    """Re-opening a resolved row can only make it worse, and costs money to do it."""
    assert not adj.is_adjudicable(row(verdict=verdict))


def test_a_confident_mismatch_is_not_gray(   ) -> None:
    """LP-229, the cost-discipline guard.

    Two obviously different strings compared with confidence are not a judgement call.
    The trigger is the band where the comparison itself was unsure, and a tier that runs
    on every mismatch is a cost line and a latency line before it is anything else.
    """
    assert not adj.is_adjudicable(row(confidence=T.TIER3_TRIGGER))
    assert not adj.is_adjudicable(row(confidence=0.99))
    assert adj.is_adjudicable(row(confidence=T.TIER3_TRIGGER - 0.01))


def test_stones_throw_never_reaches_tier_three() -> None:
    """LP-229. TC-02 resolves at Tier 2 and must not cost a model call.

    Asserted through the real comparator rather than by constructing a row, because the
    claim is about what Tier 2 does, not about what this module would do if Tier 2 failed.
    """
    from api.models import ExtractedField
    from api.rules import compare

    extracted = ExtractedField(
        field=FieldName.BRAND_NAME,
        value="STONE'S THROW",
        legible=True,
        confidence=0.97,
    )
    result = compare.compare_brand_name(extracted, "Stone's Throw")

    # Acceptable variation, at tier 2, with the difference named — which is what the PRD
    # asks for. My first version of this asserted Match and was wrong about the product,
    # not about the code: MATCH-2 wants the casing difference SHOWN, not folded away.
    assert result.verdict is Verdict.ACCEPTABLE_VARIATION
    assert result.tier == 2
    assert not adj.is_adjudicable(result), "resolved at tier 2 — must not cost a model call"


# --- failure is never a pass --------------------------------------------------------------


def test_an_adjudicator_that_raises_leaves_the_mismatch_standing() -> None:
    judge = FailingAdjudicator()
    outcome = adj.adjudicate(
        [row()], adjudicator=judge, budget=GENEROUS, commodity="spirits"
    )

    assert judge.calls == 1
    assert outcome.results[0].verdict is Verdict.MISMATCH
    assert outcome.changed == 0


def test_a_judgement_below_the_floor_changes_nothing() -> None:
    """An uncertain "these are the same" is the exact shape of a false pass."""
    outcome = run([row()], adjudicator=scripted(confidence=adj.JUDGEMENT_FLOOR - 0.01))

    assert outcome.results[0].verdict is Verdict.MISMATCH
    assert outcome.judged == 1 and outcome.changed == 0


def test_a_negative_judgement_changes_nothing() -> None:
    outcome = run([row()], adjudicator=scripted(same=False))
    assert outcome.results[0].verdict is Verdict.MISMATCH


def test_an_unscripted_pair_defaults_to_leaving_the_row_alone() -> None:
    """The fake's default matters: a test that forgets to script a case must fail by NOT
    adjudicating rather than by silently passing something."""
    outcome = run([row(expected="Something", extracted="Else Entirely")])
    assert outcome.results[0].verdict is Verdict.MISMATCH


# --- budgets ------------------------------------------------------------------------------


def test_it_does_not_start_when_there_is_no_time_left() -> None:
    """PERF-1. An adjudication that would overrun the request budget does not begin.

    Checked BEFORE the call, so the deadline cannot be blown by the call that discovers
    it. Overrunning is a 503; declining costs an adjudication nobody was promised.
    """
    judge = scripted()
    outcome = adj.adjudicate(
        [row()],
        adjudicator=judge,
        budget=adj.Budget(remaining_ms=100, remaining_usd=1.0),
        commodity="spirits",
    )

    assert judge.calls == []
    assert outcome.skipped_reason == "out of time"
    assert outcome.results[0].verdict is Verdict.MISMATCH


def test_it_does_not_start_when_the_cost_cap_is_reached() -> None:
    """OPS-4. Same shape, for money."""
    judge = scripted()
    outcome = adj.adjudicate(
        [row()],
        adjudicator=judge,
        budget=adj.Budget(remaining_ms=10_000, remaining_usd=0.0),
        commodity="spirits",
    )

    assert judge.calls == []
    assert outcome.skipped_reason == "over cost cap"


def test_the_budget_runs_down_by_what_the_calls_actually_took() -> None:
    """Two gray rows, a judge that genuinely takes time, and room for one.

    This asserted the ESTIMATE before, and that was the bug: the loop decremented by
    `estimated_ms` however long the call really took, so a judge slower than the estimate
    overran the deadline it was meant to respect. With the shipped 6s adapter timeout and
    four gray rows, Tier 3 could spend 24 seconds inside a budget it believed was 6 — and
    on `/verify` that discards a successful extraction and tells the agent the label was
    not verified.

    A fake clock rather than real sleeping: the property is that ELAPSED time is charged,
    and sleeping to prove it would make the suite slower to say the same thing.
    """
    ticks = iter([0.0, 0.0, 1.2, 1.2, 1.2, 1.2, 1.2])
    judge = ScriptedAdjudicator()
    outcome = adj.adjudicate(
        [row(), row(field=FieldName.CLASS_TYPE)],
        adjudicator=judge,
        budget=adj.Budget(remaining_ms=2_000, remaining_usd=1.0),
        commodity="spirits",
        clock=lambda: next(ticks),
    )

    # One call took 1,200ms of a 2,000ms budget, leaving 800 — under the 1,500 a second
    # judgement is allowed to need, so the second row is declined rather than attempted.
    assert len(judge.calls) == 1
    assert outcome.considered == 2 and outcome.judged == 1
    assert outcome.skipped_reason == "out of time"


def test_a_callers_estimates_survive_the_first_row() -> None:
    """`Budget` was rebuilt rather than replaced, so any estimate the CALLER set reverted
    to the class default after one judgement — silently, and only on multi-row inputs."""
    ticks = iter([0.0] * 12)
    judge = ScriptedAdjudicator()
    adj.adjudicate(
        [row(), row(field=FieldName.CLASS_TYPE), row(field=FieldName.BRAND_NAME)],
        adjudicator=judge,
        budget=adj.Budget(
            remaining_ms=600, remaining_usd=1.0, estimated_ms=150, estimated_usd=0.0001
        ),
        commodity="spirits",
        clock=lambda: next(ticks),
    )

    # 600ms at 150ms an estimate is room for all three. With the defaults restored after
    # row one it would have stopped at one.
    assert len(judge.calls) == 3


def test_a_slow_adjudicator_is_still_only_called_within_budget() -> None:
    """The guard is the budget, not a timeout on the call — a slow judge is the adapter's
    problem, and this asserts the tier does not invite one it cannot afford."""
    judge = SlowAdjudicator(delay_s=0.0)
    adj.adjudicate(
        [row()],
        adjudicator=judge,
        budget=adj.Budget(remaining_ms=0, remaining_usd=1.0),
        commodity="spirits",
    )
    assert judge.calls == 0


# --- the default, and the accounting -----------------------------------------------------


def test_no_adjudicator_is_a_stated_skip_rather_than_a_silent_one() -> None:
    """What the pipeline does today, said out loud.

    Before this module the fall-through to Mismatch was implicit, which meant "no gray
    cases" and "no adjudicator wired" looked identical in a log.
    """
    outcome = adj.adjudicate(
        [row()], adjudicator=None, budget=GENEROUS, commodity="spirits"
    )

    assert outcome.skipped_reason == "no adjudicator"
    assert outcome.considered == 1 and outcome.judged == 0
    assert outcome.results[0].verdict is Verdict.MISMATCH


def test_the_trigger_rate_is_countable() -> None:
    """LP-221. A tier that quietly starts running on everything is a cost problem before
    anyone notices it is also an accuracy problem, so the counts are part of the result."""
    outcome = run([row(), row(field=FieldName.CLASS_TYPE), row(verdict=Verdict.MATCH)])

    assert outcome.considered == 2
    assert outcome.judged == 2
    assert outcome.usd > 0


def test_rows_it_never_touched_come_back_unchanged() -> None:
    """Identity, not reconstruction: a row outside the tier must be the same object's
    values, not a rebuilt approximation of them."""
    warning_row = row(field=FieldName.GOVERNMENT_WARNING)
    outcome = run([warning_row, row()])

    assert outcome.results[0] == warning_row


@pytest.mark.parametrize(
    ("expected", "extracted"),
    [("", "Distillery of Old Tom"), ("Old Tom Distillery", ""), ("   ", "  ")],
)
def test_a_row_missing_either_side_is_not_a_judgement_call(
    expected: str, extracted: str
) -> None:
    """Both halves have to exist before "are these the same thing" is even a question.

    An empty EXPECTED is the case worth naming: the application left the field blank, the
    label carries something, and the comparison is a Mismatch. Asking a model to judge a
    value against nothing invites it to decide the blank was fine.
    """
    assert not adj.is_adjudicable(row(expected=expected, extracted=extracted))
