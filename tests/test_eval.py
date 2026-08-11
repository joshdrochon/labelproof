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

from api import canon
from api.models import FieldName, Verdict, WarningTypography
from api.provider.base import (
    ExtractionRequest,
    ExtractionResponse,
    ImageInput,
    ProviderUsage,
)
from api.provider.fake import SpecBackedProvider
from eval import sweep, tier_b
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
from eval.run import build_parser, main, payload
from fixtures.generator.catalog import (
    CATALOG,
    MUST_DECLARE_WARNING_VIOLATION,
    REQUIRED_WARNING_VIOLATIONS,
    WARNING_DEFECT_PINS,
    by_name,
    misrendered_warning_fixtures,
    warning_defects,
)
from fixtures.generator.spec import LabelSpec

REPO = Path(__file__).resolve().parents[1]

#: Fixtures on which the PIPELINE currently fails open — a declared government-warning
#: violation that comes back as a passing verdict.
#:
#: `tc06_buried_warning` renders a verbatim warning that is shrunk and low-contrast. The
#: rules engine has no prominence heuristics yet (LP-211), so it answers `match`. That is
#: a live false pass, and since the `pending` hole was closed the gate says so:
#: `python -m eval.run` exits 3 today, and CI is correctly blocked until LP-211 merges.
#:
#: This register is NOT an override — `eval.run` has none, and that is the point of the
#: fix. It exists so the developer suite can assert the true current state instead of a
#: fiction. Every assertion below is written to pass BOTH before and after LP-211 lands,
#: so the warning agent's fix cannot mask whether the gate hole is really closed.
KNOWN_LIVE_FALSE_PASSES: frozenset[str] = frozenset({"tc06_buried_warning"})


def live_false_passes() -> frozenset[str]:
    return frozenset(o.fixture for o in evaluate(CATALOG).false_passes)


def golden_exit() -> int:
    """What a full run exits with today — EXIT_OK the moment the pipeline fails closed."""
    return EXIT_WARNING_FALSE_PASS if live_false_passes() else EXIT_OK


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

def test_the_golden_set_hides_no_false_pass_we_have_not_named() -> None:
    """The register may shrink to nothing; it may never quietly grow.

    Passes before LP-211 (tc06 is the one known gap) and after (no gap at all), so the
    warning agent's fix landing cannot disguise a regression here.
    """
    assert live_false_passes() <= KNOWN_LIVE_FALSE_PASSES, (
        f"unacknowledged false pass: {sorted(live_false_passes() - KNOWN_LIVE_FALSE_PASSES)}"
    )


def test_the_gate_fires_while_a_live_false_pass_exists() -> None:
    """A known gap is still a red build. There is no register inside the gate itself."""
    report = evaluate(CATALOG)
    if live_false_passes():
        assert not report.passed, "a live false pass must fail the run"
        assert exit_code_for(gates_for(report)) == EXIT_WARNING_FALSE_PASS
    else:
        assert report.passed, render(report)


def test_every_known_gap_names_a_ticket_in_the_report() -> None:
    """A gap without an owner is a gap nobody closes."""
    text = render(evaluate(CATALOG))
    for fixture in live_false_passes():
        assert fixture in text
    assert len(KNOWN_LIVE_FALSE_PASSES) <= 1, (
        "growing this register is a deliberate act — justify it in the diff"
    )


def test_golden_set_scores_every_required_warning_violation() -> None:
    report = evaluate(CATALOG)
    assert report.missing_required_violations == []
    assert report.undeclared_violations == []


#: The names the repository committed to checking. Written out here, not imported, because
#: `assert report.required_violations == REQUIRED_WARNING_VIOLATIONS` was a tautology —
#: `evaluate()` assigns that exact constant — so emptying the constant left 767 tests green
#: and the pin defending nothing.
PINNED_REQUIRED_VIOLATIONS = frozenset(
    {
        "tc03_title_case_warning",
        "tc04_bold_warning_body",
        "tc05_reworded_warning",
        "tc07_missing_warning",
    }
)


def test_the_required_violation_list_can_only_grow() -> None:
    """A ratchet, so removing a check is a failing test rather than a quiet deletion."""
    assert PINNED_REQUIRED_VIOLATIONS <= REQUIRED_WARNING_VIOLATIONS, (
        "a fixture was removed from the zero-false-pass denominator: "
        f"{sorted(PINNED_REQUIRED_VIOLATIONS - REQUIRED_WARNING_VIOLATIONS)}"
    )


def test_the_must_declare_list_covers_the_required_list_and_the_pending_one() -> None:
    """Declaration is the wider net: it must cover fixtures the scoring pin cannot.

    `tc06_buried_warning` is exactly that case — `pending`, so it never reaches the
    denominator, which is why emptying its `expect` was invisible to every other check.
    """
    assert REQUIRED_WARNING_VIOLATIONS <= MUST_DECLARE_WARNING_VIOLATION
    assert "tc06_buried_warning" in MUST_DECLARE_WARNING_VIOLATION
    assert PINNED_REQUIRED_VIOLATIONS | {
        "tc06_buried_warning"
    } <= MUST_DECLARE_WARNING_VIOLATION


def test_every_pinned_fixture_still_exists_in_the_catalog() -> None:
    """A pin naming a deleted fixture protects nothing and looks like it does."""
    names = {spec.name for spec in CATALOG}
    assert names >= MUST_DECLARE_WARNING_VIOLATION, (
        f"pinned but absent: {sorted(MUST_DECLARE_WARNING_VIOLATION - names)}"
    )


def test_emptying_the_required_list_would_be_caught() -> None:
    """Proves the ratchet above is not itself a tautology."""
    assert not (frozenset() >= PINNED_REQUIRED_VIOLATIONS)


def test_the_declaration_pin_fires_when_an_expectation_is_emptied() -> None:
    """The reviewer's two-line diff: drop `expect`, regenerate, ship.

    Catalog-independent, so it keeps proving the hole is shut whatever tc06's expectation
    becomes when LP-211 lands.
    """
    gutted = [
        spec.with_(expect={}) if spec.name == "tc06_buried_warning" else spec
        for spec in CATALOG
    ]
    report = evaluate(gutted)
    assert report.undeclared_violations == ["tc06_buried_warning"]
    assert not report.warning_coverage_ok
    assert not report.passed
    assert "DECLARATION SHORTFALL" in render(report)
    # The pin, not `expect`, is the authority on whether this row is a violation — so
    # neutering the declaration surfaces the live fail-open as a FALSE PASS rather than
    # demoting it to a coverage shortfall. "Coverage" is the wrong page for
    # "a label violating 27 CFR 16.21 was reported compliant".
    assert exit_code_for(gates_for(report)) == EXIT_WARNING_FALSE_PASS
    assert {o.fixture for o in report.false_passes} == {"tc06_buried_warning"}


