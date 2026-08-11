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

    @property
    def correct(self) -> bool:
        return self.expected is self.actual and not self.missing_findings

    @property
    def is_warning_row(self) -> bool:
        return self.field is FieldName.GOVERNMENT_WARNING

    @property
    def is_warning_violation(self) -> bool:
        """A warning row the golden set says must NOT pass.

        These are the only rows on which a false pass is possible, so they are what the
        release gate is measured over. A pending row is excluded: the capability provably
        does not exist yet, so it can neither pass nor prove the gate was exercised.
        """
        return not self.pending and self.is_warning_row and self.expected not in PASSING

    @property
    def is_warning_false_pass(self) -> bool:
        """Expected a warning violation, got a passing verdict. Release-blocking."""
        return self.is_warning_violation and self.actual in PASSING


@dataclass
class Report:
    """Everything one run of one tier measured."""

    tier: str
    outcomes: list[FieldOutcome] = dc_field(default_factory=list)
    errors: list[tuple[str, str]] = dc_field(default_factory=list)
    fixtures: int = 0
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
    def false_passes(self) -> list[FieldOutcome]:
        return [o for o in self.outcomes if o.is_warning_false_pass]

    @property
    def warning_coverage_ok(self) -> bool:
        """Did this run actually exercise the zero-false-pass gate?

        Zero false passes out of zero checks is not evidence of anything. A narrowed
        run is exempt because the operator chose the narrowing.
        """
        return self.subset or bool(self.warning_violations)

    # --- verdicts ---------------------------------------------------------------------

    def confusion(self) -> dict[tuple[Verdict, Verdict], int]:
        """expected -> actual counts over the scored rows."""
        return Counter((o.expected, o.actual) for o in self.scored)

    # --- the bottom line ----------------------------------------------------------------

    @property
    def accuracy_ok(self) -> bool:
        return self.total > 0 and self.accuracy >= ACCURACY_FLOOR

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
    )


#: Builds the provider for one fixture. The tier-B run and the model sweep pass a factory
#: that returns a live adapter; everything else takes the offline default.
ProviderFactory = Callable[[LabelSpec, list[ImageInput]], ExtractionProvider]


def evaluate(
    specs: list[LabelSpec],
    tier: str = "A",
    *,
    subset: bool = False,
    provider_for: ProviderFactory | None = None,
) -> Report:
    """Run the given specs through the real pipeline and score the result.

    `provider_for` exists so the same scoring path can be pointed at a live model by the
    tier-B run and the model sweep. It defaults to the offline spec-backed provider,
    which is what CI uses and what makes this function safe to call with no credentials.
    """
    build_provider = provider_for or (lambda spec, images: SpecBackedProvider(spec))
    report = Report(tier=tier, subset=subset, fixtures=len(specs))

    for spec in specs:
        try:
            roles = ["front", "back"] if spec.face != "single" else ["single"]
            images = [ImageInput(index=i, data=b"", role=r) for i, r in enumerate(roles)]
            application = Application.model_validate(spec.application())
            result = verify(application, images, build_provider(spec, images))
        # Broad on purpose: the harness reports a crashed fixture, it never crashes with it.
        except Exception as exc:
            report.errors.append((spec.name, f"{type(exc).__name__}: {exc}"))
            continue

        expected = expected_verdicts(spec)
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
