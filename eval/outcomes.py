"""Scoring — what the harness measured, with no opinion about how to print it.

Two ideas carry this module.

**A field outcome is expected-versus-actual plus the findings that had to ride along.**
TC-09's verdict is Match — the label agrees with the application — and the entire point
of the case is the proof inconsistency raised beside it. Scoring verdicts alone would
mark that fixture correct while the defect went undetected, so a missing expected finding
is a failure exactly like a wrong verdict.

**Warning rows are counted separately, and counted twice over.** The release gate is zero
false passes on government-warning violations (OPS-3). That number is only meaningful
next to how many violation rows were actually scored: `0 false passes out of 0 checks` is
arithmetically true and tells you nothing, and it is precisely what a bad filter or a
dropped catalog entry produces. So the report tracks warning coverage as its own
condition and a run that checked no violations does not pass.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

# `dataclasses.field` is spelled out rather than imported: `FieldOutcome` has an
# attribute called `field`, and the bare import would be shadowed inside the class body.
from dataclasses import dataclass
from dataclasses import field as dc_field

from api.models import Application, FieldName, Verdict
from api.provider.base import ExtractionProvider, ImageInput
from api.provider.fake import SpecBackedProvider
from api.verify import verify
from fixtures.generator.catalog import (
    MUST_DECLARE_WARNING_VIOLATION,
    REQUIRED_WARNING_VIOLATIONS,
    misrendered_warning_fixtures,
    undeclared_warning_violations,
)
from fixtures.generator.spec import LabelSpec

#: Verdicts that mean "this field is fine". A warning row expected to be MISMATCH or
#: MISSING that lands on one of these is a false pass — the worst outcome this product
#: can produce (PRD §What it must never do).
PASSING: frozenset[Verdict] = frozenset({Verdict.MATCH, Verdict.NOT_APPLICABLE})

#: Field-level accuracy floor (OPS-3, PRD §Performance Requirements).
ACCURACY_FLOOR = 0.95


@dataclass
class FieldOutcome:
    """One field of one fixture: what was expected, what came back."""

    fixture: str
    field: FieldName
    expected: Verdict
    actual: Verdict
    missing_findings: list[str] = dc_field(default_factory=list)
    pending: str = ""
    """Ticket this outcome is waiting on. Reported, never counted as a failure."""

    pinned_violation: bool = False
    """The repository pins this fixture as a warning violation, whatever `expect` says.

    Without this, deleting `expect` demoted a live false pass from exit 3 to exit 5 —
    non-zero, so not a bypass, but "coverage shortfall" is the wrong page for "a label
    that violates 27 CFR 16.21 was reported compliant". The pin is the authority; `expect`
    only adds to it.
    """

    @property
    def correct(self) -> bool:
        return self.expected is self.actual and not self.missing_findings

    @property
    def is_warning_row(self) -> bool:
        return self.field is FieldName.GOVERNMENT_WARNING

    @property
    def declares_warning_violation(self) -> bool:
        """A warning row that must NOT pass — by declaration or by pin, pending or not."""
        return self.is_warning_row and (
            self.pinned_violation or self.expected not in PASSING
        )

    @property
    def is_warning_violation(self) -> bool:
        """A declared violation that COUNTS TOWARD COVERAGE — the gate's denominator.

        A pending row is excluded here because a capability that provably does not exist
        cannot prove the gate works. It is emphatically **not** excluded from
        `is_warning_false_pass`: see the asymmetry note there.
        """
        return not self.pending and self.declares_warning_violation

    @property
    def is_withheld_warning_violation(self) -> bool:
        """A declared violation held out of the denominator by `pending`.

        Reported in its own right so shrinking the denominator is a visible act rather
        than a silent one.
        """
        return bool(self.pending) and self.declares_warning_violation

    @property
    def is_warning_false_pass(self) -> bool:
        """Expected a warning violation, got a passing verdict. Release-blocking.

        **`pending` does not apply here, and the asymmetry is the whole point.** Until
        2026-08-11 this read `is_warning_violation`, so one word in the catalog removed a
        row from both the numerator and the denominator — an override of the gate the
        README swore had none, and the obvious move for anyone wanting to clear a red
        build. A reviewer reproduced it in one line.

        The rule now: `pending` excuses an INACCURATE verdict; it never excuses a PASSING
        one on a warning violation. That is not a technicality — PRD §Constraints says
        warning checks fail closed, so a capability we have not built yet must surface as
        Unreadable or Needs review. A pipeline answering `match` because it cannot see the
        defect is failing open, which is the one thing this gate exists to catch.
        """
        return self.declares_warning_violation and self.actual in PASSING


@dataclass
class Report:
    """Everything one run of one tier measured."""

    tier: str
    outcomes: list[FieldOutcome] = dc_field(default_factory=list)
    errors: list[tuple[str, str]] = dc_field(default_factory=list)
    fixtures: int = 0
    provider: str = "fake:spec"
    """Which extractor produced these outcomes.

    Recorded so the CI run can prove it never touched a live model (ENG-3): the payload
    carries this string and a test asserts the gating run reports the offline provider.
    """

    floor: float = ACCURACY_FLOOR
    """Accuracy threshold for this run. May be raised above OPS-3's 95%, never lowered —
    `eval.run` rejects a lower value rather than accepting a gate that cannot fail."""

    required_violations: frozenset[str] = frozenset()
    """Fixtures that MUST appear as scored warning violations.

    The second half of the `pending` fix. Even with a false pass now impossible to
    suppress, marking a fixture pending still shrinks the gate's denominator — five
    checks quietly becoming four, with the report cheerfully reporting "0 false passes
    across 4 violation row(s)". This is the committed list the run is measured against,
    so a shrinking denominator has to shrink this list too, in a reviewable diff.
    """

    misrendered_violations: list[str] = dc_field(default_factory=list)
    """Pinned fixtures that stopped DRAWING the defect they exist to prove is caught.

    The third door, and the one that showed the others were protecting the wrong noun.
    `pending` and `expect` are declarations about a fixture; this is the fixture. Adding
    `warning_header_case="title"` to the prominence case made the pipeline answer
    `mismatch` — satisfying the expectation, firing no gate — while the prominence
    violation went on being undetected. An easy defect had quietly stood in for a hard one.
    """

    undeclared_violations: list[str] = dc_field(default_factory=list)
    """Fixtures the repository requires to DECLARE a warning violation, that no longer do.

    The other half of the same hole. `pending` was one off-switch; `expect` sits one line
    above it in the catalog and does the same job — emptying it makes a row expect a clean
    Match, so it stops being a violation at all and vanishes from the report rather than
    failing it. Checked against the specs, not the outcomes, because by outcome time the
    declaration is already gone.
    """

    subset: bool = False
    """True when the operator narrowed the run with --fixture.

    A narrowed run is a diagnostic, not a release gate: the warning-coverage condition
    is suspended (see the judgment log, J-03) because failing a single-fixture
    investigation for "you did not check the warning" only teaches people to ignore the
    message. The zero-false-pass check still applies to whatever rows were scored.
    """

    # --- what counts ------------------------------------------------------------------

    @property
    def scored(self) -> list[FieldOutcome]:
        """Outcomes that count toward accuracy — everything not waiting on a ticket."""
        return [o for o in self.outcomes if not o.pending]

    @property
    def pending(self) -> list[FieldOutcome]:
        return [o for o in self.outcomes if o.pending and not o.correct]

    @property
    def total(self) -> int:
        return len(self.scored)

    @property
    def correct(self) -> int:
        return sum(1 for o in self.scored if o.correct)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def failures(self) -> list[FieldOutcome]:
        return [o for o in self.scored if not o.correct]

    # --- the government warning -------------------------------------------------------

    @property
    def warning_rows(self) -> list[FieldOutcome]:
        return [o for o in self.scored if o.is_warning_row]

    @property
    def warning_violations(self) -> list[FieldOutcome]:
        """Rows on which a false pass is possible. The gate's denominator."""
        return [o for o in self.outcomes if o.is_warning_violation]

    @property
    def withheld_violations(self) -> list[FieldOutcome]:
        """Declared violations held out of the denominator by `pending`."""
        return [o for o in self.outcomes if o.is_withheld_warning_violation]

    @property
    def false_passes(self) -> list[FieldOutcome]:
        return [o for o in self.outcomes if o.is_warning_false_pass]

    @property
    def missing_required_violations(self) -> list[str]:
        """Fixtures the committed list requires but this run did not score.

        Names them rather than counting them: "coverage dropped by one" sends someone
        hunting, "tc04_bold_warning_body is no longer checked" does not.
        """
        scored = {o.fixture for o in self.warning_violations}
        return sorted(self.required_violations - scored)

    @property
    def warning_coverage_ok(self) -> bool:
        """Did this run actually exercise the zero-false-pass gate, in full?

        Three ways to fail. Zero checks is not evidence of anything; a denominator that
        shrank below what the repository declares is not either; and a fixture that
        stopped declaring its violation never reaches the denominator to shrink it.

        A narrowed `--fixture` run is exempt from the first two because the operator chose
        the narrowing — but NOT from the third. A missing declaration is a property of the
        catalog, not of which fixtures this run happened to select, so narrowing must not
        launder it.
        """
        if self.undeclared_violations or self.misrendered_violations:
            return False
        if self.subset:
            return True
        return bool(self.warning_violations) and not self.missing_required_violations

    # --- verdicts ---------------------------------------------------------------------

    def confusion(self) -> dict[tuple[Verdict, Verdict], int]:
        """expected -> actual counts over the scored rows."""
        return Counter((o.expected, o.actual) for o in self.scored)

    # --- the bottom line ----------------------------------------------------------------

    @property
    def accuracy_ok(self) -> bool:
        return self.total > 0 and self.accuracy >= self.floor

    @property
    def passed(self) -> bool:
        """The same conditions `eval.gates` names individually.

        Deliberately duplicated rather than delegated: `gates` needs `Report` for typing,
        so importing it here would be a cycle. `tests/test_eval.py` pins the two
        implementations against each other so they cannot drift (judgment log, J-05).
        """
        return (
            not self.errors
            and not self.false_passes
            and self.warning_coverage_ok
            and self.accuracy_ok
        )


