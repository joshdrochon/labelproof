"""The no-content rule (SEC-4) and the log schema (LP-117, OPS-5).

The rule is enforced structurally rather than by convention: the allowlist governs what
*we* log, and the record guard governs what everything else in the process can print.
"""

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


# --- the channel exc_info stripping does not reach (SEC-4) ---------------------------
#
# Process-wide containment lives in `api/security.py`; there is one record factory in this
# process and it is that one. What is tested here is the piece exported for it to call:
# exceptions passed as a record's message or format arguments, which carry the exception's
# `str()` to stdout with no traceback involved.


def _record(msg: object = "boom", args: object = ()):  # type: ignore[no-untyped-def]
    """A record as uvicorn, asyncio or any library would create it."""
    import logging as stdlib_logging

    return stdlib_logging.LogRecord(
        "uvicorn.error", stdlib_logging.ERROR, __file__, 1, msg, args, None
    )


def _rendered(record) -> str:  # type: ignore[no-untyped-def]
    import logging as stdlib_logging

    return stdlib_logging.Formatter("%(message)s").format(record)


def test_an_exception_passed_as_a_format_argument_is_redacted() -> None:
    """`logger.error("call failed: %s", exc)` — no exc_info, so traceback stripping does
    nothing, and the exception's own message reaches stdout."""
    label_text = "BARDSTOWN, KENTUCKY"
    record = lp_logging.scrub_exception_arguments(
        _record(msg="call failed: %s", args=(ValueError(label_text),))
    )
    assert label_text not in _rendered(record)
    assert "ValueError" in _rendered(record)


def test_an_exception_passed_as_the_message_is_redacted() -> None:
    """`logger.error(exc)` — the other natural spelling."""
    label_text = "OLD TOM DISTILLERY"
    record = lp_logging.scrub_exception_arguments(_record(msg=RuntimeError(label_text)))
    assert label_text not in _rendered(record)
    assert "RuntimeError" in _rendered(record)


def test_an_exception_in_a_dict_style_format_argument_is_redacted() -> None:
    label_text = "KENTUCKY STRAIGHT BOURBON"
    record = lp_logging.scrub_exception_arguments(
        _record(msg="failed: %(why)s", args=({"why": ValueError(label_text)},))
    )
    assert label_text not in _rendered(record)


def test_a_pydantic_validation_error_is_the_case_this_exists_for() -> None:
    """Not hypothetical: extraction responses are validated on receipt, and a
    ValidationError renders the input that failed — which on that path is label text."""
    import pydantic

    class Extracted(pydantic.BaseModel):
        confidence: float

    try:
        Extracted(confidence="STONE'S THROW BOURBON")  # type: ignore[arg-type]
    except pydantic.ValidationError as exc:
        caught: BaseException = exc
    else:  # pragma: no cover - the model must reject this
        raise AssertionError("expected a ValidationError")

    assert "STONE'S THROW BOURBON" in str(caught), "premise of this test has changed"
    record = lp_logging.scrub_exception_arguments(_record(msg="bad: %s", args=(caught,)))
    assert "STONE'S THROW BOURBON" not in _rendered(record)


def test_the_redaction_is_visible_rather_than_silent() -> None:
    """Silently deleting the payload makes a broken service look like a quiet one."""
    rendered = _rendered(
        lp_logging.scrub_exception_arguments(_record(msg="%s", args=(ValueError("x"),)))
    )
    assert lp_logging.REDACTION_NOTE in rendered


def test_ordinary_log_lines_pass_through_untouched() -> None:
    """uvicorn's startup and access lines are how an ops team knows it is alive."""
    record = lp_logging.scrub_exception_arguments(
        _record(msg="Application startup complete.")
    )
    assert _rendered(record) == "Application startup complete."


def test_ordinary_format_arguments_pass_through_untouched() -> None:
    record = lp_logging.scrub_exception_arguments(
        _record(msg="%s - %s", args=("GET /health", 200))
    )
    assert _rendered(record) == "GET /health - 200"


def test_exc_info_is_left_alone_because_it_belongs_to_the_other_layer() -> None:
    """Two record factories both claiming to own the traceback is the bug this split
    avoids — each captures the other as "the original"."""
    import sys as stdlib_sys

    try:
        raise ValueError("x")
    except ValueError:
        info = stdlib_sys.exc_info()

    import logging as stdlib_logging

    record = stdlib_logging.LogRecord(
        "uvicorn.error", stdlib_logging.ERROR, __file__, 1, "boom", (), info
    )
    assert lp_logging.scrub_exception_arguments(record).exc_info is info


def test_this_module_installs_no_record_factory() -> None:
    """One factory per process, and it is `api.security.install_log_containment`."""
    import inspect

    code = "\n".join(
        stripped
        for line in inspect.getsource(lp_logging).splitlines()
        if not (stripped := line.strip()).startswith("#")
    )
    assert "logging.setLogRecordFactory(" not in code


def test_the_no_content_rule_has_no_environment_switch() -> None:
    """A compliance control an env var can turn off is not a control."""
    import inspect

    source = inspect.getsource(lp_logging)
    for switch in ("os.environ", "getenv"):
        assert switch not in source


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


# --- the ops runbook is executable, not aspirational (LP-125, ENG-5) ------------------

def _readme() -> str:
    import pathlib

    return (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()


def test_every_command_the_runbook_gives_names_a_script_that_exists() -> None:
    """A runbook that tells an operator to run something that was renamed is worse than
    no runbook — they conclude the whole document is stale."""
    import importlib
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    named = set(re.findall(r"python -m scripts\.([a-z_]+)", _readme()))
    assert named, "the ops section documents no commands at all"
    for name in sorted(named):
        assert (root / "scripts" / f"{name}.py").exists(), (
            f"README.md tells an operator to run `scripts.{name}`, which does not exist"
        )
        assert hasattr(importlib.import_module(f"scripts.{name}"), "main")


def test_every_event_the_runbook_greps_for_is_a_real_event() -> None:
    """The `jq` recipes name events. A recipe for an event nothing emits returns nothing,
    which reads as "the service is healthy"."""
    import re

    referenced = set(re.findall(r'\.event == "([a-z_]+)"', _readme()))
    unknown = sorted(referenced - set(lp_logging.EVENTS))
    assert not unknown, f"README.md greps for events that do not exist: {unknown}"


def test_the_runbook_says_where_the_log_actually_is() -> None:
    readme = _readme()
    assert "fly logs" in readme
    assert "stdout" in readme


def test_the_runbook_states_the_cost_figure_is_list_price() -> None:
    """A cost quoted without saying it is list price computed locally is a cost someone
    will put in a budget."""
    readme = _readme()
    assert "list price" in readme.lower()
    assert "not a bill" in readme.lower()


def test_the_runbook_warns_that_sample_mode_costs_nothing() -> None:
    assert "Sample-mode runs cost nothing" in _readme()


def test_the_runbook_has_a_triage_table_for_when_things_are_wrong() -> None:
    """The hardest moment to write documentation is during an incident."""
    readme = _readme()
    assert "When something is wrong" in readme
    for signal in ("unhandled_exception", "provider_retry", "sample_mode"):
        assert signal in readme
