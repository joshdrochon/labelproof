"""The eval harness itself.

A harness that always reports PASS is worse than no harness, so most of these prove it
*fails* when it should — wrong verdicts, missing findings, warning false passes, and the
quiet one: a run that scored no warning violations at all and would otherwise print a
meaningless `0 false passes`.
"""

import json

import pytest

from api.models import FieldName, Verdict
from eval.outcomes import ACCURACY_FLOOR, FieldOutcome, Report, evaluate
from eval.report import render
from eval.run import main, payload
from fixtures.generator.catalog import CATALOG


def outcome(**kw: object) -> FieldOutcome:
    base: dict[str, object] = {
        "fixture": "f",
        "field": FieldName.BRAND_NAME,
        "expected": Verdict.MATCH,
        "actual": Verdict.MATCH,
    }
    base.update(kw)
    return FieldOutcome(**base)  # type: ignore[arg-type]


def warning_violation(**kw: object) -> FieldOutcome:
    """A government-warning row the golden set says must not pass."""
    base: dict[str, object] = {
        "field": FieldName.GOVERNMENT_WARNING,
        "expected": Verdict.MISMATCH,
        "actual": Verdict.MISMATCH,
    }
    base.update(kw)
    return outcome(**base)


# --- the real golden set ----------------------------------------------------------------

def test_golden_set_passes() -> None:
    report = evaluate(CATALOG)
    assert report.passed, render(report)


def test_golden_set_has_no_false_passes_on_warnings() -> None:
    """OPS-3 — release-blocking, checked independently of overall accuracy."""
    assert evaluate(CATALOG).false_passes == []


def test_golden_set_actually_exercises_the_warning_gate() -> None:
    """The zero above is only worth something if violations were scored."""
    assert evaluate(CATALOG).warning_violations


def test_golden_set_runs_every_fixture_without_error() -> None:
    assert evaluate(CATALOG).errors == []


def test_accuracy_meets_the_floor() -> None:
    assert evaluate(CATALOG).accuracy >= ACCURACY_FLOOR


# --- the harness detects failure ----------------------------------------------------------

def test_wrong_verdict_is_counted_as_a_failure() -> None:
    report = Report(tier="A", outcomes=[
        warning_violation(),
        outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH),
    ])
    assert report.failures
    assert not report.passed


def test_missing_finding_fails_even_when_the_verdict_is_right() -> None:
    """TC-09's verdict is Match; the whole case is the finding riding alongside."""
    report = Report(tier="A", outcomes=[
        warning_violation(),
        outcome(expected=Verdict.MATCH, actual=Verdict.MATCH,
                missing_findings=["proof_abv_inconsistent"]),
    ])
    assert report.failures
    assert not report.passed


def test_warning_false_pass_fails_the_run_on_its_own() -> None:
    """Nineteen correct fields do not offset one warning slipping through."""
    outcomes = [outcome() for _ in range(19)]
    outcomes.append(warning_violation(expected=Verdict.MISSING, actual=Verdict.MATCH))
    report = Report(tier="A", outcomes=outcomes)
    assert report.accuracy >= ACCURACY_FLOOR
    assert report.false_passes
    assert not report.passed


def test_high_accuracy_does_not_excuse_a_false_pass() -> None:
    report = Report(tier="A", outcomes=[
        warning_violation(actual=Verdict.MATCH),
    ] + [outcome() for _ in range(99)])
    assert report.accuracy >= ACCURACY_FLOOR
    assert not report.passed


def test_not_applicable_on_a_warning_violation_is_also_a_false_pass() -> None:
    """Both verdicts in the passing set count — 'not required' is still 'we passed it'."""
    report = Report(tier="A", outcomes=[warning_violation(actual=Verdict.NOT_APPLICABLE)])
    assert report.false_passes


def test_unreadable_on_a_warning_violation_is_not_a_false_pass() -> None:
    """Failing closed is the correct behaviour, not a slip (PRD §Constraints)."""
    report = Report(tier="A", outcomes=[warning_violation(actual=Verdict.UNREADABLE)])
    assert report.false_passes == []


