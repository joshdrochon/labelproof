"""CI gates — the machine-readable contract another job can branch on (LP-122, OPS-6).

The harness's job is to make a regression stop a release. That needs three things a
human-readable report cannot supply: named gates, a stable exit code per gate, and a
severity order so the worst failure is the one CI sees.

**Exit codes are distinct on purpose.** A single non-zero code forces the CI job to parse
prose to tell "field accuracy slipped 0.4%" from "a label that violates 27 CFR 16.21 was
reported compliant". Those two deserve different pager behaviour, so they get different
numbers, and when several gates fail the highest-severity code wins:

    0  every blocking gate passed
    1  field accuracy below the floor
    2  usage error (unknown fixture, bad flag) — raised by the CLI, not by a gate
    3  FALSE PASS on a government-warning violation      <- hard block, ranks first
    4  a fixture crashed; the harness could not score it
    5  no warning-violation rows were scored, so the gate proved nothing

**Blocking is a property of the gate, not of the caller.** `warning_zero_false_pass` is
release-blocking by construction (OPS-3) and there is no flag that relaxes it. A caller
that wants a softer run narrows the fixture set, and a narrowed run is labelled a subset
everywhere it is reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eval.outcomes import Report

EXIT_OK = 0
EXIT_ACCURACY = 1
EXIT_USAGE = 2
EXIT_WARNING_FALSE_PASS = 3
EXIT_HARNESS_ERROR = 4
EXIT_WARNING_COVERAGE = 5

#: Worst first. A run that trips several gates exits with the earliest code in this list,
#: so a false pass is never masked by a co-occurring accuracy dip.
SEVERITY: tuple[int, ...] = (
    EXIT_WARNING_FALSE_PASS,
    EXIT_WARNING_COVERAGE,
    EXIT_HARNESS_ERROR,
    EXIT_ACCURACY,
)

PASS = "pass"  # noqa: S105 — a gate status, not a credential
FAIL = "fail"
SKIP = "skip"


@dataclass(frozen=True)
class Gate:
    """One named condition, its verdict, and what CI should exit with if it fails."""

    name: str
    status: str
    blocking: bool
    exit_code: int
    summary: str

    @property
    def ok(self) -> bool:
        """A skipped gate is not a failure. It is also not evidence."""
        return self.status != FAIL

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "blocking": self.blocking,
            "exit_code": self.exit_code,
            "summary": self.summary,
        }


def _status(ok: bool) -> str:
    return PASS if ok else FAIL


def gates_for(report: Report) -> list[Gate]:
    """The full gate set for one report, worst-first.

    These recompute the conditions behind `Report.passed` rather than reading it, and
    `tests/test_eval.py` pins the two against each other — see the judgment log, J-05.
    """
    false_passes = len(report.false_passes)
    violations = len(report.warning_violations)

    missing = report.missing_required_violations
    if report.misrendered_violations:
        coverage_summary = (
            f"fixture(s) stopped DRAWING their pinned defect: "
            f"{'; '.join(report.misrendered_violations)}"
        )
    elif report.undeclared_violations:
        coverage_summary = (
            f"fixture(s) stopped DECLARING a violation: "
            f"{', '.join(report.undeclared_violations)} — the row left the gate entirely"
        )
    elif report.subset:
        coverage_summary = "subset run — coverage not required"
    elif missing:
        coverage_summary = (
            f"required violation row(s) not scored: {', '.join(missing)} — "
            f"the denominator shrank"
        )
    elif violations:
        coverage_summary = (
            f"{violations} warning-violation row(s) scored, "
            f"{len(report.required_violations)} required"
        )
    else:
        coverage_summary = (
            "NO warning-violation rows scored; the zero-false-pass gate proved nothing"
        )

    coverage = Gate(
        name="warning_gate_exercised",
        # A missing declaration is never skipped: it is a property of the catalog, not of
        # which fixtures this run selected, so a --fixture subset must not launder it.
        status=(
            _status(report.warning_coverage_ok)
            if report.undeclared_violations
            or report.misrendered_violations
            or not report.subset
            else SKIP
        ),
        blocking=True,
        exit_code=EXIT_WARNING_COVERAGE,
        summary=coverage_summary,
    )

    return [
        Gate(
            name="warning_zero_false_pass",
            status=_status(false_passes == 0),
            blocking=True,
            exit_code=EXIT_WARNING_FALSE_PASS,
            summary=(
                f"{false_passes} warning violation(s) reported as compliant"
                if false_passes
                else f"0 false passes across {violations} violation row(s)"
            ),
        ),
        coverage,
        Gate(
            name="harness_ran_clean",
            status=_status(not report.errors),
            blocking=True,
            exit_code=EXIT_HARNESS_ERROR,
            summary=(
                f"{len(report.errors)} fixture(s) crashed and were never scored"
                if report.errors
                else f"{report.fixtures} fixture(s) ran"
            ),
        ),
        Gate(
            name="field_accuracy",
            status=_status(report.accuracy_ok),
            blocking=True,
            exit_code=EXIT_ACCURACY,
            summary=(
                f"{report.accuracy:.1%} of {report.total} rows, floor {report.floor:.1%}"
                if report.total
                else "no rows scored"
            ),
        ),
    ]


def exit_code_for(gates: list[Gate]) -> int:
    """The single number CI branches on. Worst blocking failure wins."""
    failed = {g.exit_code for g in gates if g.blocking and g.status == FAIL}
    for code in SEVERITY:
        if code in failed:
            return code
    return EXIT_OK


def status_line(report: Report, gates: list[Gate]) -> str:
    """One greppable line for a CI log, with no timestamp so it stays byte-stable."""
    code = exit_code_for(gates)
    if code != EXIT_OK:
        status = "fail"
    elif report.subset:
        # Never "pass": a narrowed run has coverage suspended, so it cannot make that
        # claim, and this line is what a CI log gets grepped for.
        status = "subset"
    else:
        status = "pass"
    return (
        f"::labelproof-eval:: tier={report.tier} "
        f"status={status} exit={code} "
        f"accuracy={report.accuracy:.4f} "
        f"false_passes={len(report.false_passes)} "
        f"warning_violations={len(report.warning_violations)} "
        f"subset={'true' if report.subset else 'false'}"
    )
