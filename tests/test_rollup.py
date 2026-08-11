"""The latency, error and cost rollup (LP-119, LP-124, OPS-1, OPS-4, OPS-5).

This script produces the number that gets quoted in a status update, so what is under
test is mostly honesty rather than arithmetic: does it say how many samples it had, does
it refuse to dress up five runs as a p95, and does it admit what it could not read.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import rollup

# --- helpers -------------------------------------------------------------------------


def line(event: str, **fields: Any) -> str:
    payload: dict[str, Any] = {"event": event, "ts": 1786464776.0}
    payload.update(fields)
    return json.dumps(payload, sort_keys=True)


def requests(*durations: int) -> list[str]:
    return [line("request_complete", duration_ms=d, status=200) for d in durations]


def verifications(*durations: int) -> list[str]:
    return [
        line("verify_complete", duration_ms=d, recommendation="ready_to_approve")
        for d in durations
    ]


def read(*lines: str) -> rollup.Reading:
    return rollup.read(list(lines))


# --- percentiles (J-06) --------------------------------------------------------------


def test_a_percentile_is_always_an_observation_that_happened() -> None:
    """Nearest-rank, not interpolated. Someone has to be able to point at the request."""
    samples = [10, 20, 30, 40]
    for p in (1, 25, 50, 75, 95, 100):
        assert rollup.percentile(samples, p) in samples


def test_the_median_of_an_even_sample_does_not_invent_a_midpoint() -> None:
    assert rollup.percentile([10, 20, 30, 40], 50) == 20


def test_p100_is_the_maximum() -> None:
    assert rollup.percentile([5, 900, 12], 100) == 900


def test_p95_of_twenty_samples_is_the_nineteenth() -> None:
    assert rollup.percentile(list(range(1, 21)), 95) == 19


def test_a_percentile_of_nothing_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError):
        rollup.percentile([], 95)


# --- reading a real, messy log --------------------------------------------------------


def test_it_reads_the_request_and_verification_series_separately() -> None:
    """`/health` is an HTTP request and is not a verification. Mixing them flatters p95."""
    reading = read(*requests(3, 4, 2400), *verifications(2400))
    assert reading.latencies[rollup.REQUEST_SERIES] == [3, 4, 2400]
    assert reading.latencies[rollup.VERIFY_SERIES] == [2400]


def test_stage_lines_become_one_series_each() -> None:
    reading = read(
        line("stage_complete", stage="extract", duration_ms=2610, ok=True),
        line("stage_complete", stage="compare", duration_ms=2, ok=True),
        line("stage_complete", stage="extract", duration_ms=2400, ok=True),
    )
    assert reading.latencies["extract"] == [2610, 2400]
    assert reading.latencies["compare"] == [2]


def test_plain_text_lines_are_skipped_and_counted() -> None:
    """Production stdout is our JSON plus uvicorn's prose. Both arrive here."""
    reading = read(
        'INFO:     Started server process [1]',
        'INFO:     Application startup complete.',
        *requests(120),
    )
    assert reading.parsed == 1
    assert reading.skipped == 2


def test_the_skipped_count_reaches_the_report() -> None:
    """A rollup over twelve lines must not look like a rollup over ten thousand."""
    reading = read("not json at all", *requests(120))
    assert "Lines skipped | 1" in rollup.render(reading, ["logs.jsonl"])


def test_a_fly_style_prefix_does_not_hide_the_json() -> None:
    """`fly logs` prefixes each line with the instance and stream."""
    reading = read(
        '2026-08-11T09:00:00Z app[abc123] iad [info] '
        + line("request_complete", duration_ms=140, status=200)
    )
    assert reading.latencies[rollup.REQUEST_SERIES] == [140]


def test_json_that_is_not_a_log_line_is_not_counted_as_one() -> None:
    reading = read('{"hello": "world"}', '[1, 2, 3]', *requests(10))
    assert reading.parsed == 1
    assert reading.skipped == 2


def test_the_time_window_comes_from_the_lines_themselves() -> None:
    reading = read(
        line("request_complete", duration_ms=1, status=200, ts=1000.0),
        line("request_complete", duration_ms=2, status=200, ts=1600.0),
    )
    assert reading.first_ts == 1000.0
    assert reading.last_ts == 1600.0
    assert "10.0 minutes" in rollup.render(reading, [])


# --- the small-sample guard (J-06) ---------------------------------------------------


def test_a_p95_from_five_runs_is_flagged_not_quietly_printed() -> None:
    report = rollup.render(read(*verifications(100, 200, 300, 400, 500)), [])
    assert "\\*" in report
    assert f"Fewer than {rollup.MIN_SAMPLES_FOR_P95} samples" in report


