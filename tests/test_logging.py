"""The no-content rule (SEC-4), enforced structurally rather than by convention."""

import io
import json

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