def test_a_fixture_that_crashes_fails_the_run() -> None:
    report = Report(tier="A", outcomes=[warning_violation()], errors=[("f", "boom")])
    assert not report.passed


def test_accuracy_below_the_floor_fails() -> None:
    outcomes = [outcome() for _ in range(90)]
    outcomes += [outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH) for _ in range(10)]
    outcomes.append(warning_violation())
    report = Report(tier="A", outcomes=outcomes)
    assert report.accuracy < ACCURACY_FLOOR
    assert not report.passed


# --- the vacuous-zero guard (LP-121) --------------------------------------------------------

def test_a_run_that_scored_no_warning_violations_does_not_pass() -> None:
    """`0 false passes out of 0 checks` is not evidence. It is a broken fixture load."""
    report = Report(tier="A", outcomes=[outcome() for _ in range(50)])
    assert report.false_passes == []
    assert not report.warning_coverage_ok
    assert not report.passed


def test_the_report_says_so_when_the_gate_was_not_exercised() -> None:
    report = Report(tier="A", outcomes=[outcome()])
    text = render(report)
    assert "NO WARNING-VIOLATION ROWS SCORED" in text
    assert "proves nothing" in text


def test_a_subset_run_is_exempt_from_the_coverage_guard() -> None:
    """--fixture is a diagnostic. Failing it for 'you did not check the warning' would
    only teach people to ignore the message."""
    report = Report(tier="A", outcomes=[outcome()], subset=True)
    assert report.warning_coverage_ok
    assert report.passed
    assert "NOT A RELEASE GATE" in render(report)


def test_a_subset_run_still_fails_on_a_false_pass() -> None:
    report = Report(tier="A", outcomes=[warning_violation(actual=Verdict.MATCH)], subset=True)
    assert not report.passed


def test_a_pending_warning_row_does_not_count_as_coverage() -> None:
    """A capability that provably does not exist cannot prove the gate was exercised."""
    report = Report(tier="A", outcomes=[
        outcome(),
        warning_violation(actual=Verdict.MATCH, pending="LP-211"),
    ])
    assert report.warning_violations == []
    assert not report.passed


def test_an_empty_run_does_not_pass() -> None:
    assert not Report(tier="A").passed


# --- pending capability --------------------------------------------------------------------

def test_pending_outcomes_do_not_count_toward_accuracy() -> None:
    report = Report(tier="A", outcomes=[
        outcome(),
        outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH, pending="LP-211"),
    ])
    assert report.total == 1
    assert report.accuracy == 1.0


def test_pending_outcomes_do_not_trip_the_false_pass_gate() -> None:
    report = Report(tier="A", outcomes=[
        warning_violation(),
        warning_violation(actual=Verdict.MATCH, pending="LP-211"),
    ])
    assert report.false_passes == []
    assert report.passed


def test_a_run_with_nothing_scored_does_not_pass() -> None:
    """Nothing verified is not the same as everything passed."""
    report = Report(tier="A", outcomes=[
        outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH, pending="LP-211"),
    ])
    assert report.total == 0
    assert not report.passed


def test_pending_outcomes_still_appear_in_the_report() -> None:
    """Excluded from the score, never hidden — otherwise it is suppression."""
    report = Report(tier="A", outcomes=[
        outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH, pending="LP-211"),
    ])
    assert report.pending
    assert "LP-211" in render(report)
    assert "Pending capability" in render(report)


def test_a_pending_fixture_that_starts_passing_is_not_reported() -> None:
    """Once the capability lands the row disappears from pending on its own."""
    report = Report(tier="A", outcomes=[
        outcome(expected=Verdict.MISMATCH, actual=Verdict.MISMATCH, pending="LP-211"),
    ])
    assert report.pending == []


def test_only_tc06_is_pending_right_now() -> None:
    """If another fixture becomes pending, that should be a deliberate act."""
    pending = {s.name: s.pending for s in CATALOG if s.pending}
    assert pending == {"tc06_buried_warning": "LP-211"}


# --- reporting (LP-121) -----------------------------------------------------------------------

