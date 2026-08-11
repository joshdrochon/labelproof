"""27 CFR 16.22 appearance rules, and the abstention path that will dominate in practice.

The measured context behind these tests: extraction runs on Claude Haiku 4.5 because it
is the only model inside the five-second budget, and it is the weakest model available.
Judging bold from a photograph is the most likely thing it declines to answer. So the
`None` path is not an edge case here — it is the expected case, and it carries at least
as many tests as the answering path.

The property every test in this file is ultimately defending: **no combination of
typography signals produces a pass that the signals did not establish.**
"""

import itertools

import pytest

from api.models import BoundingBox, Finding, WarningTypography
from api.rules import typography

TRISTATE: list[bool | None] = [True, False, None]


def _codes(findings: tuple[Finding, ...] | list[Finding]) -> set[str]:
    return {f.code for f in findings}


# --- the tri-state contract -----------------------------------------------------------

def test_none_is_not_false() -> None:
    """The single most dangerous confusion in this module."""
    abstained = typography.assess(WarningTypography(header_is_bold=None))
    denied = typography.assess(WarningTypography(header_is_bold=False))

    assert "warning_header_bold_unverified" in _codes(abstained.findings)
    assert "warning_header_not_bold" in _codes(denied.findings)
    assert abstained.type_style_violations == ()
    assert denied.type_style_violations != ()


def test_none_is_not_true_either() -> None:
    """Abstention must not fall through into silence, which reads as a pass."""
    assert not typography.assess(WarningTypography()).is_clean


def test_a_signal_the_model_answered_well_is_clean() -> None:
    signals = WarningTypography(
        header_is_bold=True, body_is_bold=False, contrast_ok=True, relative_size=1.0
    )
    assert typography.assess(signals).is_clean


def test_all_three_bright_lines_must_be_answered_to_be_clean() -> None:
    """Contrast joined bold on this list after a confirmed false pass: a verbatim
    warning with contrast_ok=None reached Ready to approve."""
    answered = WarningTypography(header_is_bold=True, body_is_bold=False, contrast_ok=True)
    assert typography.assess(answered).is_clean
    for blank in ("header_is_bold", "body_is_bold", "contrast_ok"):
        signals = answered.model_copy(update={blank: None})
        assert not typography.assess(signals).is_clean, blank


# --- WARN-2, the heading in bold ------------------------------------------------------

def test_header_not_bold_is_a_violation() -> None:
    findings = typography.check_header_bold(WarningTypography(header_is_bold=False))
    assert [f.code for f in findings] == ["warning_header_not_bold"]
    assert findings[0].severity == typography.SEVERITY_VIOLATION
    assert findings[0].citation == "27 CFR 16.22"


def test_header_bold_unknown_is_unverified_not_silent() -> None:
    findings = typography.check_header_bold(WarningTypography(header_is_bold=None))
    assert [f.code for f in findings] == ["warning_header_bold_unverified"]
    assert findings[0].severity == typography.SEVERITY_UNVERIFIED


def test_header_bold_true_says_nothing() -> None:
    assert typography.check_header_bold(WarningTypography(header_is_bold=True)) == []


# --- WARN-7, the inverse rule ---------------------------------------------------------

@pytest.mark.tc("TC-04")
def test_body_bold_is_a_violation() -> None:
    findings = typography.check_body_not_bold(WarningTypography(body_is_bold=True))
    assert [f.code for f in findings] == ["warning_body_is_bold"]
    assert findings[0].severity == typography.SEVERITY_VIOLATION


@pytest.mark.tc("TC-04")
def test_body_bold_message_names_the_inverse_rule() -> None:
    """An agent reading this has to understand that bold is the *problem* here."""
    message = typography.check_body_not_bold(WarningTypography(body_is_bold=True))[0].message
    assert "must not be in bold" in message


def test_body_bold_unknown_is_unverified() -> None:
    findings = typography.check_body_not_bold(WarningTypography(body_is_bold=None))
    assert [f.code for f in findings] == ["warning_body_bold_unverified"]
    assert findings[0].severity == typography.SEVERITY_UNVERIFIED


def test_body_not_bold_says_nothing() -> None:
    assert typography.check_body_not_bold(WarningTypography(body_is_bold=False)) == []