def test_twenty_runs_are_not_flagged() -> None:
    report = rollup.render(read(*verifications(*range(100, 120))), [])
    assert "Fewer than" not in report


def test_the_sample_size_is_on_every_row() -> None:
    report = rollup.render(read(*verifications(100, 200, 300)), [])
    matching = next(row for row in report.splitlines() if rollup.VERIFY_SERIES in row)
    assert "| 3 |" in matching


# --- the PERF-1 gate ------------------------------------------------------------------


def over_budget(*durations: int) -> list[str]:
    return [
        line("verify_over_budget", duration_ms=d, count=2, recommendation="needs_review")
        for d in durations
    ]


def pregated(*durations: int) -> list[str]:
    return [line("verify_pregated", duration_ms=d, count=1) for d in durations]


def test_a_p95_inside_the_target_says_so() -> None:
    report = rollup.render(read(*verifications(*([1200] * 25))), [])
    assert "within target" in report
    assert "OVER TARGET" not in report


def test_a_p95_over_the_target_is_stated_plainly() -> None:
    report = rollup.render(read(*verifications(*([9600] * 25))), [])
    assert "OVER TARGET" in report


def test_the_gate_is_graded_against_the_perf_1_target_not_the_enforced_deadline() -> None:
    """`request_budget_ms` now derives from the configured model's measured latency and
    is deliberately allowed above 5s rather than 503 on every call. Grading against it
    would mark a service as passing PERF-1 for meeting a budget relaxed to fit the model."""
    from api.config import Config

    assert Config().request_budget_ms > Config().latency_target_ms, (
        "this test is only meaningful while the deadline sits above the target"
    )
    report = rollup.render(read(*verifications(*([9600] * 25))), [])
    assert f"{Config().latency_target_ms}ms target" in report
    assert "OVER TARGET" in report


def test_budget_stopped_requests_are_in_the_latency_series() -> None:
    """The defect this exists to prevent.

    On a model whose median is above the deadline, most requests end in
    `verify_over_budget`. Feeding the series from `verify_complete` alone deletes exactly
    the slow tail a percentile exists to measure, and prints a comfortable p95 for a
    service that verified almost nothing.
    """
    reading = read(*verifications(*([4300] * 5)), *over_budget(*([20700] * 95)))
    assert len(reading.latencies[rollup.VERIFY_SERIES]) == 100
    assert int(rollup.percentile(reading.latencies[rollup.VERIFY_SERIES], 95)) == 20700
    assert "OVER TARGET" in rollup.render(reading, [])


def test_pregated_requests_are_in_the_latency_series_too() -> None:
    """The pre-gate pulls the other way — 300ms answers that would flatter the median.
    It is in the series because it is a real response, and the share that verified
    nothing is printed next to the number so it cannot be read alone."""
    reading = read(*verifications(2400, 2500), *pregated(310, 305))
    assert sorted(reading.latencies[rollup.VERIFY_SERIES]) == [305, 310, 2400, 2500]


def test_the_gate_line_names_the_share_that_verified_nothing() -> None:
    """A fast p95 earned by answering "not checked" quickly is not the gate being met,
    and the fact has to sit on the same line as the number that gets quoted."""
    report = rollup.render(read(*verifications(*([2400] * 5)), *over_budget(*([20700] * 95))), [])
    gate = report[report.index("PERF-1 gate") : report.index("This is server-side time")]
    assert "verified nothing" in gate
    assert "95 of 100" in gate


def test_a_clean_window_says_every_response_verified_something() -> None:
    """Silence there would read as "not measured" rather than "all good"."""
    report = rollup.render(read(*verifications(*([2400] * 25))), [])
    assert "actually verified a label" in report


def test_the_gate_is_measured_on_verifications_not_on_every_request() -> None:
    """Thousands of 2ms health checks would drag any all-request p95 under the target."""
    report = rollup.render(
        read(*requests(*([2] * 500)), *verifications(*([9600] * 25))), []
    )
    assert "OVER TARGET" in report


def test_the_gate_says_it_is_a_floor_not_the_stopwatch() -> None:
    """Server-side time excludes upload, network and render. Claiming otherwise is how
    a p95 becomes a promise the product cannot keep."""
    report = rollup.render(read(*verifications(*([1200] * 25))), [])
    assert "floor" in report
    assert "timed_run.py" in report


def test_a_log_with_no_verifications_says_the_gate_is_unmeasured() -> None:
    report = rollup.render(read(*requests(3, 4, 5)), [])
    assert "unmeasured" in report