def test_a_false_pass_is_announced_before_anything_else() -> None:
    """It must be impossible to overlook — first thing on the page, not line forty."""
    report = Report(tier="A", outcomes=[warning_violation(actual=Verdict.MATCH)])
    head = render(report).split("\n", 6)
    assert any("FALSE PASS" in line for line in head)


def test_the_warning_section_reports_its_denominator() -> None:
    """A zero without the count of checks behind it is not a result."""
    report = evaluate(CATALOG)
    text = render(report)
    assert "GOVERNMENT WARNING — ZERO-FALSE-PASS GATE" in text
    assert f"must NOT pass: {len(report.warning_violations):4d}" in text
    assert "<- must be 0" in text


def test_the_false_pass_row_names_the_fixture() -> None:
    report = Report(tier="A", outcomes=[
        warning_violation(fixture="tc07_missing_warning", expected=Verdict.MISSING,
                          actual=Verdict.MATCH),
    ])
    assert "tc07_missing_warning" in render(report)


def test_the_confusion_matrix_shows_every_verdict_even_at_zero() -> None:
    """A fixed grid is diffable run over run; a sparse listing hides absent categories."""
    text = render(evaluate(CATALOG))
    assert "Confusion matrix" in text
    for verdict in Verdict:
        assert verdict.value in text


def test_the_confusion_matrix_marks_rows_that_did_not_reproduce() -> None:
    report = Report(tier="A", outcomes=[
        warning_violation(),
        outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH),
    ])
    matrix = [line for line in render(report).split("\n") if line.startswith("! ")]
    assert any("mismatch" in line for line in matrix)


def test_the_report_never_leaks_an_absolute_path() -> None:
    """Output has to be byte-identical between machines as well as between runs."""
    assert "/Users/" not in render(evaluate(CATALOG))


def test_errors_are_not_quietly_folded_into_accuracy() -> None:
    report = Report(tier="A", outcomes=[warning_violation()], errors=[("tc99", "KeyError")])
    text = render(report)
    assert "did not run at all" in text
    assert "tc99" in text


# --- the CLI --------------------------------------------------------------------------------

def test_cli_exits_zero_when_the_set_passes() -> None:
    assert main([]) == 0


def test_cli_json_output_is_parseable(capsys: pytest.CaptureFixture[str]) -> None:
    main(["--json"])
    body = json.loads(capsys.readouterr().out)
    assert body["passed"] is True
    assert body["false_passes"] == 0
    assert body["warning_violations"] > 0
    assert body["warning_coverage_ok"] is True


def test_cli_can_run_a_single_fixture() -> None:
    assert main(["--fixture", "tc01_old_tom_clean"]) == 0


def test_cli_marks_a_single_fixture_run_as_a_subset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["--json", "--fixture", "tc01_old_tom_clean"])
    assert json.loads(capsys.readouterr().out)["subset"] is True


def test_cli_rejects_an_unknown_fixture() -> None:
    assert main(["--fixture", "does_not_exist"]) == 2


def test_payload_round_trips_through_json() -> None:
    assert json.loads(json.dumps(payload(evaluate(CATALOG))))["tier"] == "A"


# --- expectations are honest -----------------------------------------------------------------

def test_no_fixture_expects_a_warning_violation_to_pass() -> None:
    """A golden set that expected a violation to be a Match would encode the bug."""
    for spec in CATALOG:
        expected = spec.expect.get("government_warning")
        if expected in ("mismatch", "missing"):
            assert expected not in ("match", "not_applicable")


def test_finding_expectations_reference_codes_the_code_can_raise() -> None:
    known = {
        "proof_abv_inconsistent", "spirits_abv_abbreviation", "non_standard_fill",
        "warning_missing", "warning_header_not_all_caps", "warning_header_not_bold",
        "warning_body_is_bold", "warning_header_missing",
        "warning_header_bold_unverified", "warning_body_bold_unverified",
    }
    for spec in CATALOG:
        for codes in spec.expect_findings.values():
            for code in codes:
                assert code in known, f"{spec.name} expects unknown finding {code!r}"
