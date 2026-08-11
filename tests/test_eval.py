"""The eval harness itself.

A harness that always reports PASS is worse than no harness, so most of these prove it
*fails* when it should — wrong verdicts, missing findings, and warning false passes.
"""

import pytest

from api.models import FieldName, Verdict
from eval.run import ACCURACY_FLOOR, FieldOutcome, Report, evaluate, main, render
from fixtures.generator.catalog import CATALOG, by_name


def outcome(**kw: object) -> FieldOutcome:
    base: dict[str, object] = {
        "fixture": "f",
        "field": FieldName.BRAND_NAME,
        "expected": Verdict.MATCH,
        "actual": Verdict.MATCH,
    }
    base.update(kw)
    return FieldOutcome(**base)  # type: ignore[arg-type]


# --- the real golden set ----------------------------------------------------------------

def test_golden_set_passes() -> None:
    report = evaluate(CATALOG)
    assert report.passed, render(report)


def test_golden_set_has_no_false_passes_on_warnings() -> None:
    """OPS-3 — release-blocking, checked independently of overall accuracy."""
    assert evaluate(CATALOG).false_passes == []


def test_golden_set_runs_every_fixture_without_error() -> None:
    assert evaluate(CATALOG).errors == []


def test_accuracy_meets_the_floor() -> None:
    assert evaluate(CATALOG).accuracy >= ACCURACY_FLOOR


# --- the harness detects failure ----------------------------------------------------------

def test_wrong_verdict_is_counted_as_a_failure() -> None:
    report = Report(tier="A", outcomes=[
        outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH)
    ])
    assert report.failures
    assert not report.passed


def test_missing_finding_fails_even_when_the_verdict_is_right() -> None:
    """TC-09's verdict is Match; the whole case is the finding riding alongside."""
    report = Report(tier="A", outcomes=[
        outcome(expected=Verdict.MATCH, actual=Verdict.MATCH,
                missing_findings=["proof_abv_inconsistent"])
    ])
    assert report.failures
    assert not report.passed


def test_warning_false_pass_fails_the_run_on_its_own() -> None:
    """Nineteen correct fields do not offset one warning slipping through."""
    outcomes = [outcome() for _ in range(19)]
    outcomes.append(outcome(field=FieldName.GOVERNMENT_WARNING,
                            expected=Verdict.MISSING, actual=Verdict.MATCH))
    report = Report(tier="A", outcomes=outcomes)
    assert report.accuracy >= ACCURACY_FLOOR
    assert report.false_passes
    assert not report.passed


def test_high_accuracy_does_not_excuse_a_false_pass() -> None:
    report = Report(tier="A", outcomes=[
        outcome(field=FieldName.GOVERNMENT_WARNING,
                expected=Verdict.MISMATCH, actual=Verdict.MATCH)
    ] + [outcome() for _ in range(99)])
    assert report.accuracy >= ACCURACY_FLOOR
    assert not report.passed


def test_a_fixture_that_crashes_fails_the_run() -> None:
    report = Report(tier="A", outcomes=[outcome()], errors=[("f", "boom")])
    assert not report.passed


def test_accuracy_below_the_floor_fails() -> None:
    outcomes = [outcome() for _ in range(90)]
    outcomes += [outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH) for _ in range(10)]
    report = Report(tier="A", outcomes=outcomes)
    assert report.accuracy < ACCURACY_FLOOR
    assert not report.passed


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
        outcome(),
        outcome(field=FieldName.GOVERNMENT_WARNING, expected=Verdict.MISMATCH,
                actual=Verdict.MATCH, pending="LP-211"),
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


def test_an_empty_run_does_not_pass() -> None:
    assert not Report(tier="A").passed


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


# --- reporting -------------------------------------------------------------------------------

def test_report_names_the_release_gate_explicitly() -> None:
    assert "False passes on warning violations" in render(evaluate(CATALOG))


def test_report_shows_the_confusion_matrix() -> None:
    assert "Confusion matrix" in render(evaluate(CATALOG))


def test_cli_exits_zero_when_the_set_passes() -> None:
    assert main([]) == 0


def test_cli_json_output_is_parseable(capsys: pytest.CaptureFixture[str]) -> None:
    import json
    main(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["false_passes"] == 0


def test_cli_can_run_a_single_fixture() -> None:
    assert main(["--fixture", "tc01_old_tom_clean"]) == 0


def test_cli_rejects_an_unknown_fixture() -> None:
    assert main(["--fixture", "does_not_exist"]) == 2


# --- expectations are honest -----------------------------------------------------------------

def test_no_fixture_expects_a_warning_violation_to_pass() -> None:
    """A golden set that expected a violation to be a Match would encode the bug."""
    for spec in CATALOG:
        expected = spec.expect.get("government_warning")
        if expected in ("mismatch", "missing"):
            assert expected not in ("match", "not_applicable")


def test_finding_expectations_reference_codes_the_code_can_raise() -> None:
    """Warning codes come from `warning.CHECK_MANIFEST`, which is itself asserted
    against the source (LP-218), so this list cannot fall behind the rules engine."""
    from api.rules.warning import FINDING_CODES

    known = {
        "proof_abv_inconsistent", "spirits_abv_abbreviation", "non_standard_fill",
    } | FINDING_CODES
    for spec in CATALOG:
        for codes in spec.expect_findings.values():
            for code in codes:
                assert code in known, f"{spec.name} expects unknown finding {code!r}"