# --- errors (LP-124, OPS-5) -----------------------------------------------------------


def failures(*pairs: tuple[str, str]) -> list[str]:
    return [
        line("request_failed", kind=kind, code=code, status=400) for kind, code in pairs
    ]


def statuses(*codes: int) -> list[str]:
    return [line("request_complete", duration_ms=10, status=code) for code in codes]


def test_the_error_rate_is_non_2xx_over_all_requests() -> None:
    report = rollup.render(read(*statuses(200, 200, 200, 400, 503)), [])
    assert "**40.0%**" in report


def test_client_and_server_errors_are_not_lumped_together() -> None:
    """A 4xx is the caller's request being wrong. A 5xx is ours. Averaging them into one
    number tells an operator nothing about who has to fix something."""
    payload = rollup.as_json(read(*statuses(200, 400, 400, 500)))
    assert payload["errors"]["by_status"] == {200: 1, 400: 2, 500: 1}


def test_a_clean_window_reports_a_zero_error_rate() -> None:
    report = rollup.render(read(*statuses(200, 200, 200)), [])
    assert "**0.0%**" in report


def test_the_taxonomy_breakdown_names_kind_and_code() -> None:
    report = rollup.render(
        read(*statuses(400, 400, 413), *failures(("user", "incomplete_application"),
                                                ("user", "incomplete_application"),
                                                ("user", "file_too_large"))),
        [],
    )
    assert "incomplete_application" in report
    assert "file_too_large" in report
    assert "`user`" in report


def test_the_taxonomy_is_ordered_by_how_often_it_happens() -> None:
    report = rollup.render(
        read(*failures(("provider", "provider_unavailable"),
                       ("user", "file_too_large"),
                       ("user", "file_too_large"))),
        [],
    )
    assert report.index("file_too_large") < report.index("provider_unavailable")


def test_a_200_that_verified_nothing_is_counted_even_though_it_is_not_an_error() -> None:
    """The pre-gate and the budget stop both answer 200. An error summary that reports
    0% while a quarter of the labels were never checked is true and misleading."""
    reading = read(
        *statuses(200, 200, 200, 200),
        *verifications(2400, 2500),
        line("verify_pregated", duration_ms=310, count=1),
        line("verify_over_budget", duration_ms=5000, count=2,
             recommendation="needs_review"),
    )
    report = rollup.render(reading, [])
    assert "**0.0%**" in report, "no HTTP request failed"
    assert "Unverified rate" in report
    assert "**50.0%**" in report


def test_the_unverified_paths_are_named_separately() -> None:
    """"Too blurry to read" and "ran out of time" need different fixes."""
    reading = read(
        *verifications(2400),
        line("verify_pregated", duration_ms=310, count=1),
        line("verify_over_budget", duration_ms=5000, count=1,
             recommendation="needs_review"),
    )
    report = rollup.render(reading, [])
    assert "images unreadable" in report
    assert "ran out of time" in report


def test_the_report_explains_why_the_two_rates_differ() -> None:
    reading = read(*statuses(200), *verifications(2400))
    report = rollup.render(reading, [])
    assert "different questions" in report


def test_degradations_are_counted_without_being_called_failures() -> None:
    """A retry that succeeded is not an error. A rising retry count is still the shape
    of an outage forming."""
    reading = read(
        *statuses(200, 200),
        line("provider_retry", attempt=1, kind="provider"),
        line("provider_retry", attempt=2, kind="provider"),
        line("circuit_breaker", status="open"),
    )
    report = rollup.render(reading, [])
    assert "Degraded but handled" in report
    assert "provider_retry` | 2" in report


def test_an_unhandled_exception_is_called_out_loudly() -> None:
    reading = read(*statuses(200, 500), line("unhandled_exception", kind="internal",
                                             code="internal_error"))
    report = rollup.render(reading, [])
    assert "unchosen failures" in report
    assert "unhandled_exception` x1" in report


def test_a_window_with_no_faults_says_so_rather_than_staying_silent() -> None:
    """Absence of a scary section reads as "not measured". Say it explicitly."""
    report = rollup.render(read(*statuses(200, 200)), [])
    assert "No `unhandled_exception` lines" in report


def test_a_log_with_no_requests_says_the_error_rate_is_unknowable() -> None:
    reading = read(line("app_started", model="claude-opus-5"))
    report = rollup.render(reading, [])
    assert "nothing can be said about error rate" in report


def test_json_output_carries_both_rates() -> None:
    payload = rollup.as_json(
        read(
            *statuses(200, 200, 200, 500),
            *verifications(2400),
            line("verify_pregated", duration_ms=310, count=1),
        )
    )
    assert payload["errors"]["error_rate"] == 0.25
    assert payload["errors"]["unverified_rate"] == 0.5
    assert payload["errors"]["pregated"] == 1


