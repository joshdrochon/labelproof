"""The no-content rule (SEC-4) and the log schema (LP-117, OPS-5).

The rule is enforced structurally rather than by convention: the allowlist governs what
*we* log, and the record guard governs what everything else in the process can print.
"""

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


# --- the log schema (LP-117, OPS-5) ---------------------------------------------------

def _event_calls_in_source() -> dict[str, set[str]]:
    """Every `log("x")` / `warn("x")` / `error("x")` literal under `api/`."""
    import pathlib
    import re

    pattern = re.compile(r'(?:applog|lp_logging|logging)\.(log|warn|error)\(\s*"([a-z_0-9]+)"')
    root = pathlib.Path(__file__).resolve().parents[1] / "api"
    found: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        for call, event in pattern.findall(path.read_text()):
            found.setdefault(event, set()).add(call)
    # `api/logging.stage` writes this one directly, without going through the wrappers.
    found.setdefault("stage_complete", set()).add("log")
    return found


def _events_in_source() -> set[str]:
    return set(_event_calls_in_source())


def test_every_event_the_code_emits_is_in_the_schema() -> None:
    """The schema is a promise. Without this, an undocumented event breaks it silently.

    Whoever adds an event adds a row to `EVENTS` in the same change — that is the whole
    cost, and it is what keeps the operator-facing doc from becoming fiction.
    """
    missing = sorted(_events_in_source() - set(lp_logging.EVENTS))
    assert not missing, (
        f"These events are emitted but not documented in api.logging.EVENTS: {missing}. "
        f"Add a row with its level and one line saying what it means (LP-117)."
    )


def test_the_schema_documents_no_event_that_does_not_exist() -> None:
    """A schema listing a retired event sends an operator hunting for a line that will
    never appear."""
    stale = sorted(set(lp_logging.EVENTS) - _events_in_source())
    assert not stale, f"api.logging.EVENTS documents events nothing emits: {stale}"


def test_the_schema_records_the_level_the_code_actually_uses() -> None:
    """`warn` recorded as INFO is worse than no schema — it is a wrong alert rule."""
    import logging as stdlib_logging

    call_levels = {
        "log": stdlib_logging.INFO,
        "warn": stdlib_logging.WARNING,
        "error": stdlib_logging.ERROR,
    }
    wrong: list[str] = []
    for event, calls in _event_calls_in_source().items():
        # `circuit_breaker` is genuinely emitted at two levels — opening is a warning,
        # closing is not. The schema records the more severe of the two.
        if lp_logging.EVENTS[event][0] != max(call_levels[call] for call in calls):
            wrong.append(event)
    assert not wrong, f"api.logging.EVENTS records the wrong level for: {wrong}"


def test_every_event_has_a_description_someone_could_act_on() -> None:
    for event, (_level, description) in lp_logging.EVENTS.items():
        assert description.strip(), f"{event} has no description"
        assert description.endswith("."), f"{event}: describe it in a sentence"


# --- correlation (LP-117) -------------------------------------------------------------

def test_the_request_id_is_generated_not_accepted() -> None:
    """An id chosen by the caller can be forged to blend two agents' requests together,
    and correlation is the only reason to have one."""
    first = lp_logging.new_request_id()
    second = lp_logging.new_request_id()
    assert first != second
    assert first.startswith("req_")


def test_a_line_written_outside_a_request_carries_no_empty_id(_capture: io.StringIO) -> None:
    """Startup lines have no request to correlate to. An empty id is not an id."""
    lp_logging.set_request_id("")
    lp_logging.log("app_started", model="claude-opus-5")
    assert "request_id" not in _lines(_capture)[0]


# --- the traceback hole the allowlist cannot reach (SEC-4) ----------------------------

def _foreign_record(exc=None, msg="boom", args=()):  # type: ignore[no-untyped-def]
    """A record as uvicorn, asyncio or any library would create it."""
    import logging as stdlib_logging
    import sys

    exc_info = None
    if exc is not None:
        try:
            raise exc
        except BaseException:
            exc_info = sys.exc_info()
    factory = stdlib_logging.getLogRecordFactory()
    return factory("uvicorn.error", stdlib_logging.ERROR, __file__, 1, msg, args, exc_info)


def _rendered(record) -> str:  # type: ignore[no-untyped-def]
    import logging as stdlib_logging

    return stdlib_logging.Formatter("%(message)s").format(record)


def test_a_foreign_logger_cannot_print_a_traceback() -> None:
    """uvicorn logs `logger.error(..., exc_info=exc)`. The message is safe; the
    traceback is the leak."""
    lp_logging.install_stdout_guard()
    record = _foreign_record(ValueError("brand is OLD TOM DISTILLERY"))
    assert record.exc_info is None
    assert record.exc_text is None


def test_the_exceptions_own_message_never_survives() -> None:
    """A pydantic ValidationError quotes the input that failed — here, label text."""
    lp_logging.install_stdout_guard()
    secret = "STONE'S THROW BOURBON"
    assert secret not in _rendered(_foreign_record(ValueError(secret)))


def test_an_exception_passed_as_the_message_is_redacted() -> None:
    lp_logging.install_stdout_guard()
    secret = "OLD TOM DISTILLERY"
    assert secret not in _rendered(_foreign_record(msg=RuntimeError(secret)))


