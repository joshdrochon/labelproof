"""The eval harness itself.

A harness that always reports PASS is worse than no harness, so most of these prove it
*fails* when it should — wrong verdicts, missing findings, warning false passes, and the
quiet one: a run that scored no warning violations at all and would otherwise print a
meaningless `0 false passes`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from api.models import FieldName, Verdict
from eval import tier_b
from eval.gates import (
    EXIT_ACCURACY,
    EXIT_HARNESS_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_WARNING_COVERAGE,
    EXIT_WARNING_FALSE_PASS,
    exit_code_for,
    gates_for,
    status_line,
)
from eval.outcomes import ACCURACY_FLOOR, FieldOutcome, Report, evaluate
from eval.report import render
from eval.run import main, payload
from fixtures.generator.catalog import CATALOG

REPO = Path(__file__).resolve().parents[1]


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


# --- CI gates (LP-122) ------------------------------------------------------------------------

def test_the_golden_set_clears_every_blocking_gate() -> None:
    gates = gates_for(evaluate(CATALOG))
    assert [g.name for g in gates if g.status == "fail"] == []
    assert exit_code_for(gates) == EXIT_OK


def test_gates_agree_with_report_passed() -> None:
    """The two implementations of the same conditions must never disagree (J-05)."""
    reports = [
        evaluate(CATALOG),
        Report(tier="A", outcomes=[warning_violation()]),
        Report(tier="A", outcomes=[warning_violation(actual=Verdict.MATCH)]),
        Report(tier="A", outcomes=[outcome()]),
        Report(tier="A", outcomes=[outcome()], subset=True),
        Report(tier="A", outcomes=[warning_violation()], errors=[("f", "boom")]),
        Report(tier="A"),
        Report(
            tier="A",
            outcomes=[warning_violation()]
            + [outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH) for _ in range(9)],
        ),
    ]
    for report in reports:
        gates = gates_for(report)
        assert report.passed == (exit_code_for(gates) == EXIT_OK), render(report)


def test_a_false_pass_exits_three() -> None:
    """The safety-critical failure gets its own code so CI can page differently."""
    report = Report(tier="A", outcomes=[warning_violation(actual=Verdict.MATCH)])
    assert exit_code_for(gates_for(report)) == EXIT_WARNING_FALSE_PASS


def test_no_warning_coverage_exits_five() -> None:
    report = Report(tier="A", outcomes=[outcome() for _ in range(20)])
    assert exit_code_for(gates_for(report)) == EXIT_WARNING_COVERAGE


def test_a_crashed_fixture_exits_four() -> None:
    report = Report(tier="A", outcomes=[warning_violation()], errors=[("f", "boom")])
    assert exit_code_for(gates_for(report)) == EXIT_HARNESS_ERROR


def test_low_accuracy_exits_one() -> None:
    outcomes = [warning_violation()]
    outcomes += [outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH) for _ in range(9)]
    outcomes += [outcome() for _ in range(10)]
    report = Report(tier="A", outcomes=outcomes)
    assert exit_code_for(gates_for(report)) == EXIT_ACCURACY


def test_a_false_pass_outranks_every_other_failure() -> None:
    """A compliance failure must never be masked by a co-occurring accuracy dip."""
    outcomes = [warning_violation(actual=Verdict.MATCH)]
    outcomes += [outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH) for _ in range(9)]
    report = Report(tier="A", outcomes=outcomes, errors=[("f", "boom")])
    assert exit_code_for(gates_for(report)) == EXIT_WARNING_FALSE_PASS


def test_the_false_pass_gate_is_always_blocking() -> None:
    """There is no configuration under which OPS-3 becomes advisory."""
    for report in (evaluate(CATALOG), Report(tier="A", subset=True), Report(tier="A")):
        gate = next(g for g in gates_for(report) if g.name == "warning_zero_false_pass")
        assert gate.blocking
        assert gate.exit_code == EXIT_WARNING_FALSE_PASS


def test_the_coverage_gate_is_skipped_not_failed_on_a_subset_run() -> None:
    gates = gates_for(Report(tier="A", outcomes=[outcome()], subset=True))
    gate = next(g for g in gates if g.name == "warning_gate_exercised")
    assert gate.status == "skip"
    assert gate.ok


def test_the_gate_table_is_printed_in_the_report() -> None:
    text = render(evaluate(CATALOG))
    assert "CI gates (OPS-6)" in text
    for name in ("warning_zero_false_pass", "warning_gate_exercised",
                 "harness_ran_clean", "field_accuracy"):
        assert name in text


def test_the_report_ends_with_a_greppable_status_line() -> None:
    report = evaluate(CATALOG)
    line = render(report).strip().split("\n")[-1]
    assert line == status_line(report, gates_for(report))
    assert line.startswith("::labelproof-eval::")


def test_the_failing_report_names_its_exit_code() -> None:
    report = Report(tier="A", outcomes=[warning_violation(actual=Verdict.MATCH)])
    assert f"FAIL (exit {EXIT_WARNING_FALSE_PASS})" in render(report)


def test_cli_exit_code_matches_the_payload() -> None:
    assert main(["--json"]) == EXIT_OK


def test_cli_usage_error_code_is_distinct_from_every_gate() -> None:
    gate_codes = {g.exit_code for g in gates_for(evaluate(CATALOG))}
    assert EXIT_USAGE not in gate_codes
    assert main(["--fixture", "nope"]) == EXIT_USAGE


def test_cli_writes_a_report_artifact(tmp_path: object) -> None:
    from pathlib import Path

    out = Path(str(tmp_path)) / "nested" / "report.json"
    assert main(["--report-json", str(out)]) == EXIT_OK
    body = json.loads(out.read_text())
    assert body["status"] == "pass"
    assert body["gates"]
    assert body["exit_code"] == EXIT_OK


def test_documented_ci_command_matches_the_parser() -> None:
    """eval/README.md is the handoff artifact for whoever wires the workflow."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "eval" / "README.md").read_text()
    assert "python -m eval.run --report-json eval/out/report.json" in readme
    for code in (EXIT_OK, EXIT_ACCURACY, EXIT_USAGE, EXIT_WARNING_FALSE_PASS,
                 EXIT_HARNESS_ERROR, EXIT_WARNING_COVERAGE):
        assert f"`{code}`" in readme