def test_the_error_summary_reads_the_log_a_real_failure_writes() -> None:
    """A 4xx driven through the real HTTP stack, rolled up by the real script."""
    import io as stdio
    import json as jsonlib

    from fastapi.testclient import TestClient

    from api import logging as applog
    from api.config import Config
    from api.main import create_app

    client = TestClient(create_app(config=Config(use_fake_provider=True)))
    stream = stdio.StringIO()
    applog.configure(stream=stream)

    client.post("/verify", files=[("images", ("x.png", b"not a png", "image/png"))],
                data={"application": jsonlib.dumps({"commodity": "spirits"})})
    client.get("/health")

    reading = rollup.read(stream.getvalue().splitlines())
    assert reading.requests == 2
    assert reading.status_class(4) == 1
    assert reading.taxonomy, "the failure did not reach the taxonomy breakdown"
    assert "Errors" in rollup.render(reading, [])


# --- cost -----------------------------------------------------------------------------


def test_cost_lines_are_totalled_and_averaged() -> None:
    reading = read(
        line("verification_cost", usd=0.08, input_tokens=9840, output_tokens=1120,
             cache_read_tokens=0, model="claude-opus-5", provider="anthropic"),
        line("verification_cost", usd=0.04, input_tokens=4900, output_tokens=560,
             cache_read_tokens=4000, model="claude-opus-5", provider="anthropic"),
    )
    report = rollup.render(reading, [])
    assert "$0.1200" in report
    assert "$0.0600" in report
    assert "claude-opus-5" in report


def test_sample_mode_runs_are_called_out_rather_than_averaged_in() -> None:
    """Sample mode makes no model call. A mean that includes it is not a cost per
    verification, and the deliverable it feeds is a cost analysis."""
    reading = read(
        line("verification_cost", usd=0.08, input_tokens=9840, output_tokens=1120,
             cache_read_tokens=0, model="claude-opus-5", provider="anthropic"),
        line("verification_cost", usd=0.0, input_tokens=0, output_tokens=0,
             cache_read_tokens=0, model="claude-opus-5", provider="fake:spec"),
    )
    report = rollup.render(reading, [])
    assert "sample mode" in report.lower()
    assert "fake:spec" in report


def test_cache_reads_with_no_writes_are_called_a_lower_bound() -> None:
    """Every cached prefix is written once before it can be read. A window with reads and
    zero writes is not a warm cache — it is an unpriced one, and writes cost 1.25x an
    input token."""
    reading = read(
        line("verification_cost", usd=0.04, input_tokens=4900, output_tokens=560,
             cache_read_tokens=4000, cache_creation_tokens=0, model="claude-opus-5",
             provider="anthropic"),
    )
    report = rollup.render(reading, [])
    assert "lower bound" in report
    assert "cache_creation_input_tokens" in report
    assert rollup.as_json(reading)["cost_usd"]["cache_writes_reported"] is False


def test_a_window_that_reports_cache_writes_carries_no_such_warning() -> None:
    reading = read(
        line("verification_cost", usd=0.09, input_tokens=4900, output_tokens=560,
             cache_read_tokens=4000, cache_creation_tokens=1684, model="claude-opus-5",
             provider="anthropic"),
    )
    assert "lower bound" not in rollup.render(reading, [])
    assert rollup.as_json(reading)["cost_usd"]["cache_writes_reported"] is True


def test_cache_write_tokens_are_reported_alongside_reads() -> None:
    reading = read(
        line("verification_cost", usd=0.09, input_tokens=100, output_tokens=10,
             cache_read_tokens=4000, cache_creation_tokens=1684, model="claude-opus-5",
             provider="anthropic"),
    )
    assert "Mean cache-write tokens | 1684" in rollup.render(reading, [])


def test_a_log_with_no_cost_lines_says_nothing_was_priced() -> None:
    assert "nothing was priced" in rollup.render(read(*requests(10)), [])


# --- output shape ---------------------------------------------------------------------


def test_the_stage_table_follows_pipeline_order_not_alphabetical() -> None:
    """A table that reads preprocess → extract → compare is a table someone can scan."""
    reading = read(
        line("stage_complete", stage="compare", duration_ms=2, ok=True),
        line("stage_complete", stage="extract", duration_ms=2610, ok=True),
        line("stage_complete", stage="preprocess", duration_ms=59, ok=True),
    )
    order = rollup.series_order(reading)
    assert order.index("preprocess") < order.index("extract") < order.index("compare")