def test_an_exception_passed_as_a_format_argument_is_redacted() -> None:
    """`logger.error("call failed: %s", exc)` is the other common shape."""
    lp_logging.install_stdout_guard()
    secret = "BARDSTOWN, KENTUCKY"
    record = _foreign_record(msg="call failed: %s", args=(ValueError(secret),))
    assert secret not in _rendered(record)


def test_an_exception_in_a_dict_style_format_argument_is_redacted() -> None:
    lp_logging.install_stdout_guard()
    secret = "KENTUCKY STRAIGHT BOURBON"
    record = _foreign_record(msg="failed: %(why)s", args=({"why": ValueError(secret)},))
    assert secret not in _rendered(record)


def test_the_suppression_is_visible_rather_than_silent() -> None:
    """Silently deleting a traceback makes a broken service look like a quiet one."""
    lp_logging.install_stdout_guard()
    rendered = _rendered(_foreign_record(ValueError("x")))
    assert lp_logging.REDACTION_NOTE in rendered
    assert "ValueError" in rendered, "the exception type is safe and worth keeping"


def test_ordinary_foreign_lines_are_left_alone() -> None:
    """uvicorn's startup and access lines are how an ops team knows it is alive."""
    lp_logging.install_stdout_guard()
    assert _rendered(_foreign_record(msg="Application startup complete.")) == (
        "Application startup complete."
    )


def test_our_own_lines_are_unaffected_by_the_guard(_capture: io.StringIO) -> None:
    lp_logging.install_stdout_guard()
    lp_logging.log("verify_complete", duration_ms=1200, verdict="match")
    assert _lines(_capture)[-1]["duration_ms"] == 1200


def test_the_guard_is_installed_by_configure() -> None:
    lp_logging.uninstall_stdout_guard()
    lp_logging.configure(stream=io.StringIO())
    assert _foreign_record(ValueError("secret")).exc_info is None


def test_the_guard_can_be_declined_in_code_but_never_by_environment() -> None:
    """A compliance control an env var can switch off is not a control."""
    import inspect

    lp_logging.uninstall_stdout_guard()
    lp_logging.configure(stream=io.StringIO(), guard_stdout=False)
    assert _foreign_record(ValueError("secret")).exc_info is not None

    source = inspect.getsource(lp_logging)
    for switch in ("os.environ", "getenv"):
        assert switch not in source, "no environment switch on the no-content rule"
    lp_logging.install_stdout_guard()


def test_installing_twice_does_not_lose_the_original_factory() -> None:
    import logging as stdlib_logging

    lp_logging.uninstall_stdout_guard()
    original = stdlib_logging.getLogRecordFactory()
    lp_logging.install_stdout_guard()
    lp_logging.install_stdout_guard()
    lp_logging.uninstall_stdout_guard()
    assert stdlib_logging.getLogRecordFactory() is original
    lp_logging.install_stdout_guard()


# --- the doc cannot drift from the code (LP-117, ENG-5) -------------------------------

def _readme_table(marker: str) -> dict[str, list[str]]:
    """Parse the marked Markdown table out of README.md into {first cell: [cells]}."""
    import pathlib
    import re

    readme = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()
    block = re.search(
        rf"<!-- {marker}:BEGIN -->(.*?)<!-- {marker}:END -->", readme, re.S
    )
    assert block, f"README.md is missing the {marker} block that documents the log schema"

    rows: dict[str, list[str]] = {}
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        name = cells[0].strip("`")
        if name in ("Field", "Event"):
            continue
        rows[name] = cells[1:]
    return rows


def test_the_readme_documents_exactly_the_allowlisted_fields() -> None:
    """An operator reading the README must not find a field that does not exist, and
    must not be surprised by one in a log line that the README never mentioned."""
    documented = set(_readme_table("LOG-FIELDS"))
    assert documented == set(lp_logging.ALLOWED_FIELDS), (
        f"README.md field table is out of step with ALLOWED_FIELDS. "
        f"Missing from the README: {sorted(set(lp_logging.ALLOWED_FIELDS) - documented)}. "
        f"In the README but not allowlisted: {sorted(documented - set(lp_logging.ALLOWED_FIELDS))}."
    )


def test_every_documented_field_says_what_it_carries() -> None:
    for name, cells in _readme_table("LOG-FIELDS").items():
        assert cells and cells[0].strip(), f"README.md documents `{name}` with no description"


def test_the_readme_documents_exactly_the_events_the_schema_declares() -> None:
    documented = set(_readme_table("LOG-EVENTS"))
    assert documented == set(lp_logging.EVENTS), (
        f"README.md event table is out of step with api.logging.EVENTS. "
        f"Missing from the README: {sorted(set(lp_logging.EVENTS) - documented)}. "
        f"In the README but not in EVENTS: {sorted(documented - set(lp_logging.EVENTS))}."
    )


def test_the_readme_records_the_same_level_as_the_schema() -> None:
    import logging as stdlib_logging

    for event, cells in _readme_table("LOG-EVENTS").items():
        expected = stdlib_logging.getLevelName(lp_logging.EVENTS[event][0])
        assert cells[0] == expected, (
            f"README.md says {event} is {cells[0]}; api.logging.EVENTS says {expected}"
        )


def test_the_readme_states_the_no_content_rule_rather_than_only_implying_it() -> None:
    """SEC-4 is a compliance requirement. It is documented where an operator reads, not
    only where a developer reads."""
    import pathlib

    readme = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "SEC-4" in readme
    assert "raises on anything else" in readme or "raises on anything" in readme