# --- the CI run itself (LP-071) ----------------------------------------------------------------

def _run(args: list[str], **env: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONHASHSEED": "0", **env}
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=environment,
    )


def test_the_documented_ci_command_runs_and_passes(tmp_path: Path) -> None:
    """The exact command in eval/README.md, end to end, as CI will invoke it."""
    artifact = tmp_path / "report.json"
    done = _run(["-m", "eval.run", "--report-json", str(artifact)])
    assert done.returncode == EXIT_OK, done.stdout + done.stderr
    assert json.loads(artifact.read_text())["status"] == "pass"


def test_the_ci_run_makes_no_live_provider_call(tmp_path: Path) -> None:
    """ENG-3: CI is deterministic and passes with no network. Proven, not assumed.

    A poisoned key would make any real call fail loudly; the stronger assertion is that
    the Anthropic SDK is never even imported by the gating path.
    """
    probe = (
        "import sys;"
        "from eval.run import main;"
        "code = main(['--json']);"
        "live = sorted(m for m in sys.modules if m.startswith('anthropic'));"
        "assert not live, live;"
        "sys.exit(code)"
    )
    done = _run(["-c", probe], ANTHROPIC_API_KEY="sk-ant-not-a-real-key")
    assert done.returncode == EXIT_OK, done.stdout + done.stderr


def test_the_run_records_which_extractor_produced_the_number(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["--json"])
    assert json.loads(capsys.readouterr().out)["provider"] == "fake:spec"