def test_a_correct_heading_does_not_excuse_a_bold_body() -> None:
    """The failure mode this rule exists for: checking the heading and stopping."""
    signals = WarningTypography(header_is_bold=True, body_is_bold=True)
    assert "warning_body_is_bold" in _codes(typography.assess(signals).findings)


# --- WARN-5, prominence (LP-211) ------------------------------------------------------

@pytest.mark.tc("TC-06")
def test_shrunken_warning_raises_a_prominence_concern() -> None:
    assessment = typography.assess(WarningTypography(relative_size=0.45))
    assert "warning_less_prominent" in assessment.prominence_concerns


@pytest.mark.tc("TC-06")
def test_prominence_message_quantifies_how_much_smaller() -> None:
    finding = typography.check_prominence(WarningTypography(relative_size=0.45))[0]
    assert "55%" in finding.message


def test_same_size_as_the_rest_of_the_label_is_fine() -> None:
    assert typography.check_prominence(WarningTypography(relative_size=1.0)) == []


def test_larger_than_the_rest_of_the_label_is_fine() -> None:
    assert typography.check_prominence(WarningTypography(relative_size=1.6)) == []


def test_the_prominence_boundary_is_inclusive_of_concern() -> None:
    """At exactly the ratio, flag. Ties go toward the agent's attention."""
    at = typography.PROMINENCE_CONCERN_RATIO
    assert typography.check_prominence(WarningTypography(relative_size=at))
    assert not typography.check_prominence(WarningTypography(relative_size=at + 0.01))


def test_unknown_prominence_is_not_a_concern_but_is_admitted() -> None:
    """WARN-9: never claim to have checked size. Never block every label either."""
    assessment = typography.assess(
        WarningTypography(header_is_bold=True, body_is_bold=False, contrast_ok=True)
    )
    assert assessment.prominence_concerns == ()
    assert "relative_size" in assessment.unassessed
    note = next(f for f in assessment.findings if f.code == "warning_prominence_unassessed")
    assert note.severity == typography.SEVERITY_CONTEXT


def test_the_unassessed_note_never_changes_a_verdict() -> None:
    """A context finding is an admission, not an accusation."""
    signals = WarningTypography(header_is_bold=True, body_is_bold=False, contrast_ok=True)
    assessment = typography.assess(signals)
    assert assessment.is_clean
    assert assessment.findings  # it still says so out loud


def test_unassessed_note_is_one_line() -> None:
    assessment = typography.assess(
        WarningTypography(header_is_bold=True, body_is_bold=False, contrast_ok=True)
    )
    notes = [f for f in assessment.findings if f.severity == typography.SEVERITY_CONTEXT]
    assert len(notes) == 1
    assert "size" in notes[0].message


# --- WARN-5, buried text (LP-212) -----------------------------------------------------

@pytest.mark.tc("TC-06")
def test_low_contrast_is_a_prominence_concern() -> None:
    assessment = typography.assess(WarningTypography(contrast_ok=False))
    assert "warning_low_contrast" in assessment.prominence_concerns


@pytest.mark.tc("TC-06")
def test_low_contrast_points_at_the_region() -> None:
    finding = typography.check_contrast(WarningTypography(contrast_ok=False))[0]
    assert "outlined area" in finding.message


def test_good_contrast_says_nothing() -> None:
    assert typography.check_contrast(WarningTypography(contrast_ok=True)) == []


def test_unknown_contrast_blocks_match_and_asserts_nothing() -> None:
    """The confirmed false pass. 16.22(a)(1) states contrasting background as a
    requirement with a yes/no answer, so an abstention is a real gap — but it is a gap,
    not an accusation, so it never lands in prominence_concerns."""
    assessment = typography.assess(
        WarningTypography(header_is_bold=True, body_is_bold=False, contrast_ok=None)
    )
    assert "warning_contrast_unverified" in assessment.unconfirmed
    assert assessment.prominence_concerns == ()
    assert not assessment.is_clean


# --- the whole grid -------------------------------------------------------------------

@pytest.mark.parametrize(
    ("header_bold", "body_bold"), list(itertools.product(TRISTATE, TRISTATE))
)
def test_only_a_fully_answered_pair_can_be_clean(
    header_bold: bool | None, body_bold: bool | None
) -> None:
    """Exhaustive over the bright lines. Clean if and only if both were answered right."""
    assessment = typography.assess(
        WarningTypography(
            header_is_bold=header_bold, body_is_bold=body_bold, contrast_ok=True
        )
    )
    expected_clean = header_bold is True and body_bold is False
    assert assessment.is_clean is expected_clean


