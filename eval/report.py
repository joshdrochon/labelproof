"""Rendering — accuracy, the confusion matrix, and the gate nobody may miss (LP-121).

Three deliberate choices here, each one a response to a way this report could mislead.

**A false pass is announced before anything else.** If any government-warning violation
came back as a pass, the report opens with a banner block, repeats the count in its own
section, and closes on FAIL. It is the worst outcome the product can produce, so it is
not allowed to be one line among forty.

**Zero is reported with its denominator.** `False passes: 0` is meaningless without
`out of N violation rows checked`, because a run that checked nothing prints the same
zero. When N is zero the report says so and the run fails (see `Report.warning_coverage_ok`).

**The confusion matrix is a fixed 6x6 grid.** Every verdict gets a row and a column even
at zero, so the shape does not move with the data — the PRD tracks accuracy run over run,
and LP-123 requires two runs to be byte-identical. Nothing here prints a timestamp, a
duration or an absolute path for the same reason.
"""

from __future__ import annotations

from api.models import Verdict
from eval.gates import FAIL, SKIP, Gate, exit_code_for, gates_for, status_line
from eval.outcomes import Report

RULE = "=" * 78
THIN = "-" * 78

#: Column headers for the matrix. Short so six of them fit on one line at a fixed width,
#: which is what keeps the grid diffable between runs.
_SHORT: dict[Verdict, str] = {
    Verdict.MATCH: "match",
    Verdict.ACCEPTABLE_VARIATION: "accvar",
    Verdict.MISMATCH: "mismat",
    Verdict.MISSING: "missng",
    Verdict.UNREADABLE: "unread",
    Verdict.NOT_APPLICABLE: "n/a",
}

_TIER_DESCRIPTION: dict[str, str] = {
    "A": "synthetic fixtures — deterministic, gates CI",
    "B": "real bottle photographs — reported, never gates",
}


def banner(lines: list[str]) -> list[str]:
    """A block that cannot be skimmed past."""
    return [RULE, *lines, RULE]


def header(report: Report) -> list[str]:
    out: list[str] = []
    if report.false_passes:
        out += banner(
            [
                f"!! {len(report.false_passes)} FALSE PASS(ES) ON GOVERNMENT-WARNING "
                f"VIOLATIONS !!",
                "",
                "A label that violates 27 CFR 16.21/16.22 was reported as compliant.",
                "This is release-blocking on its own, whatever the accuracy number says.",
                "Details below under GOVERNMENT WARNING.",
            ]
        )
        out.append("")
    elif not report.warning_coverage_ok:
        out += banner(
            [
                "!! NO WARNING-VIOLATION ROWS SCORED !!",
                "",
                "The zero-false-pass gate was not exercised, so its zero proves nothing.",
                "The golden set should contain warning violations — check the fixture load.",
            ]
        )
        out.append("")

    description = _TIER_DESCRIPTION.get(report.tier, "")
    title = f"LabelProof eval — Tier {report.tier}"
    out.append(f"{title} ({description})" if description else title)
    out.append(RULE)
    if report.subset:
        out.append("SUBSET RUN — a --fixture filter is active. NOT A RELEASE GATE.")
    out.append(
        f"Fixtures run: {report.fixtures}    Field rows scored: {report.total}    "
        f"Extractor: {report.provider}"
    )
    return out


def errors_section(report: Report) -> list[str]:
    if not report.errors:
        return []
    out = ["", f"ERRORS ({len(report.errors)}) — fixtures that did not run at all:"]
    out += [f"  {name}: {message}" for name, message in report.errors]
    out.append("  A fixture that crashed was not verified. It is not a pass.")
    return out