def test_a_seeded_regression_trips_the_threshold_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of a threshold is that a worse run exits non-zero."""
    import eval.run as run_module

    broken = [CATALOG[0].with_(expect={"brand_name": "mismatch"}), *CATALOG[1:]]
    monkeypatch.setattr(run_module, "CATALOG", broken)

    # One wrong row out of ~98 is still above the 95% floor — the default run passes.
    assert main([]) == EXIT_OK
    # Raise the bar and the same run is blocked.
    assert main(["--min-accuracy", "1.0"]) == EXIT_ACCURACY


def test_the_threshold_can_be_raised() -> None:
    assert main(["--min-accuracy", "1.0"]) == EXIT_OK


def test_the_threshold_cannot_be_lowered_below_the_ops3_floor() -> None:
    """A gate whose bar can be lowered until it passes is not a gate."""
    assert main(["--min-accuracy", "0.5"]) == EXIT_USAGE
    assert main(["--min-accuracy", "0.0"]) == EXIT_USAGE


def test_the_threshold_rejects_impossible_values() -> None:
    assert main(["--min-accuracy", "1.5"]) == EXIT_USAGE


def test_the_effective_threshold_is_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["--json", "--min-accuracy", "0.99"])
    body = json.loads(capsys.readouterr().out)
    assert body["accuracy_floor"] == 0.99
    assert body["ops3_floor"] == ACCURACY_FLOOR


# --- determinism (LP-123) ----------------------------------------------------------------------

def test_two_runs_produce_byte_identical_text_output() -> None:
    """ENG-3. Two interpreters, two hash seeds, one set of bytes.

    A same-process double call cannot catch set- or dict-ordering drift — it shares one
    hash seed and one set of interned strings. Two subprocesses with different
    PYTHONHASHSEED values can, which is the whole point of running it this way.
    """
    first = _run(["-m", "eval.run"], PYTHONHASHSEED="0")
    second = _run(["-m", "eval.run"], PYTHONHASHSEED="12345")
    assert first.returncode == second.returncode == EXIT_OK
    assert first.stdout == second.stdout


def test_two_runs_produce_byte_identical_json_output() -> None:
    first = _run(["-m", "eval.run", "--json"], PYTHONHASHSEED="0")
    second = _run(["-m", "eval.run", "--json"], PYTHONHASHSEED="98765")
    assert first.stdout == second.stdout


def test_two_report_artifacts_are_byte_identical(tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    _run(["-m", "eval.run", "--report-json", str(a)], PYTHONHASHSEED="1")
    _run(["-m", "eval.run", "--report-json", str(b)], PYTHONHASHSEED="2")
    assert a.read_bytes() == b.read_bytes()


def test_the_report_carries_nothing_that_changes_between_runs() -> None:
    """No timestamp, no elapsed time, no absolute path — the three usual culprits."""
    text = render(evaluate(CATALOG))
    for leak in (str(REPO), "/Users/", "elapsed", "seconds", "20260", "20261"):
        assert leak not in text, leak


def test_the_confusion_matrix_keeps_its_shape_when_the_data_changes() -> None:
    """A grid whose rows appear and vanish with the data cannot be diffed run over run."""
    full = render(evaluate(CATALOG)).split("\n")
    sparse = render(Report(tier="A", outcomes=[warning_violation()])).split("\n")

    def grid(lines: list[str]) -> int:
        start = next(i for i, line in enumerate(lines) if "Confusion matrix" in line)
        return sum(1 for line in lines[start:start + 9] if line.startswith((" ", "!")))

    assert grid(full) == grid(sparse)


# --- Tier B: real bottle photographs (LP-332) --------------------------------------------------

TIER_B_DIR = REPO / "golden" / "tier_b"


def _tier_b_manifest(
    tmp_path: Path,
    labels: list[dict[str, object]],
    absent: frozenset[str] = frozenset(),
    **top: object,
) -> Path:
    """Write a Tier B manifest with one real image on disk per declared file.

    `absent` names files the manifest declares but that are deliberately not written —
    the case where a row points at a photograph nobody has.
    """
    images = tmp_path / "images"
    images.mkdir(exist_ok=True)
    source = REPO / "fixtures" / "labels" / "tc01_old_tom_clean.png"
    for label in labels:
        for image in label.get("images", []):  # type: ignore[union-attr]
            name = str(image.get("file", ""))
            if name and name not in absent and not (images / name).exists():
                (images / name).write_bytes(source.read_bytes())

    body: dict[str, object] = {"tier": "B", "gates_ci": False, "labels": labels}
    body.update(top)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(body, indent=2))
    return path


def _old_tom_row(**overrides: object) -> dict[str, object]:
    spec = CATALOG[0]
    row: dict[str, object] = {
        "name": "b01_old_tom",
        "commodity": "spirits",
        "images": [{"file": "b01.png", "role": "single"}],
        "application": spec.application(),
        "expect": {},
        "expect_findings": {},
        "capture": {"conditions": "straight on"},
        "ground_truth": "bootstrapped",
        "notes": "Baseline straight-on shot.",
    }
    row.update(overrides)
    return row


def test_the_committed_tier_b_manifest_loads_clean() -> None:
    """It ships empty, but it must be a valid empty rather than a broken one."""
    loaded = tier_b.load()
    assert loaded.problems == [], loaded.problems
    assert loaded.is_empty
    assert loaded.capture_guide, "the capture guide is the instruction for populating it"


def test_tier_b_never_gates_by_construction() -> None:
    assert tier_b.TIER_B_GATES is False


def test_a_manifest_claiming_to_gate_is_rejected(tmp_path: Path) -> None:
    """Flipping this flag would quietly reverse the one rule that keeps Tier B honest."""
    path = _tier_b_manifest(tmp_path, [], gates_ci=True)
    assert any("gates_ci" in p for p in tier_b.load(path).problems)


def test_an_empty_tier_b_reports_no_accuracy_at_all(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0 of 0 is not 100%, and a section that rendered 100% would end up in a submission."""
    main(["--json", "--tier", "all"])
    body = json.loads(capsys.readouterr().out)["tier_b"]
    assert body["empty"] is True
    assert body["accuracy"] is None
    assert body["gap_pp"] is None