def expected_verdicts(spec: LabelSpec) -> dict[FieldName, Verdict]:
    """Expected verdict per field. Anything unlisted is expected to be clean."""
    return {FieldName(k): Verdict(v) for k, v in spec.expect.items()}


def outcome_for(
    spec: LabelSpec,
    field_name: FieldName,
    actual: Verdict,
    raised_codes: set[str],
    expected: dict[FieldName, Verdict],
) -> FieldOutcome:
    """Score one field of one fixture."""
    want = expected.get(
        field_name,
        # Unlisted fields are expected clean; Not applicable counts as clean.
        Verdict.NOT_APPLICABLE if actual is Verdict.NOT_APPLICABLE else Verdict.MATCH,
    )
    wanted_codes = spec.expect_findings.get(field_name.value, [])
    return FieldOutcome(
        fixture=spec.name,
        field=field_name,
        expected=want,
        actual=actual,
        missing_findings=[c for c in wanted_codes if c not in raised_codes],
        pending=spec.pending,
        pinned_violation=(
            field_name is FieldName.GOVERNMENT_WARNING
            and spec.name in MUST_DECLARE_WARNING_VIOLATION
        ),
    )


#: Builds the provider for one fixture. The tier-B run and the model sweep pass a factory
#: that returns a live adapter; everything else takes the offline default.
ProviderFactory = Callable[[LabelSpec, list[ImageInput]], ExtractionProvider]