def test_neutering_expect_on_a_pinned_fixture_that_is_caught_is_a_coverage_failure() -> None:
    """When the pipeline does catch the defect, the shortfall is genuinely coverage."""
    gutted = [
        spec.with_(expect={}) if spec.name == "tc04_bold_warning_body" else spec
        for spec in CATALOG
    ]
    report = evaluate(gutted)
    assert report.undeclared_violations == ["tc04_bold_warning_body"]
    slipped = {o.fixture for o in report.false_passes}
    assert "tc04_bold_warning_body" not in slipped, "the pipeline still answers mismatch"
    # tc06's separate, live fail-open is the only thing in there.
    assert slipped <= KNOWN_LIVE_FALSE_PASSES


def test_a_passing_expectation_is_as_bad_as_no_expectation() -> None:
    gutted = [
        spec.with_(expect={"government_warning": "match"})
        if spec.name == "tc04_bold_warning_body"
        else spec
        for spec in CATALOG
    ]
    report = evaluate(gutted)
    assert "tc04_bold_warning_body" in report.undeclared_violations
    assert not report.passed


# --- what the fixture RENDERS, not what is declared about it -----------------------------------

def test_every_pinned_fixture_draws_exactly_its_pinned_defect() -> None:
    assert misrendered_warning_fixtures(list(CATALOG)) == []


def test_the_pins_and_the_defect_derivation_agree_on_the_real_catalog() -> None:
    """Reading the pins should tell you what the set covers, without opening the specs."""
    for name, pinned in WARNING_DEFECT_PINS.items():
        spec = by_name(name)
        assert warning_defects(spec) == pinned, name


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"warning_header_case": "title"}, "adds a defect the rules engine already catches"),
        ({"warning_body_bold": True}, "adds a catchable defect"),
        ({"warning_header_bold": False}, "adds a catchable defect"),
        ({"include_warning": False}, "swaps the hard case for the easiest one"),
        ({"warning_scale": 1.0, "warning_contrast": 1.0}, "removes the defect entirely"),
        (
            {"warning_scale": 1.0, "warning_contrast": 1.0, "warning_header_case": "title"},
            "the two-line retarget that reads innocent in a diff",
        ),
    ],
)
def test_tc06_cannot_be_retargeted_away_from_prominence(
    changes: dict[str, object], why: str
) -> None:
    """The third door. Every pin before this one protected a declaration ABOUT tc06.

    Adding any catchable defect makes the pipeline answer `mismatch`, which satisfies the
    expectation, so nothing fired — while the 16.21 prominence violation went on being
    completely undetected. The fixture had simply stopped isolating it.
    """
    retargeted = [
        spec.with_(**changes) if spec.name == "tc06_buried_warning" else spec
        for spec in CATALOG
    ]
    report = evaluate(retargeted)
    assert report.misrendered_violations, why
    assert "tc06_buried_warning" in report.misrendered_violations[0]
    assert not report.passed
    assert exit_code_for(gates_for(report)) in (
        EXIT_WARNING_COVERAGE,
        EXIT_WARNING_FALSE_PASS,
    )
    assert "RENDER SHORTFALL" in render(report)


CLEAN_WARNING = {
    "include_warning": True,
    "warning_text": None,
    "warning_header_case": "upper",
    "warning_header_bold": True,
    "warning_body_bold": False,
    "warning_scale": 1.0,
    "warning_contrast": 1.0,
}


@pytest.mark.parametrize("name", sorted(WARNING_DEFECT_PINS))
def test_the_render_pin_covers_every_warning_fixture_not_just_tc06(name: str) -> None:
    """Neutralising any pinned fixture is the same attack, and is caught the same way."""
    neutralised = [
        spec.with_(**CLEAN_WARNING) if spec.name == name else spec
        for spec in CATALOG
    ]
    problems = misrendered_warning_fixtures(neutralised)
    assert any(name in p for p in problems), problems


@pytest.mark.parametrize("name", sorted(WARNING_DEFECT_PINS))
def test_adding_a_second_defect_is_caught_on_every_pinned_fixture(name: str) -> None:
    """Adding is as bad as removing: an easy defect standing in for a hard one."""
    spec = by_name(name)
    if not spec.include_warning:
        pytest.skip("typography knobs are meaningless when the warning is absent")
    extra = "body_bold" not in WARNING_DEFECT_PINS[name]
    swapped = [
        s.with_(warning_body_bold=extra, warning_header_bold=not extra)
        if s.name == name
        else s
        for s in CATALOG
    ]
    assert any(name in p for p in misrendered_warning_fixtures(swapped))


def test_the_defect_derivation_reads_pixels_not_intent() -> None:
    base = by_name("tc01_old_tom_clean")
    assert warning_defects(base) == frozenset()
    assert warning_defects(base.with_(include_warning=False)) == frozenset({"absent"})
    assert warning_defects(base.with_(warning_scale=0.45)) == frozenset({"prominence"})
    assert warning_defects(base.with_(warning_contrast=0.35)) == frozenset({"prominence"})
    # A warning that is small but still legible is not a prominence defect.
    assert warning_defects(base.with_(warning_scale=0.9)) == frozenset()
    assert warning_defects(
        base.with_(warning_body_bold=True, warning_header_case="title")
    ) == frozenset({"body_bold", "header_not_all_caps"})


def test_a_missing_fixture_cannot_satisfy_its_render_pin() -> None:
    """Deleting the fixture must not silently satisfy the pin by absence."""
    without = [s for s in CATALOG if s.name != "tc06_buried_warning"]
    assert misrendered_warning_fixtures(without) == [], (
        "absence is caught by the existence pin, not this one"
    )
    names = {s.name for s in without}
    assert not names >= MUST_DECLARE_WARNING_VIOLATION