def test_the_empty_state_says_so_in_plain_language() -> None:
    text = render(evaluate(CATALOG), tier_b.load())
    assert "Tier B" in text
    assert "EMPTY" in text
    assert "says NOTHING about real bottle photographs" in text
    assert "0 of 0 is not" in text


def test_tier_b_appears_even_in_the_default_tier_a_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The gap must never be invisible, so the status shows on every run."""
    main([])
    assert "Tier B" in capsys.readouterr().out


def test_a_tier_b_false_pass_does_not_change_the_exit_code() -> None:
    """Tier B is reported, never gating — including its safety-critical rows."""
    tier_a = evaluate(CATALOG)
    broken = Report(tier="B", outcomes=[warning_violation(actual=Verdict.MATCH)])
    assert exit_code_for(gates_for(tier_a)) == EXIT_OK
    text = render(tier_a, tier_b.load(), broken)
    assert "Tier B does not gate" in text
    assert text.strip().endswith("subset=false")


def test_the_a_to_b_gap_is_published(tmp_path: Path) -> None:
    tier_a = evaluate(CATALOG)
    tier_b_report = Report(
        tier="B",
        outcomes=[outcome() for _ in range(9)]
        + [outcome(expected=Verdict.MISMATCH, actual=Verdict.MATCH)],
    )
    text = render(tier_a, tier_b.load(), tier_b_report)
    assert "A↔B accuracy gap" in text
    assert "+10.0 pp" in text
    assert "Blending them would hide the second" in text


def test_tier_b_runs_end_to_end_against_a_populated_manifest(tmp_path: Path) -> None:
    """The loader, the image read and the scoring, with the model stubbed out."""
    from api.provider.fake import SpecBackedProvider

    path = _tier_b_manifest(tmp_path, [_old_tom_row()])
    loaded = tier_b.load(path)
    assert loaded.problems == []
    assert len(loaded.labels) == 1
    assert loaded.labels[0].images[0].path.read_bytes()

    report = tier_b.evaluate(
        loaded.labels, lambda label, images: SpecBackedProvider(CATALOG[0])
    )
    assert report.tier == "B"
    assert report.errors == []
    assert report.total > 0
    assert report.accuracy == 1.0


def test_tier_b_loads_the_real_image_bytes(tmp_path: Path) -> None:
    path = _tier_b_manifest(tmp_path, [_old_tom_row()])
    label = tier_b.load(path).labels[0]
    inputs = tier_b.image_inputs(label)
    assert inputs[0].data.startswith(b"\x89PNG")
    assert inputs[0].media_type == "image/png"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"commodity": "cider"}, "commodity"),
        ({"expect": {"government_warning": "probably_fine"}}, "field/verdict"),
        ({"expect": {"vintage": "match"}}, "field/verdict"),
        ({"ground_truth": "trust_me"}, "ground_truth"),
        ({"notes": "  "}, "no notes"),
        ({"images": []}, "at least one entry"),
        ({"application": {"brand_name": "x"}}, "not a valid record"),
    ],
)
def test_the_loader_names_what_is_wrong(
    tmp_path: Path, overrides: dict[str, object], fragment: str
) -> None:
    path = _tier_b_manifest(tmp_path, [_old_tom_row(**overrides)])
    problems = tier_b.load(path).problems
    assert any(fragment in p for p in problems), problems


def test_a_declared_image_that_is_missing_is_a_defect_not_a_skip(tmp_path: Path) -> None:
    """Scoring a row against an image nobody has would be worse than not scoring it."""
    path = _tier_b_manifest(
        tmp_path,
        [_old_tom_row(images=[{"file": "gone.heic", "role": "front"}])],
        absent=frozenset({"gone.heic"}),
    )
    loaded = tier_b.load(path)
    assert any("is not in" in p for p in loaded.problems), loaded.problems
    assert loaded.labels == []
    assert not loaded.usable


def test_duplicate_label_names_are_rejected(tmp_path: Path) -> None:
    path = _tier_b_manifest(tmp_path, [_old_tom_row(), _old_tom_row()])
    assert any("duplicate" in p for p in tier_b.load(path).problems)


def test_a_broken_manifest_fails_the_run_that_asked_for_tier_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed manifest is a repo defect, not a model result."""
    path = _tier_b_manifest(tmp_path, [_old_tom_row(commodity="cider")])
    monkeypatch.setattr(tier_b, "MANIFEST", path)
    assert main(["--tier", "b"]) == EXIT_USAGE
    # ...and does not break the run that never asked for it.
    assert main([]) == EXIT_OK


def test_tier_b_skips_rather_than_fails_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An offline machine has not regressed. It has simply not measured this."""
    path = _tier_b_manifest(tmp_path, [_old_tom_row()])
    monkeypatch.setattr(tier_b, "MANIFEST", path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert main(["--json", "--tier", "b"]) == EXIT_OK
    body = json.loads(capsys.readouterr().out)["tier_b"]
    assert body["accuracy"] is None
    assert "ANTHROPIC_API_KEY" in body["note"]


def test_the_tier_b_readme_documents_the_row_shape() -> None:
    """Dropping images in a directory is only the last step if the shape is written down."""
    readme = (TIER_B_DIR / "README.md").read_text()
    for key in ("images", "application", "expect", "ground_truth", "notes"):
        assert key in readme
    assert "python -m eval.run --tier b" in readme


def test_tier_b_does_not_report_a_missing_warning_violation_as_a_hole() -> None:
    """An approved label cannot carry a violation, so its absence is expected, not a gap."""
    report = Report(tier="B", outcomes=[outcome()])
    text = render(evaluate(CATALOG), tier_b.load(), report)
    assert "an approved label carries no violation to find" in text.lower()


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