@pytest.mark.parametrize("signals", [
    WarningTypography(header_is_bold=h, body_is_bold=b, contrast_ok=c, relative_size=r)
    for h in TRISTATE
    for b in TRISTATE
    for c in TRISTATE
    for r in (None, 0.4, 1.0)
])
def test_assessment_never_reports_a_bucket_it_did_not_find(
    signals: WarningTypography,
) -> None:
    """Every code in a bucket must also appear in `findings`, and vice versa."""
    assessment = typography.assess(signals)
    bucketed = set(
        assessment.type_style_violations
        + assessment.unconfirmed
        + assessment.prominence_concerns
    )
    context = {
        f.code for f in assessment.findings if f.severity == typography.SEVERITY_CONTEXT
    }
    assert bucketed | context == _codes(assessment.findings)


def test_every_finding_cites_the_regulation() -> None:
    for signals in (
        WarningTypography(header_is_bold=False, body_is_bold=True, contrast_ok=False,
                          relative_size=0.3),
        WarningTypography(),
    ):
        for finding in typography.assess(signals).findings:
            assert finding.citation == "27 CFR 16.22"


# --- escalation trigger ---------------------------------------------------------------

def test_escalation_fires_when_a_bright_line_is_unresolved() -> None:
    signals = WarningTypography(header_is_bold=None, body_is_bold=False)
    assert typography.needs_escalation(signals, warning_text="GOVERNMENT WARNING: ...")


def test_escalation_fires_when_the_warning_could_not_be_read() -> None:
    answered = WarningTypography(header_is_bold=True, body_is_bold=False)
    assert typography.needs_escalation(answered, warning_text=None)
    assert typography.needs_escalation(answered, warning_text="  ")
    assert typography.needs_escalation(answered, warning_text="text", legible=False)


def test_escalation_does_not_fire_when_everything_was_answered() -> None:
    signals = WarningTypography(header_is_bold=True, body_is_bold=False)
    assert not typography.needs_escalation(signals, warning_text="GOVERNMENT WARNING: ...")


def test_escalation_does_not_fire_on_a_violation() -> None:
    """A `False` is an answer. Re-asking until the model relents is not a design."""
    signals = WarningTypography(header_is_bold=False, body_is_bold=True)
    assert not typography.needs_escalation(signals, warning_text="GOVERNMENT WARNING: ...")


def test_escalation_ignores_prominence_signals() -> None:
    """A stronger model cannot measure millimetres either (WARN-9)."""
    signals = WarningTypography(header_is_bold=True, body_is_bold=False, relative_size=None)
    assert not typography.needs_escalation(signals, warning_text="x")


def test_unresolved_signals_lists_only_the_bright_lines() -> None:
    signals = WarningTypography(header_is_bold=None, body_is_bold=None, contrast_ok=None)
    assert typography.unresolved_signals(signals) == ("header_is_bold", "body_is_bold")


def test_escalation_request_says_what_it_wants_and_why() -> None:
    request = typography.escalation_request(
        WarningTypography(header_is_bold=None, body_is_bold=False),
        image_index=1,
        bbox=BoundingBox(x0=0.1, y0=0.7, x1=0.9, y1=0.9),
        warning_text="GOVERNMENT WARNING: ...",
    )
    assert request.image_index == 1
    assert request.wanted == ("header_is_bold",)
    assert "header is bold" in request.reason


def test_escalation_request_for_an_unreadable_warning_asks_for_the_text_too() -> None:
    request = typography.escalation_request(
        WarningTypography(), image_index=0, legible=False
    )
    assert "warning_text" in request.wanted
    assert request.bbox is None


# --- escalation merge: the part that must never manufacture a pass --------------------

def _reread(**kwargs: bool | None) -> typography.WarningReread:
    return typography.WarningReread(typography=WarningTypography(**kwargs), model="strong")


def test_a_second_look_fills_a_blank() -> None:
    merged, findings = typography.adopt_reread(
        WarningTypography(header_is_bold=None, body_is_bold=False),
        _reread(header_is_bold=True),
    )
    assert merged.header_is_bold is True
    assert merged.body_is_bold is False
    assert findings == ()