def accuracy_section(report: Report) -> list[str]:
    out = [
        "",
        f"Field accuracy: {report.correct}/{report.total} "
        f"({report.accuracy:.1%})   floor {report.floor:.1%}"
        f"   {'OK' if report.accuracy_ok else 'BELOW FLOOR'}",
    ]

    if report.failures:
        out += ["", f"Mismatched expectations ({len(report.failures)}):"]
        for o in report.failures:
            detail = (
                f"missing finding(s): {', '.join(o.missing_findings)}"
                if o.missing_findings
                else f"got {o.actual.value}"
            )
            out.append(
                f"  {o.fixture:34s} {o.field.value:20s} "
                f"expected {o.expected.value:22s} {detail}"
            )

    if report.pending:
        out += ["", f"Pending capability ({len(report.pending)}) — not counted:"]
        for o in report.pending:
            out.append(
                f"  {o.fixture:34s} {o.field.value:20s} "
                f"expected {o.expected.value:22s} got {o.actual.value}   "
                f"waiting on {o.pending}"
            )
    return out


def confusion_section(report: Report) -> list[str]:
    """A fixed 6x6 grid, rows expected, columns actual, plus per-verdict recall."""
    counts = report.confusion()
    verdicts = list(Verdict)

    out = ["", "Confusion matrix — rows: expected, columns: actual"]
    out.append(
        f"  {'':22s}" + "".join(f"{_SHORT[v]:>8s}" for v in verdicts) + "  |   total  recall"
    )
    for want in verdicts:
        row = [counts.get((want, got), 0) for got in verdicts]
        total = sum(row)
        hit = counts.get((want, want), 0)
        recall = f"{hit / total:.0%}" if total else "-"
        marker = " " if total == hit else "!"
        out.append(
            f"{marker} {want.value:22s}"
            + "".join(f"{n:8d}" for n in row)
            + f"  |{total:8d}{recall:>8s}"
        )
    out.append(
        "  A '!' marks an expected verdict the pipeline did not always reproduce."
    )
    return out


def warning_section(report: Report) -> list[str]:
    """The release gate, reported with its denominator (OPS-3)."""
    violations = report.warning_violations
    out = [
        "",
        RULE,
        "GOVERNMENT WARNING — ZERO-FALSE-PASS GATE (OPS-3, release-blocking)",
        RULE,
        f"  Warning rows scored:                {len(report.warning_rows):4d}",
        f"  Of those, violations the set says must NOT pass: {len(violations):4d}",
        f"  Of those, reported as passing (FALSE PASSES):    "
        f"{len(report.false_passes):4d}   <- must be 0",
    ]

    if report.false_passes:
        out.append("")
        for o in report.false_passes:
            out.append(
                f"  FALSE PASS  {o.fixture:34s} expected {o.expected.value}, "
                f"got {o.actual.value}"
            )
        out.append("")
        out.append("  This gate blocks release regardless of overall accuracy.")
    elif not violations:
        out.append("")
        out.append("  NO VIOLATION ROWS WERE SCORED — the zero above proves nothing.")
        if report.subset:
            out.append("  Suspended: this is a --fixture subset run, not a release gate.")
        else:
            out.append("  A full run must exercise this gate. Treating it as a failure.")
    else:
        out.append("")
        out.append(f"  Clean: {len(violations)} violation(s) checked, none slipped through.")

    out.append(THIN)
    return out


_GATE_MARK: dict[str, str] = {FAIL: "FAIL", SKIP: "skip", "pass": "pass"}


def gates_section(gates: list[Gate]) -> list[str]:
    """The CI contract, printed where a human can see what the exit code meant."""
    out = ["", "CI gates (OPS-6) — blocking gates set the exit code:"]
    for gate in gates:
        blocking = "blocking" if gate.blocking else "advisory"
        out.append(
            f"  [{_GATE_MARK[gate.status]:4s}] {gate.name:24s} {blocking:9s} "
            f"exit {gate.exit_code}   {gate.summary}"
        )
    return out


def render(report: Report) -> str:
    """The human-readable report. Byte-stable across runs (LP-123)."""
    gates = gates_for(report)
    code = exit_code_for(gates)

    lines: list[str] = []
    lines += header(report)
    lines += errors_section(report)
    lines += accuracy_section(report)
    lines += confusion_section(report)
    lines += warning_section(report)
    lines += gates_section(gates)
    lines += ["", "PASS" if code == 0 else f"FAIL (exit {code})"]
    lines += ["", status_line(report, gates)]
    return "\n".join(lines)
