"""The no-content rule (SEC-4), enforced structurally rather than by convention."""

import io
import json
import logging

import pytest

from api import logging as lp_logging
from api.logging import ContentInLogError


@pytest.fixture(autouse=True)
def _capture() -> io.StringIO:
    stream = io.StringIO()
    lp_logging.configure(stream=stream)
    return stream


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# --- the rule -----------------------------------------------------------------------

def test_logging_a_label_value_raises() -> None:
    """There is no channel through which a brand name reaches a log line."""
    with pytest.raises(ContentInLogError):
        lp_logging.log("extracted", brand_name="OLD TOM DISTILLERY")


@pytest.mark.parametrize(
    "field",
    ["brand_name", "extracted", "value", "warning_text", "text", "producer", "address"],
)
def test_content_bearing_fields_are_all_rejected(field: str) -> None:
    with pytest.raises(ContentInLogError):
        lp_logging.log("event", **{field: "anything"})


def test_the_error_explains_why_rather_than_just_refusing() -> None:
    with pytest.raises(ContentInLogError, match="SEC-4"):
        lp_logging.log("event", brand_name="x")


def test_allowlisted_fields_are_accepted(_capture: io.StringIO) -> None:
    lp_logging.log("verified", duration_ms=1200, verdict="match", confidence=0.95)
    assert _lines(_capture)[0]["verdict"] == "match"


def test_no_allowlisted_field_could_carry_label_text() -> None:
    """Audit: every allowed field is an id, a measurement, or a category."""
    suspicious = {"text", "value", "content", "name", "brand", "extracted", "raw"}
    assert not (lp_logging.ALLOWED_FIELDS & suspicious)


# --- correlation --------------------------------------------------------------------

def test_request_id_rides_on_every_line(_capture: io.StringIO) -> None:
    rid = lp_logging.new_request_id()
    lp_logging.log("one")
    lp_logging.log("two")
    assert all(line["request_id"] == rid for line in _lines(_capture))


def test_lines_are_one_json_object_each(_capture: io.StringIO) -> None:
    lp_logging.log("a", count=1)
    lp_logging.log("b", count=2)
    assert len(_lines(_capture)) == 2


def test_output_is_deterministically_ordered(_capture: io.StringIO) -> None:
    """Sorted keys so a diff between two runs is meaningful."""
    lp_logging.log("x", count=1, stage="extract")
    raw = _capture.getvalue()
    assert raw.index('"count"') < raw.index('"event"') < raw.index('"stage"')


# --- stage timing -------------------------------------------------------------------

def test_stage_logs_a_duration(_capture: io.StringIO) -> None:
    with lp_logging.stage("extract", image_index=0):
        pass
    line = _lines(_capture)[0]
    assert line["stage"] == "extract"
    assert line["duration_ms"] >= 0
    assert line["ok"] is True


def test_stage_records_failure_without_swallowing_the_exception(_capture: io.StringIO) -> None:
    with pytest.raises(ValueError), lp_logging.stage("extract"):
        raise ValueError("boom")
    assert _lines(_capture)[0]["ok"] is False


def test_stage_exposes_its_duration_to_the_caller() -> None:
    with lp_logging.stage("compare") as s:
        pass
    assert s.duration_ms >= 0


# --- `level` cannot be captured by a field ------------------------------------------


def test_a_field_named_level_is_rejected_rather_than_rerouted() -> None:
    """`stage("extract", level="debug")` used to type-check and then explode.

    `log()`'s `level` parameter was an ordinary one, so it competed with `**fields` for
    the name, and the only way to make the kwargs unpack assignable was a `# type:
    ignore` sitting directly on top of that hazard. `level` is positional-only now, so a
    stray `level=` lands in `fields` and meets the allowlist — which is the loud, correct
    outcome rather than a silenced one.
    """
    with pytest.raises(ContentInLogError, match="level"), lp_logging.stage(
        "extract", level="debug"
    ):
        pass


def test_the_severity_of_warn_and_error_still_gets_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Making `level` positional-only must not quietly demote every warning to info.

    `warn()` and `error()` were the only two callers passing `level=` by keyword. They
    now pass it positionally, and this asserts the severity that reaches the stdlib
    logger, which the JSON payload does not carry.
    """
    seen: list[int] = []
    monkeypatch.setattr(
        lp_logging._logger, "log", lambda level, message: seen.append(level)
    )

    lp_logging.log("app_started")
    lp_logging.warn("provider_unavailable", kind="provider")
    lp_logging.error("unhandled_exception", kind="internal")

    assert seen == [logging.INFO, logging.WARNING, logging.ERROR]
