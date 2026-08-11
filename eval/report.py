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
from eval.outcomes import PASSING, Report
from eval.tier_b import TierBSet

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
    withheld = report.withheld_violations
    out = [
        "",
        RULE,
        "GOVERNMENT WARNING - ZERO-FALSE-PASS GATE (OPS-3, release-blocking)",
        RULE,
        f"  Warning rows scored:                {len(report.warning_rows):4d}",
        f"  Of those, violations the set says must NOT pass: {len(violations):4d}"
        f"   (required: {len(report.required_violations)})",
        f"  Of those, reported as passing (FALSE PASSES):    "
        f"{len(report.false_passes):4d}   <- must be 0",
    ]

    if withheld:
        out.append("")
        out.append(
            f"  WITHHELD from the denominator by 'pending': {len(withheld)}. These are"
        )
        out.append("  declared violations this run did not count as coverage:")
        for o in withheld:
            verdict = "PASSING" if o.actual in PASSING else o.actual.value
            out.append(f"    {o.fixture:34s} got {verdict:22s} waiting on {o.pending}")
        out.append("  'pending' cannot hide a false pass — it is still counted above.")

    if report.missing_required_violations:
        out.append("")
        out.append("  COVERAGE SHORTFALL — required violation row(s) were not scored:")
        for name in report.missing_required_violations:
            out.append(f"    {name}")
        out.append(
            "  These are pinned in fixtures/generator/catalog.py. Shrinking the gate"
        )
        out.append("  means editing that list, in a diff someone has to approve.")

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
        if report.tier == "B":
            # Expected here, and stated so it does not read as a hole in the set: a
            # commercially approved label cannot carry a warning violation by definition,
            # so those cases are simulated in Tier A and cannot be photographed.
            out.append("  Expected for Tier B: an approved label carries no violation to find.")
            out.append("  Warning violations are validated under simulated degradation only.")
        elif report.subset:
            out.append("  Suspended: this is a --fixture subset run, not a release gate.")
        else:
            out.append("  A full run must exercise this gate. Treating it as a failure.")
    else:
        out.append("")
        out.append(f"  Clean: {len(violations)} violation(s) checked, none slipped through.")

    out.append(THIN)
    return out


def tier_b_status(tier_b: TierBSet, note: str = "") -> list[str]:
    """One block that appears in every run, so the gap is never invisible (LP-332).

    The failure mode being designed against is a Tier B section that quietly renders
    `0/0 = 100%` and gets pasted into a submission. With no photographs this prints no
    accuracy figure at all — the only reading that cannot be misread.
    """
    out = ["", THIN, "Tier B — real bottle photographs (reported, NEVER gates CI)"]

    if tier_b.problems:
        out.append(f"  MANIFEST PROBLEMS ({len(tier_b.problems)}) — this set is not usable:")
        out += [f"    {p}" for p in tier_b.problems]

    if tier_b.is_empty:
        out += [
            "  EMPTY — 0 labels in golden/tier_b/manifest.json.",
            "",
            "  This run says NOTHING about real bottle photographs. Tier A is generated by",
            "  our own renderer, so its number is an upper bound, not an answer. No Tier B",
            "  accuracy is reported here because there is none to report — 0 of 0 is not",
            "  100%.",
            "",
            "  To populate: drop 6-8 phone photos into golden/tier_b/images/ and add a row",
            "  each to the manifest. golden/tier_b/README.md has the shot list and shape.",
        ]
    else:
        verified = tier_b.hand_verified
        out.append(
            f"  {len(tier_b.labels)} label(s) declared, "
            f"{verified} hand-verified, {len(tier_b.labels) - verified} bootstrapped."
        )
    if note:
        out.append(f"  {note}")
    out.append(THIN)
    return out


def gap_section(tier_a: Report, tier_b: Report) -> list[str]:
    """The A-to-B gap, published as a metric rather than buried (BUILD.md §5)."""
    if not tier_b.total:
        return []
    gap = (tier_a.accuracy - tier_b.accuracy) * 100
    return [
        "",
        f"A↔B accuracy gap: {gap:+.1f} pp   "
        f"(Tier A {tier_a.accuracy:.1%} on {tier_a.total} rows, "
        f"Tier B {tier_b.accuracy:.1%} on {tier_b.total} rows)",
        "  Published, not averaged. Tier A measures the pipeline against our own renderer;",
        "  Tier B measures it against real labels. Blending them would hide the second.",
    ]


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


def body(report: Report) -> list[str]:
    """Everything about one tier, without the gate table or the verdict."""
    return (
        header(report)
        + errors_section(report)
        + accuracy_section(report)
        + confusion_section(report)
        + warning_section(report)
    )


def render(
    report: Report,
    tier_b: TierBSet | None = None,
    tier_b_report: Report | None = None,
    tier_b_note: str = "",
) -> str:
    """The human-readable report. Byte-stable across runs (LP-123).

    Only the Tier A gates set the verdict. Tier B is rendered alongside and never
    contributes to it — that is the whole point of the split (BUILD.md §5).
    """
    gates = gates_for(report)
    code = exit_code_for(gates)

    lines: list[str] = body(report)
    if tier_b is not None:
        lines += tier_b_status(tier_b, tier_b_note)
    if tier_b_report is not None:
        lines += ["", RULE]
        lines += body(tier_b_report)
        lines += gap_section(report, tier_b_report)
        lines += ["", "Tier B does not gate. The verdict below is Tier A's alone."]
    lines += gates_section(gates)
    lines += ["", "PASS" if code == 0 else f"FAIL (exit {code})"]
    lines += ["", status_line(report, gates)]
    return "\n".join(lines)