def test_the_roll_up_caveat_is_in_the_committed_table_not_only_the_readme() -> None:
    """The README is not what gets handed over — `rollup.md` is. Someone reading only the
    artifact will try to add the stage column up."""
    reading = read(
        line("stage_complete", stage="preprocess", duration_ms=59, ok=True),
        line("stage_complete", stage="ingest", duration_ms=41, ok=True),
        line("stage_complete", stage="quality", duration_ms=18, ok=True),
    )
    report = rollup.render(reading, [])
    assert "Do not add the stage column up" in report
    assert "`ingest`, `quality`" in report


def test_a_report_without_the_roll_up_rows_carries_no_such_caveat() -> None:
    """Noise in a report is how readers learn to skip the prose."""
    reading = read(line("stage_complete", stage="extract", duration_ms=2610, ok=True))
    assert "Do not add the stage column up" not in rollup.render(reading, [])


def test_an_unrecognised_series_still_gets_reported() -> None:
    """A stage added later must appear without editing this script."""
    reading = read(line("stage_complete", stage="rerank", duration_ms=40, ok=True))
    assert "rerank" in rollup.render(reading, [])


def test_json_output_carries_the_sample_size_and_the_confidence_flag() -> None:
    payload = rollup.as_json(read(*verifications(100, 200, 300)))
    entry = payload["latency_ms"][rollup.VERIFY_SERIES]
    assert entry["n"] == 3
    assert entry["enough_for_p95"] is False
    assert entry["p50"] == 200


def test_the_report_is_markdown_that_can_be_committed() -> None:
    report = rollup.render(read(*verifications(*range(100, 130))), ["logs.jsonl"])
    assert report.startswith("# Latency and cost rollup")
    assert report.endswith("\n")
    assert "logs.jsonl" in report


# --- the command line ------------------------------------------------------------------


def test_it_reads_a_file_and_writes_a_report(tmp_path: Path, capsys: Any) -> None:
    log = tmp_path / "logs.jsonl"
    log.write_text("\n".join(verifications(*range(100, 130))) + "\n")
    out = tmp_path / "report.md"

    assert rollup.main([str(log), "--out", str(out)]) == 0
    assert "Latency and cost rollup" in out.read_text()


def test_it_reads_stdin_when_given_no_files(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(verifications(120, 130))))
    assert rollup.main([]) == 0
    assert "Latency and cost rollup" in capsys.readouterr().out


def test_an_empty_or_unparseable_log_fails_rather_than_printing_an_empty_table(
    tmp_path: Path, capsys: Any
) -> None:
    """A report over zero lines that looks like a report is worse than an error."""
    log = tmp_path / "nothing.log"
    log.write_text("INFO:     Application startup complete.\n")
    assert rollup.main([str(log)]) == 1
    assert "No LabelProof log lines" in capsys.readouterr().err


def test_json_mode_emits_parseable_json(tmp_path: Path, capsys: Any) -> None:
    log = tmp_path / "logs.jsonl"
    log.write_text("\n".join(verifications(120, 130)) + "\n")
    assert rollup.main([str(log), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["lines_read"] == 2


# --- against the real thing -----------------------------------------------------------


def test_it_rolls_up_the_log_a_real_verification_writes() -> None:
    """The end-to-end check that matters: the script and the service agree on the
    field names. A rollup that reads a log format nobody writes is worthless."""
    import json as jsonlib

    from fastapi.testclient import TestClient

    from api import logging as applog
    from api.config import Config
    from api.main import create_app

    root = Path(__file__).resolve().parents[1]
    sample = jsonlib.loads((root / "assets" / "samples" / "old_tom.json").read_text())
    application = {k: v for k, v in sample.items() if not k.startswith("_")}
    labels = root / "fixtures" / "labels"

    client = TestClient(create_app(config=Config(use_fake_provider=True)))
    stream = io.StringIO()
    applog.configure(stream=stream)

    for _ in range(3):
        client.post(
            "/verify",
            files=[
                ("images", (name, (labels / name).read_bytes(), "image/png"))
                for name in ("tc16_front_back_front.png", "tc16_front_back_back.png")
            ],
            data={"application": jsonlib.dumps(application)},
        )

    reading = rollup.read(stream.getvalue().splitlines())
    assert reading.skipped == 0
    assert len(reading.latencies[rollup.VERIFY_SERIES]) == 3
    assert len(reading.latencies[rollup.REQUEST_SERIES]) == 3
    for stage in ("preprocess", "extract", "compare"):
        assert len(reading.latencies[stage]) == 3
    assert len(reading.usd) == 3