@pytest.mark.parametrize(
    "bad_expect",
    [
        {"government_warning": "probably_fine"},
        {"government_warning ": "mismatch"},
        {"vintage": "match"},
        {"government_warning": ""},
    ],
)
def test_a_malformed_expectation_is_a_harness_error_not_an_accuracy_failure(
    bad_expect: dict[str, str],
) -> None:
    """A typo in the catalog exited 1 — the code this harness defines as 'below floor'.

    Same class as the `nan` threshold bug: a configuration mistake wearing a gate's exit
    code. pytest caught it because pytest shows tracebacks; the harness did not.
    """
    broken = [
        spec.with_(expect=bad_expect) if spec.name == "tc01_old_tom_clean" else spec
        for spec in CATALOG
    ]
    report = evaluate(broken)
    assert any("tc01_old_tom_clean" in name for name, _ in report.errors)
    assert exit_code_for(gates_for(report)) in (
        EXIT_HARNESS_ERROR,
        EXIT_WARNING_FALSE_PASS,
    )
    assert "did not run at all" in render(report)


def test_the_cli_survives_a_malformed_catalog(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No traceback, and an exit code that names the right problem."""
    import eval.run as run_module

    broken = [CATALOG[0].with_(expect={"government_warning": "probably_fine"}), *CATALOG[1:]]
    monkeypatch.setattr(run_module, "CATALOG", broken)
    code = main(["--json"])
    body = json.loads(capsys.readouterr().out)
    assert code != EXIT_ACCURACY
    assert body["errors"], "the malformed fixture must be reported, not raised"


def test_a_subset_run_cannot_launder_a_retargeted_fixture() -> None:
    retargeted = [
        spec.with_(warning_scale=1.0, warning_contrast=1.0)
        for spec in CATALOG
        if spec.name == "tc06_buried_warning"
    ]
    report = evaluate(retargeted, subset=True)
    assert report.misrendered_violations
    assert not report.passed


def test_a_subset_run_cannot_launder_a_missing_declaration() -> None:
    """Narrowing is an operator's choice about scope, not a licence to drop a check."""
    gutted = [
        spec.with_(expect={}) for spec in CATALOG if spec.name == "tc06_buried_warning"
    ]
    report = evaluate(gutted, subset=True)
    assert report.undeclared_violations == ["tc06_buried_warning"]
    assert not report.passed


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


def test_pending_does_not_excuse_a_passing_verdict_on_a_warning_violation() -> None:
    """The bypass, closed. `pending` excuses a WRONG verdict, never a PASSING one.

    Catalog-independent on purpose: this is the assertion that must keep proving the hole
    is shut no matter what any fixture's expectation becomes later.
    """
    report = Report(tier="A", outcomes=[
        warning_violation(),
        warning_violation(fixture="hidden", actual=Verdict.MATCH, pending="LP-999"),
    ])
    assert [o.fixture for o in report.false_passes] == ["hidden"]
    assert not report.passed


def test_pending_still_excuses_an_inaccurate_but_fail_closed_verdict() -> None:
    """The legitimate use survives: a capability we lack may be wrong, not permissive."""
    report = Report(
        tier="A",
        outcomes=[
            warning_violation(),
            warning_violation(expected=Verdict.MISSING, actual=Verdict.UNREADABLE,
                              pending="LP-211"),
        ],
    )
    assert report.false_passes == []
    assert report.passed


def test_marking_a_required_fixture_pending_fails_the_run() -> None:
    """The denominator cannot shrink quietly either — the reviewer's suggested addition."""
    required = frozenset({"tc03_title_case_warning", "tc04_bold_warning_body"})
    report = Report(
        tier="A",
        required_violations=required,
        outcomes=[
            warning_violation(fixture="tc03_title_case_warning"),
            # Marked pending, and answering non-permissively so no false pass fires.
            warning_violation(fixture="tc04_bold_warning_body", actual=Verdict.UNREADABLE,
                              pending="LP-999"),
        ],
    )
    assert report.missing_required_violations == ["tc04_bold_warning_body"]
    assert not report.warning_coverage_ok
    assert not report.passed
    assert exit_code_for(gates_for(report)) == EXIT_WARNING_COVERAGE


def test_the_report_names_a_shrunken_denominator() -> None:
    report = Report(
        tier="A",
        required_violations=frozenset({"tc03_title_case_warning", "tc04_bold_warning_body"}),
        outcomes=[warning_violation(fixture="tc03_title_case_warning")],
    )
    text = render(report)
    assert "COVERAGE SHORTFALL" in text
    assert "tc04_bold_warning_body" in text


def test_withheld_violations_are_reported_not_silently_dropped() -> None:
    report = Report(tier="A", outcomes=[
        warning_violation(),
        warning_violation(fixture="tc06_buried_warning", actual=Verdict.UNREADABLE,
                          pending="LP-211"),
    ])
    text = render(report)
    assert "WITHHELD from the denominator" in text
    assert "tc06_buried_warning" in text
    assert "LP-211" in text


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
    assert "GOVERNMENT WARNING - ZERO-FALSE-PASS GATE" in text
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

def test_cli_exit_code_reflects_the_real_state_of_the_set() -> None:
    assert main([]) == golden_exit()


def test_cli_json_output_is_parseable(capsys: pytest.CaptureFixture[str]) -> None:
    main(["--json"])
    body = json.loads(capsys.readouterr().out)
    assert body["false_passes"] == len(live_false_passes())
    assert body["passed"] is (golden_exit() == EXIT_OK)
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

def test_only_the_known_gap_fails_a_blocking_gate() -> None:
    gates = gates_for(evaluate(CATALOG))
    failing = {g.name for g in gates if g.status == "fail"}
    expected = {"warning_zero_false_pass"} if live_false_passes() else set()
    assert failing == expected
    assert exit_code_for(gates) == golden_exit()


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
    assert main(["--json"]) == golden_exit()


def test_cli_usage_error_code_is_distinct_from_every_gate() -> None:
    gate_codes = {g.exit_code for g in gates_for(evaluate(CATALOG))}
    assert EXIT_USAGE not in gate_codes
    assert main(["--fixture", "nope"]) == EXIT_USAGE


def test_cli_writes_a_report_artifact(tmp_path: object) -> None:
    from pathlib import Path

    out = Path(str(tmp_path)) / "nested" / "report.json"
    assert main(["--report-json", str(out)]) == golden_exit()
    body = json.loads(out.read_text())
    assert body["gates"]
    assert body["exit_code"] == golden_exit()


def _readme_exit_table() -> dict[int, str]:
    """Parse the exit-code table out of eval/README.md as {code: gate name}."""
    readme = (REPO / "eval" / "README.md").read_text()
    table: dict[int, str] = {}
    for line in readme.split("\n"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or not cells[0].startswith("`"):
            continue
        code = cells[0].strip("`")
        if not code.isdigit():
            continue
        table[int(code)] = cells[1].strip("`*")
    return table


def test_the_readme_exit_code_table_matches_the_gates() -> None:
    """Pins code-to-gate, not just 'the digit appears somewhere'.

    The previous version asserted each digit appeared in backticks anywhere in the file,
    so a reviewer could swap 3 and 5 in the table and both 'documented' tests stayed
    green — drifting the one fact the pinning test exists to protect.
    """
    documented = _readme_exit_table()
    actual = {g.exit_code: g.name for g in gates_for(evaluate(CATALOG))}

    for code, name in actual.items():
        assert code in documented, f"exit {code} ({name}) is not in the README table"
        assert documented[code] == name, (
            f"README says exit {code} is {documented[code]!r}, gates say {name!r}"
        )

    assert documented.keys() == actual.keys() | {EXIT_OK, EXIT_USAGE}, (
        "the README table documents a code no gate emits, or omits one"
    )
    assert documented[EXIT_OK] in ("—", "-", "")
    assert documented[EXIT_USAGE] in ("—", "-", "")


def test_documented_ci_command_matches_the_parser() -> None:
    """eval/README.md is the handoff artifact for whoever wires the workflow."""
    readme = (REPO / "eval" / "README.md").read_text()
    assert "python -m eval.run --report-json eval/out/report.json" in readme
    # The command must actually parse — a documented flag that no longer exists is worse
    # than no documentation.
    build_parser().parse_args(["--report-json", "eval/out/report.json"])


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
    assert done.returncode == golden_exit(), done.stdout + done.stderr
    assert json.loads(artifact.read_text())["exit_code"] == golden_exit()


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
    assert done.returncode == golden_exit(), done.stdout + done.stderr


def test_the_run_records_which_extractor_produced_the_number(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["--json"])
    assert json.loads(capsys.readouterr().out)["provider"] == "fake:spec"


def test_a_seeded_regression_trips_the_threshold_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of a threshold is that a worse run exits non-zero.

    Built on a set with the live fail-open fixture removed, so the accuracy gate is what
    the assertions actually observe. Written against the full catalog, both assertions
    reduced to `== 3` while tc06 false-passes — the seeded regression was never the thing
    being measured, and the test proved nothing until LP-211 landed.
    """
    import eval.run as run_module

    sound = [s for s in CATALOG if s.name not in KNOWN_LIVE_FALSE_PASSES]
    monkeypatch.setattr(run_module, "CATALOG", sound)
    monkeypatch.setattr(
        "eval.outcomes.REQUIRED_WARNING_VIOLATIONS", REQUIRED_WARNING_VIOLATIONS
    )
    assert main([]) == EXIT_OK, "the sound subset must be green before seeding anything"

    broken = [sound[0].with_(expect={"brand_name": "mismatch"}), *sound[1:]]
    monkeypatch.setattr(run_module, "CATALOG", broken)

    # One wrong row out of ~98 is still above the 95% floor — the default run passes.
    assert main([]) == EXIT_OK
    # Raise the bar and the same run is blocked, by the accuracy gate specifically.
    assert main(["--min-accuracy", "1.0"]) == EXIT_ACCURACY


def test_the_threshold_can_be_raised() -> None:
    assert main(["--min-accuracy", "1.0"]) == golden_exit()


def test_the_threshold_cannot_be_lowered_below_the_ops3_floor() -> None:
    """A gate whose bar can be lowered until it passes is not a gate."""
    assert main(["--min-accuracy", "0.5"]) == EXIT_USAGE
    assert main(["--min-accuracy", "0.0"]) == EXIT_USAGE


def test_the_threshold_rejects_impossible_values() -> None:
    assert main(["--min-accuracy", "1.5"]) == EXIT_USAGE


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_the_threshold_rejects_non_finite_values(value: str) -> None:
    """`nan > 1.0` and `nan < 0.95` are both False, so nan slipped through both bounds
    and surfaced as 'floor nan% BELOW FLOOR', exit 1 — a typo wearing the accuracy gate's
    exit code."""
    assert main([f"--min-accuracy={value}"]) == EXIT_USAGE


# --- surviving an ASCII terminal (LP-123 robustness) -------------------------------------------

def test_the_report_is_pure_ascii() -> None:
    """`PYTHONIOENCODING=ascii` is normal in minimal containers and cron.

    An em-dash there raises UnicodeEncodeError and Python exits 1, which this harness's
    own exit-code table defines as an accuracy failure.
    """
    render(evaluate(CATALOG)).encode("ascii")


def test_every_report_shape_is_pure_ascii() -> None:
    shapes = [
        Report(tier="A", outcomes=[warning_violation(actual=Verdict.MATCH)]),
        Report(tier="A", outcomes=[outcome()], subset=True),
        Report(tier="A", errors=[("f", "boom")]),
        Report(
            tier="A",
            required_violations=frozenset({"missing_one"}),
            outcomes=[warning_violation()],
        ),
    ]
    for report in shapes:
        render(report, tier_b.load(), Report(tier="B", outcomes=[outcome()])).encode("ascii")


def test_the_sweep_report_is_pure_ascii() -> None:
    specs = evidence_complete_specs()
    sweep.render(_run_sweep({"claude-opus-5": 1.0}, specs, repeat=3), specs).encode("ascii")
    sweep.render(_run_sweep({"claude-opus-5": 1.0}, list(CATALOG)), list(CATALOG)).encode(
        "ascii"
    )


def test_the_cli_runs_under_an_ascii_only_stdout() -> None:
    done = _run(
        ["-m", "eval.run"], PYTHONIOENCODING="ascii", LC_ALL="C", LANG="C"
    )
    assert done.returncode == golden_exit(), done.stdout + done.stderr
    assert "UnicodeEncodeError" not in done.stderr
    assert "GOVERNMENT WARNING" in done.stdout


def test_ascii_transliteration_is_readable_not_mangled() -> None:
    from eval.report import ascii_safe

    assert ascii_safe("a — b") == "a -- b"
    assert ascii_safe("A↔B") == "A<->B"
    assert ascii_safe("≥95%") == ">=95%"
    assert ascii_safe("中") == "?"


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
    assert first.returncode == second.returncode == golden_exit()
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
        declared = label.get("images", [])
        assert isinstance(declared, list)
        for image in declared:
            name = str(image.get("file", "")) if isinstance(image, dict) else ""
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
    assert exit_code_for(gates_for(tier_a)) == golden_exit()
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
    assert "A-to-B accuracy gap" in text
    assert "+10.0 pp" in text
    assert "Blending them would hide the second" in text


def test_the_gap_section_does_not_overstate_what_tier_a_measures() -> None:
    """Tier A never touches the renderer or a model: `data=b""`, verdicts from the spec."""
    tier_a = evaluate(CATALOG)
    tier_b_report = Report(tier="B", outcomes=[outcome() for _ in range(4)])
    text = render(tier_a, tier_b.load(), tier_b_report)
    assert "RULES ENGINE" in text
    assert "no pixels and no model" in text
    assert "our own renderer" not in text


def test_the_empty_tier_b_block_does_not_overstate_tier_a_either() -> None:
    text = render(evaluate(CATALOG), tier_b.load())
    assert "our own renderer" not in text
    assert "RULES ENGINE" in text


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
    # A gate failure outranks it; with no gate failure the manifest sets the code.
    assert main(["--tier", "b"]) == (golden_exit() or EXIT_USAGE)
    # ...and does not break the run that never asked for it.
    assert main([]) == golden_exit()


def test_a_broken_manifest_never_masks_a_warning_false_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A compliance failure must not reach CI disguised as 'bad flag'.

    Reproduced by the reviewer: the same Tier A false pass exited 3 with a good manifest
    and 2 with a broken one, while the JSON kept saying 3.
    """
    path = _tier_b_manifest(tmp_path, [_old_tom_row(commodity="cider")])
    monkeypatch.setattr(tier_b, "MANIFEST", path)
    monkeypatch.setattr(
        "eval.run.evaluate",
        lambda *a, **k: Report(
            tier="A", outcomes=[warning_violation(actual=Verdict.MATCH)]
        ),
    )
    code = main(["--json", "--tier", "b"])
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_WARNING_FALSE_PASS
    assert body["exit_code"] == code, "the payload and the process must agree"
    assert body["tier_b_manifest_invalid"] is True


def test_every_top_level_signal_in_the_payload_agrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`exit_code`, `status` and `passed` must never contradict each other.

    A broken Tier B manifest with every Tier A gate green produced
    `{"exit_code": 2, "status": "fail", "passed": true}` — the same shape of bug as the
    exit-code disagreement, one field over, and a CI job branching on `.passed` ships.
    """
    path = _tier_b_manifest(tmp_path, [_old_tom_row(commodity="cider")])
    monkeypatch.setattr(tier_b, "MANIFEST", path)
    for argv in ([], ["--tier", "b"], ["--tier", "all"]):
        code = main(["--json", *argv])
        body = json.loads(capsys.readouterr().out)
        assert body["exit_code"] == code, argv
        assert body["passed"] is (code == 0), argv
        assert body["status"] == ("pass" if code == 0 else "fail"), argv


def test_a_broken_manifest_alone_makes_passed_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Isolates the exact case: gates green, manifest broken."""
    path = _tier_b_manifest(tmp_path, [_old_tom_row(commodity="cider")])
    monkeypatch.setattr(tier_b, "MANIFEST", path)
    monkeypatch.setattr(
        "eval.run.evaluate",
        lambda *a, **k: Report(
            tier="A",
            outcomes=[warning_violation()],
            required_violations=frozenset(),
        ),
    )
    code = main(["--json", "--tier", "b"])
    body = json.loads(capsys.readouterr().out)
    assert code == EXIT_USAGE
    assert body["passed"] is False
    assert body["gates_passed"] is True, "the Tier A verdict stays available separately"


def test_the_payload_exit_code_always_equals_the_process_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """README promises 'two forms, same content'. Hold it to that."""
    path = _tier_b_manifest(tmp_path, [_old_tom_row(commodity="cider")])
    monkeypatch.setattr(tier_b, "MANIFEST", path)
    for argv in ([], ["--tier", "b"], ["--tier", "all"]):
        code = main(["--json", *argv])
        assert json.loads(capsys.readouterr().out)["exit_code"] == code, argv


def test_tier_b_errors_are_visible_in_the_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Six labels that all failed must not look like Tier B never ran."""
    path = _tier_b_manifest(tmp_path, [_old_tom_row()])
    monkeypatch.setattr(tier_b, "MANIFEST", path)
    monkeypatch.setattr(tier_b, "evaluate", lambda labels, provider: Report(
        tier="B", fixtures=len(labels), errors=[(label.name, "APIStatusError: 400")
                                               for label in labels],
    ))
    monkeypatch.setattr("eval.run.live.has_credentials", lambda: True)
    monkeypatch.setattr("eval.run.live.build", lambda *a, **k: (lambda *_: None))

    main(["--json", "--tier", "b"])
    body = json.loads(capsys.readouterr().out)["tier_b"]
    assert body["ran"] is True
    assert body["errors"], "a run where every label failed must say so"
    assert body["accuracy"] is None


def test_a_tier_b_that_never_ran_is_distinguishable_from_one_that_all_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["--json"])
    body = json.loads(capsys.readouterr().out)["tier_b"]
    assert body["ran"] is False
    assert body["errors"] == []


def test_tier_b_skips_rather_than_fails_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An offline machine has not regressed. It has simply not measured this."""
    path = _tier_b_manifest(tmp_path, [_old_tom_row()])
    monkeypatch.setattr(tier_b, "MANIFEST", path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert main(["--json", "--tier", "b"]) == golden_exit()
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


# --- model-tier sweep (LP-329) -----------------------------------------------------------------

class _Harness:
    """A stand-in for one model: fixed latency, fixed token cost, correct extraction.

    Lets the whole sweep — the table, the disqualification rule, the recommendation — be
    exercised with no network and no spend, which is the only way those rules get tested
    at all. `load_images` doubles as the hook that tells the extractor which fixture is in
    front of it, mirroring how the real adapter receives one label at a time.
    """

    def __init__(self, seconds: float, tokens: tuple[int, int] = (10_000, 800)) -> None:
        self.name = "fake:timed"
        self.seconds = seconds
        self.tokens = tokens
        self.spec = CATALOG[0]
        self._now = 0.0

    def clock(self) -> float:
        value = self._now
        self._now += self.seconds
        return value

    def load_images(self, spec: LabelSpec) -> list[ImageInput]:
        self.spec = spec
        roles = ["front", "back"] if spec.face != "single" else ["single"]
        return [ImageInput(index=i, data=b"x", role=r) for i, r in enumerate(roles)]

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        # A model that FAILS CLOSED where the rules engine currently fails open. These
        # tests exercise the sweep's table, disqualification rule and recommendation; the
        # pipeline's own LP-211 gap is asserted separately and would otherwise turn every
        # simulated model into a false-pass disqualification for a reason unrelated to
        # what is under test.
        illegible = (
            {FieldName.GOVERNMENT_WARNING}
            if self.spec.name in KNOWN_LIVE_FALSE_PASSES
            else set()
        )
        response = SpecBackedProvider(self.spec, illegible=illegible).extract(request)
        response.usage = ProviderUsage(
            input_tokens=self.tokens[0], output_tokens=self.tokens[1], model=self.name
        )
        return response


def _run_sweep(
    models: dict[str, float], specs: list[LabelSpec], repeat: int = 1
) -> list[sweep.ModelResult]:
    """Run the sweep with a per-model latency, offline."""
    results = []
    for model, seconds in models.items():
        harness = _Harness(seconds)
        results.append(
            sweep.run_model(
                model,
                specs,
                harness,
                repeat=repeat,
                clock=harness.clock,
                load_images=harness.load_images,
            )
        )
    return results


def evidence_complete_specs() -> list[LabelSpec]:
    """The catalog plus enough DISTINCT fixtures to satisfy the evidence gate.

    Two per posture, because that is what the gate now requires: one fixture cannot
    distinguish "the model reads this posture" from "the model reads this one picture",
    and re-sending the same PNG does not help.

    These stay test-local. The catalog's real gap (`header_not_bold` has zero fixtures,
    every other posture has one) is deliberately left alone — the fixtures that close it
    belong to the warning agent, and the sweep is supposed to keep reporting the gap until
    they land. These stand-ins exist so the ship rule itself can be tested on a set that
    does have evidence.
    """
    base = CATALOG[0]
    variants = [
        ("header_not_bold_a", {"warning_header_bold": False}),
        ("header_not_bold_b", {"warning_header_bold": False, "brand_name": "SECOND LABEL"}),
        ("header_case_b", {"warning_header_case": "lower"}),
        ("body_bold_b", {"warning_body_bold": True, "brand_name": "SECOND LABEL"}),
        ("text_altered_b", {"warning_text": "According to the Surgeon General, drink less."}),
        ("warning_absent_b", {"include_warning": False}),
    ]
    return [
        *CATALOG,
        *(
            base.with_(
                name=f"local_{name}",
                expect={"government_warning": "mismatch"},
                notes=f"Local stand-in giving the {name} posture a second distinct label.",
                **changes,  # type: ignore[arg-type]
            )
            for name, changes in variants
        ),
    ]


def test_the_sweep_measures_accuracy_cost_and_latency_per_model() -> None:
    results = _run_sweep({"claude-opus-5": 4.0, "claude-haiku-4-5": 2.0}, list(CATALOG))
    opus, haiku = results
    assert opus.report.accuracy == 1.0
    assert opus.p50 == 4.0 and haiku.p50 == 2.0
    # 10k input + 800 output at $5/$25 per MTok = $0.07; at $1/$5 = $0.014.
    assert round(opus.usd_per_label, 4) == 0.07
    assert round(haiku.usd_per_label, 4) == 0.014
    assert opus.qualified and haiku.qualified


def test_the_cheapest_qualifying_tier_is_what_ships() -> None:
    """The ship rule, applied rather than restated — on a set that has the evidence."""
    specs = evidence_complete_specs()
    results = _run_sweep({"claude-opus-5": 4.0, "claude-haiku-4-5": 2.0}, specs, repeat=3)
    winner = sweep.recommend(results, specs)
    assert winner is not None
    assert winner.model == "claude-haiku-4-5"
    assert "SHIPS: claude-haiku-4-5" in sweep.render(results, specs)


class _TypographyLiar(_Harness):
    """A model that reads the warning's typography as compliant when it is not.

    The realistic Haiku failure: the text is transcribed correctly, and `body_is_bold` /
    `header_is_all_caps` come back the way a compliant label would look. Nothing else about
    the extraction is wrong, which is exactly why it slips past everything except the
    warning gate.
    """

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        response = super().extract(request)
        for extraction in response.extractions:
            if extraction.warning_text is None:
                continue
            extraction.warning_text = canon.CANONICAL_WARNING
            # The comparison reads the extracted FIELD, not `warning_text`; a liar that
            # only rewrote the latter would not actually reach the warning rules.
            field = extraction.fields.get(FieldName.GOVERNMENT_WARNING)
            if field is not None:
                field.value = canon.CANONICAL_WARNING
            extraction.warning_typography = WarningTypography(
                header_is_all_caps=True,
                header_is_bold=True,
                body_is_bold=False,
                relative_size=1.0,
                contrast_ok=True,
            )
        return response


def test_a_fast_model_that_reads_the_warning_wrong_is_disqualified() -> None:
    """The whole point of the instrument: speed does not buy a pass on the warning gate.

    End to end through a misreporting extractor, not by appending the outcome the
    assertion then reads. The previous version proved `disqualifiers` consults
    `false_passes`; it did not prove that a model reporting `body_is_bold=False` on tc04
    becomes one.
    """
    specs = evidence_complete_specs()
    liar = _TypographyLiar(1.0)
    result = sweep.run_model(
        "claude-haiku-4-5", specs, liar, repeat=3,
        clock=liar.clock, load_images=liar.load_images,
    )

    slipped = {o.fixture for o in result.report.false_passes}
    assert "tc04_bold_warning_body" in slipped, "a bold body read as non-bold must be caught"
    assert "tc03_title_case_warning" in slipped
    assert not result.qualified
    assert any("false pass" in r for r in result.disqualifiers)

    text = sweep.render([result], specs)
    assert "DISQUALIFIED" in text
    assert "fast and reads the warning wrong is disqualified here, not excused" in text


def test_the_same_model_reading_typography_honestly_is_not_disqualified() -> None:
    """The control. Without it, the test above could pass for the wrong reason."""
    specs = evidence_complete_specs()
    honest = _run_sweep({"claude-haiku-4-5": 1.0}, specs, repeat=3)[0]
    assert honest.report.false_passes == []
    assert honest.qualified


def test_a_disqualified_cheaper_model_is_named_next_to_the_winner() -> None:
    """The report must say why the cheap tier was passed over, not just pick another."""
    specs = evidence_complete_specs()
    results = _run_sweep({"claude-opus-5": 4.0, "claude-haiku-4-5": 1.0}, specs, repeat=3)
    results[1].report.outcomes.append(
        warning_violation(fixture="tc07_missing_warning", actual=Verdict.MATCH)
    )
    text = sweep.render(results, specs)
    assert "SHIPS: claude-opus-5" in text
    assert "claude-haiku-4-5 is cheaper" in text
    assert "disqualified" in text


def test_latency_never_disqualifies() -> None:
    """p95 here is extraction plus rules, not upload-to-verdict. It is context, not a gate."""
    specs = evidence_complete_specs()
    results = _run_sweep({"claude-opus-5": 12.0}, specs, repeat=3)
    result = results[0]
    assert result.latency_risk
    assert result.qualified
    assert result.disqualifiers == []
    text = sweep.render(results, specs)
    assert "LATENCY RISK" in text
    assert "CONTEXT only" in text


def test_no_qualifying_model_is_reported_as_such() -> None:
    specs = evidence_complete_specs()
    results = _run_sweep({"claude-haiku-4-5": 1.0}, specs, repeat=3)
    results[0].report.errors.append(("tc01_old_tom_clean", "boom"))
    assert sweep.recommend(results, specs) is None
    assert "NO MODEL QUALIFIES" in sweep.render(results, specs)


def test_a_p95_from_too_few_samples_says_it_is_not_a_percentile() -> None:
    """`2 image(s) n=1` reported a p95 from one sample, on the axis the decision turns on."""
    specs = [s for s in CATALOG if s.face != "single"]
    text = sweep.render(_run_sweep({"claude-opus-5": 3.0}, list(CATALOG)), list(CATALOG))
    assert specs, "the set must still contain a two-image fixture"
    assert "[thin]" in text
    assert "the maximum, not a percentile" in text


def test_enough_repeats_clear_the_thin_marker() -> None:
    specs = evidence_complete_specs()
    text = sweep.render(_run_sweep({"claude-opus-5": 3.0}, specs, repeat=5), specs)
    assert "[thin]" not in text


def test_latency_is_split_by_call_shape() -> None:
    """One image is one call; two images are two concurrent calls. Blending describes
    neither, and the split is what the current model decision turns on."""
    results = _run_sweep({"claude-opus-5": 3.0}, list(CATALOG))
    assert results[0].call_shapes == [1, 2]
    text = sweep.render(results, list(CATALOG))
    assert "Latency by call shape" in text
    assert "1 image(s)" in text and "2 image(s)" in text


def test_the_sweep_refuses_to_recommend_on_a_posture_it_never_tested() -> None:
    """The reviewer's 69%-coin-flip finding, closed at the source.

    The disqualification rule was always right; the evidence behind it was one sample for
    body-bold and none at all for header-bold. A model cannot be recommended on a posture
    it was never shown.
    """
    specs = list(CATALOG)
    results = _run_sweep({"claude-haiku-4-5": 1.0}, specs, repeat=3)
    assert all(r.qualified for r in results)
    assert sweep.recommend(results, specs) is None

    text = sweep.render(results, specs)
    assert "NO RECOMMENDATION" in text
    assert "header_not_bold" in text
    assert "SHIPS:" not in text


def test_the_current_set_has_the_gap_the_reviewer_found() -> None:
    """Pins the actual hole so closing it is a visible, deliberate change."""
    coverage = sweep.posture_coverage(CATALOG)
    assert coverage["header_not_bold"] == 0, "closed upstream — drop this test"
    assert coverage["body_bold"] == 1


def test_repeats_are_not_evidence() -> None:
    """Re-sending one PNG is not a second sample of the posture.

    The previous version of this test asserted the opposite and codified the flaw as
    intended behaviour: `--repeat 100` reported "100 sample(s), an error rate up to 3%
    would go unseen" from a single image, and `--repeat 3` — the default — cleared the
    gate on its own.
    """
    assert sweep.posture_coverage(CATALOG)["body_bold"] == 1
    # No repeat parameter exists to inflate it any more.
    assert sweep.posture_coverage(list(CATALOG) * 100)["body_bold"] == 1


def test_repeats_cannot_clear_the_evidence_gate() -> None:
    for repeat in (1, 3, 100):
        problems = sweep.evidence_problems(CATALOG, repeat=repeat)
        assert any("header_not_bold" in p for p in problems), repeat
        assert any("body_bold" in p for p in problems), repeat


def test_a_hundred_repeats_of_one_image_does_not_claim_a_small_blind_spot() -> None:
    specs = [s for s in CATALOG if s.name == "tc04_bold_warning_body"]
    results = _run_sweep({"claude-opus-5": 1.0}, specs, repeat=100)
    text = sweep.render(results, specs)
    assert "1 fixture(s) x 100 run(s)" in text
    # The blind-spot figure is bounded by the fixture count, so it stays at 95%.
    assert "up to 95% would go unseen" in text
    assert "NO RECOMMENDATION" in text


def test_the_report_prints_fixtures_and_runs_separately() -> None:
    """A single 'samples' figure cannot tell two labels from one label sent twice."""
    specs = evidence_complete_specs()
    text = sweep.render(_run_sweep({"claude-opus-5": 1.0}, specs, repeat=3), specs)
    assert "fixture(s) x 3 run(s)" in text
    assert "Blind-spot figures come from the fixture count alone" in text


def test_too_few_runs_per_fixture_also_blocks_a_recommendation() -> None:
    """Distinct fixtures bound the posture claim; runs bound the stability claim."""
    specs = evidence_complete_specs()
    thin = _run_sweep({"claude-opus-5": 1.0}, specs, repeat=1)
    assert sweep.recommend(thin, specs) is None
    assert any("--repeat 1" in p for p in sweep.evidence_problems(specs, repeat=1))

    enough = _run_sweep({"claude-opus-5": 1.0}, specs, repeat=3)
    assert sweep.recommend(enough, specs) is not None


def test_recommend_requires_the_spec_set() -> None:
    """An optional `specs` meant any forgetful caller skipped the evidence gate."""
    results = _run_sweep({"claude-opus-5": 1.0}, list(CATALOG), repeat=3)
    with pytest.raises(TypeError):
        sweep.recommend(results)  # type: ignore[call-arg]


def test_a_single_sample_proves_almost_nothing_and_the_report_says_so() -> None:
    """One clean read is consistent with a model that is wrong 95% of the time."""
    assert sweep.undetectable_error_rate(1) == pytest.approx(0.95)
    assert sweep.undetectable_error_rate(3) == pytest.approx(0.632, abs=0.01)
    assert sweep.undetectable_error_rate(30) < 0.10
    text = sweep.render(_run_sweep({"claude-opus-5": 1.0}, list(CATALOG)), list(CATALOG))
    assert "would go unseen" in text


def test_repeat_actually_runs_each_label_more_than_once() -> None:
    specs = list(CATALOG[:2])
    once = _run_sweep({"claude-opus-5": 1.0}, specs, repeat=1)[0]
    thrice = _run_sweep({"claude-opus-5": 1.0}, specs, repeat=3)[0]
    assert len(thrice.runs) == 3 * len(once.runs)
    assert thrice.report.total == 3 * once.report.total


def test_the_warn_fp_column_carries_its_denominator() -> None:
    """The sin fixed in the Tier A report, fixed in the table that picks the model.

    `0` reads identically whether it was 0-of-4 or 0-of-1, and this is where the decision
    is made.
    """
    specs = evidence_complete_specs()
    results = _run_sweep({"claude-opus-5": 1.0}, specs, repeat=3)
    text = sweep.render(results, specs)
    checked = len(results[0].report.warning_violations)
    assert checked > 0
    assert f"0/{checked}" in text
    assert "false passes / warning-violation rows checked" in text


def test_prices_cover_the_default_sweep() -> None:
    from eval.pricing import DEFAULT_SWEEP, price_for

    for model in DEFAULT_SWEEP:
        assert price_for(model) is not None, model


def test_an_unpriced_model_shows_no_price_rather_than_a_made_up_one() -> None:
    from eval.pricing import price_for

    assert price_for("claude-not-a-model") is None
    specs = evidence_complete_specs()
    results = _run_sweep({"claude-not-a-model": 2.0}, specs, repeat=3)
    assert results[0].priced is False
    assert "no price" in sweep.render(results, specs)
    # Evidence is sufficient here, so the None is about the missing price alone.
    assert sweep.evidence_problems(specs, repeat=3) == []
    assert sweep.recommend(results, specs) is None


def test_percentile_is_nearest_rank() -> None:
    assert sweep.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert sweep.percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    assert sweep.percentile([], 0.95) == 0.0


def test_the_sweep_is_opt_in_and_never_runs_by_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CI command cannot reach the live path — that is what keeps the build free."""
    assert main([]) == golden_exit()
    assert "MODEL-TIER SWEEP" not in capsys.readouterr().out


def test_a_dry_run_estimates_the_spend_and_stops() -> None:
    done = _run(["-m", "eval.run", "--model", "claude-opus-5", "--dry-run"])
    assert done.returncode == golden_exit()
    assert "Estimated spend" in done.stdout
    assert "Nothing was spent" in done.stdout
    assert "MODEL-TIER SWEEP" not in done.stdout


def test_the_sweep_skips_rather_than_fails_offline() -> None:
    """An offline machine has not regressed; it has not measured."""
    done = _run(["-m", "eval.run", "--model", "claude-haiku-4-5"], ANTHROPIC_API_KEY="")
    assert done.returncode == golden_exit()
    assert "ANTHROPIC_API_KEY is not set" in done.stdout
    assert "not a failure" in done.stdout


def test_the_sweep_does_not_disturb_the_tier_a_status_line() -> None:
    done = _run(["-m", "eval.run", "--model", "claude-opus-5", "--dry-run"])
    assert done.stdout.strip().split("\n")[-1].startswith("::labelproof-eval::")


def test_fixture_images_exist_for_every_spec() -> None:
    """The sweep sends real pixels; a missing PNG must be a clear error, not a silent zero."""
    for spec in CATALOG:
        assert sweep.image_inputs(spec), spec.name


# --- expectations are honest -----------------------------------------------------------------

PASSING_VERDICTS = {v.value for v in (Verdict.MATCH, Verdict.NOT_APPLICABLE)}


def renders_a_warning_defect(spec: LabelSpec) -> bool:
    """Does this spec draw a label that violates 27 CFR 16.21/16.22?

    Named and reused so the guard below can exercise the predicate itself rather than a
    tautology over its output.
    """
    return (
        not spec.include_warning
        or spec.warning_header_case != "upper"
        or not spec.warning_header_bold
        or spec.warning_body_bold
        or spec.warning_text is not None
    )


def expectation_violations(specs: list[LabelSpec]) -> list[str]:
    """Fixtures that render a warning defect but expect the warning to pass."""
    bad = []
    for spec in specs:
        if not renders_a_warning_defect(spec):
            continue
        expected = spec.expect.get("government_warning")
        if expected is None or expected in PASSING_VERDICTS:
            bad.append(spec.name)
    return bad


def test_no_fixture_expects_a_warning_violation_to_pass() -> None:
    """A golden set that expected a violation to be a Match would encode the bug."""
    defective = [s for s in CATALOG if renders_a_warning_defect(s)]
    assert defective, "the set must contain warning defects at all"
    assert expectation_violations(list(CATALOG)) == []


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"warning_body_bold": True}, "body_bold"),
        ({"warning_header_bold": False}, "header_not_bold"),
        ({"warning_header_case": "title"}, "header_case"),
        ({"include_warning": False}, "absent"),
        ({"warning_text": "Drink responsibly."}, "text_altered"),
    ],
)
def test_the_expectation_check_catches_each_kind_of_liar(
    changes: dict[str, object], label: str
) -> None:
    """Guards the guard, and actually runs it.

    The previous version reduced to `assert "match" in {"match", "not_applicable"}` — it
    never invoked the predicate it claimed to protect, so deleting a clause from the
    defect check (say `warning_body_bold`) silently stopped tc04 being examined and
    nothing went red. Each case here fails if its clause is removed.
    """
    liar = CATALOG[0].with_(
        name=f"liar_{label}", expect={"government_warning": "match"}, **changes
    )
    assert renders_a_warning_defect(liar), f"the defect predicate ignores {label}"
    assert expectation_violations([liar]) == [f"liar_{label}"]


def test_the_expectation_check_also_catches_a_missing_expectation() -> None:
    """`expect={}` is the same off-switch as `expect={"...": "match"}`."""
    silent = CATALOG[0].with_(name="silent", warning_body_bold=True, expect={})
    assert expectation_violations([silent]) == ["silent"]


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