def evaluate(
    specs: list[LabelSpec],
    tier: str = "A",
    *,
    subset: bool = False,
    floor: float = ACCURACY_FLOOR,
    provider_name: str = "fake:spec",
    required_violations: frozenset[str] | None = None,
    provider_for: ProviderFactory | None = None,
) -> Report:
    """Run the given specs through the real pipeline and score the result.

    `provider_for` exists so the same scoring path can be pointed at a live model by the
    tier-B run and the model sweep. It defaults to the offline spec-backed provider,
    which is what CI uses and what makes this function safe to call with no credentials.
    """
    build_provider = provider_for or (lambda spec, images: SpecBackedProvider(spec))
    report = Report(
        tier=tier,
        subset=subset,
        fixtures=len(specs),
        floor=floor,
        provider=provider_name,
        required_violations=(
            REQUIRED_WARNING_VIOLATIONS if required_violations is None else required_violations
        ),
        undeclared_violations=undeclared_warning_violations(list(specs)),
        misrendered_violations=misrendered_warning_fixtures(list(specs)),
    )

    for spec in specs:
        try:
            # `expected_verdicts` is INSIDE the try on purpose. A typo in a fixture's
            # `expect` — an unknown verdict, a key with a stray space — raises ValueError,
            # and outside the try that reached the operator as a traceback and exit 1,
            # which this harness's own table defines as "accuracy below the floor". A
            # malformed catalog is a harness error (exit 4) and belongs on that page.
            expected = expected_verdicts(spec)
            roles = ["front", "back"] if spec.face != "single" else ["single"]
            images = [ImageInput(index=i, data=b"", role=r) for i, r in enumerate(roles)]
            application = Application.model_validate(spec.application())
            result = verify(application, images, build_provider(spec, images))
        # Broad on purpose: the harness reports a crashed fixture, it never crashes with it.
        except Exception as exc:  # noqa: BLE001 — a harness reports failures, it does not raise them
            report.errors.append((spec.name, f"{type(exc).__name__}: {exc}"))
            continue

        for field_result in result.fields:
            report.outcomes.append(
                outcome_for(
                    spec,
                    field_result.field,
                    field_result.verdict,
                    {f.code for f in field_result.findings},
                    expected,
                )
            )

    return report


__all__ = [
    "ACCURACY_FLOOR",
    "PASSING",
    "FieldOutcome",
    "ProviderFactory",
    "Report",
    "evaluate",
    "expected_verdicts",
    "outcome_for",
]