def test_a_second_look_can_report_a_violation_the_first_pass_missed() -> None:
    merged, _ = typography.adopt_reread(
        WarningTypography(body_is_bold=None), _reread(body_is_bold=True)
    )
    assert merged.body_is_bold is True


def test_a_second_look_cannot_overturn_a_recorded_violation() -> None:
    """The whole safety case. A bigger model does not get to clear a violation."""
    merged, findings = typography.adopt_reread(
        WarningTypography(header_is_bold=False), _reread(header_is_bold=True)
    )
    assert merged.header_is_bold is None
    assert "warning_typography_disputed" in _codes(findings)


def test_a_second_look_cannot_erase_an_answer_by_abstaining() -> None:
    merged, findings = typography.adopt_reread(
        WarningTypography(header_is_bold=False, body_is_bold=True), _reread()
    )
    assert merged.header_is_bold is False
    assert merged.body_is_bold is True
    assert findings == ()


def test_disagreement_collapses_to_unknown_not_to_the_stronger_model() -> None:
    merged, findings = typography.adopt_reread(
        WarningTypography(body_is_bold=True), _reread(body_is_bold=False)
    )
    assert merged.body_is_bold is None
    assert findings[0].severity == typography.SEVERITY_UNVERIFIED


def test_agreement_stands() -> None:
    merged, findings = typography.adopt_reread(
        WarningTypography(header_is_bold=True, body_is_bold=False),
        _reread(header_is_bold=True, body_is_bold=False),
    )
    assert merged.header_is_bold is True
    assert findings == ()


def test_a_reread_with_nothing_in_it_changes_nothing() -> None:
    first = WarningTypography(header_is_bold=True, body_is_bold=False, relative_size=0.9)
    merged, findings = typography.adopt_reread(first, typography.WarningReread())
    assert merged == first
    assert findings == ()


def test_relative_size_is_kept_not_averaged() -> None:
    merged, _ = typography.adopt_reread(
        WarningTypography(relative_size=0.4), _reread_size(0.9)
    )
    assert merged.relative_size == 0.4


def test_relative_size_is_filled_when_absent() -> None:
    merged, _ = typography.adopt_reread(WarningTypography(), _reread_size(0.9))
    assert merged.relative_size == 0.9


def _reread_size(value: float) -> typography.WarningReread:
    return typography.WarningReread(typography=WarningTypography(relative_size=value))


@pytest.mark.parametrize(
    ("first", "second"), list(itertools.product(TRISTATE, TRISTATE))
)
def test_merge_never_invents_compliance(first: bool | None, second: bool | None) -> None:
    """Exhaustive: the merged value is True only when somebody actually saw True and
    nobody saw otherwise."""
    merged, _ = typography.adopt_reread(
        WarningTypography(header_is_bold=first), _reread(header_is_bold=second)
    )
    expected_true = (first is True and second is not False) or (
        first is None and second is True
    )
    assert (merged.header_is_bold is True) is expected_true


def test_the_rereader_protocol_accepts_a_stub() -> None:
    """The adapter is owned elsewhere; this proves the contract is implementable."""

    class Stub:
        name = "stub"

        def reread_warning(
            self, request: typography.WarningRereadRequest
        ) -> typography.WarningReread:
            return typography.WarningReread(
                warning_text="GOVERNMENT WARNING: ...",
                typography=WarningTypography(header_is_bold=True),
                model="stub-strong",
            )

    stub = Stub()
    assert isinstance(stub, typography.WarningRereader)
    request = typography.escalation_request(
        WarningTypography(), image_index=0, warning_text="x"
    )
    assert stub.reread_warning(request).model == "stub-strong"


def test_a_stub_that_abstains_is_a_valid_rereader() -> None:
    """A stronger model must be allowed to say it cannot tell, and that must be safe."""

    class Abstainer:
        name = "abstainer"

        def reread_warning(
            self, request: typography.WarningRereadRequest
        ) -> typography.WarningReread:
            return typography.WarningReread(typography=WarningTypography())

    first = WarningTypography(header_is_bold=None, body_is_bold=False, contrast_ok=True)
    merged, findings = typography.adopt_reread(
        first, Abstainer().reread_warning(
            typography.escalation_request(first, image_index=0, warning_text="x")
        )
    )
    assert merged.header_is_bold is None
    assert findings == ()
    assert not typography.assess(merged).is_clean
