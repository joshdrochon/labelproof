"""The timed-run evidence artifact (LP-120, PERF-1, PERF-2).

This script produces the table that backs the p95 claim, so the tests are mostly about
what the table refuses to say: it will not call sample-mode numbers a latency result, it
will not hide the runs that failed, it will not call run 1 cold, and it will not quietly
average away a server clock that disagrees with the stopwatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import timed_run
from scripts.timed_run import Reply, Report, Run

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "fixtures" / "labels"
SAMPLE = ROOT / "assets" / "samples" / "old_tom.json"


# --- helpers -------------------------------------------------------------------------


def application() -> dict[str, Any]:
    raw = json.loads(SAMPLE.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def images() -> list[tuple[str, bytes]]:
    return [(n, (LABELS / n).read_bytes()) for n in ("tc16_front_back_front.png",)]


def verdict_body(total: int = 2400, **overrides: Any) -> bytes:
    body: dict[str, Any] = {
        "request_id": "req_abc123",
        "aggregate": {"recommendation": "ready_to_approve", "rationale": "", "driving_field": None},
        "fields": [],
        "images": [],
        "timings_ms": {
            "ingest": 40, "quality": 18, "preprocess": 58,
            "extract": total - 60, "compare": 2, "adjudicate": 0, "total": total,
        },
        "cost": {"input_tokens": 9840, "output_tokens": 1120, "cache_read_tokens": 0,
                 "usd": 0.077},
    }
    body.update(overrides)
    return json.dumps(body).encode()


def replying(*replies: Reply, delay_s: float = 0.0) -> Any:
    """A poster that hands back canned replies in order, then repeats the last.

    `delay_s` makes the client stopwatch measure something, which is what the two-clock
    comparisons need.
    """
    import time as clock

    queue = list(replies)

    def post(path: str, content_type: str, body: bytes) -> Reply:
        assert path == "/verify"
        assert content_type.startswith("multipart/form-data; boundary=")
        if delay_s:
            clock.sleep(delay_s)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return post


def report_from(
    *replies: Reply, runs: int | None = None, delay_s: float = 0.0, **overrides: Any
) -> Report:
    count = runs if runs is not None else len(replies)
    fields: dict[str, Any] = {
        "url": "http://localhost:8000",
        "runs": timed_run.measure(
            count, replying(*replies, delay_s=delay_s), application(), images()
        ),
        "started_at": "2026-08-11 09:00:00Z",
        "image_names": ["front.png"],
        "image_bytes": 30935,
    }
    fields.update(overrides)
    return Report(**fields)


# --- the multipart body ---------------------------------------------------------------


def test_the_encoded_form_carries_the_application_and_every_image() -> None:
    content_type, body = timed_run.build_multipart(application(), images())
    assert content_type.startswith("multipart/form-data; boundary=----labelproof")
    assert b'name="application"' in body
    assert b'name="images"; filename="tc16_front_back_front.png"' in body
    assert b"OLD TOM DISTILLERY" in body


def test_each_request_gets_a_fresh_boundary() -> None:
    """A boundary reused across processes is a boundary that can collide with payload
    bytes. Cheap to make unique."""
    first, _ = timed_run.build_multipart(application(), images())
    second, _ = timed_run.build_multipart(application(), images())
    assert first != second


def test_the_image_bytes_survive_encoding_unmodified() -> None:
    _, body = timed_run.build_multipart(application(), images())
    assert images()[0][1] in body


def test_documentation_keys_in_a_sample_file_are_not_sent() -> None:
    """`assets/samples/old_tom.json` carries `_source` notes for humans."""
    payload, _ = timed_run.load_payload(
        "http://unused", str(SAMPLE), [str(LABELS / "tc01_old_tom_clean.png")]
    )
    assert not any(key.startswith("_") for key in payload)


# --- one run --------------------------------------------------------------------------


def test_a_successful_run_captures_both_clocks_and_the_stages() -> None:
    run = timed_run.run_once(1, replying(Reply(200, verdict_body(2400))), application(), images())
    assert run.ok
    assert run.server_total_ms == 2400
    assert run.stages["extract"] == 2340
    assert run.client_ms >= 0
    assert run.recommendation == "ready_to_approve"
    assert run.request_id == "req_abc123"


def test_a_failed_run_still_records_how_long_it_took_to_fail() -> None:
    """A slow failure is a latency fact. Dropping it makes the table optimistic."""
    body = json.dumps({"error": {"kind": "provider", "code": "provider_unavailable",
                                 "message": "down", "next_step": "retry"}}).encode()
    run = timed_run.run_once(1, replying(Reply(503, body)), application(), images())
    assert not run.ok
    assert run.status == 503
    assert run.detail == "provider_unavailable"
    assert run.client_ms >= 0


def test_a_connection_failure_is_reported_rather_than_raised() -> None:
    run = timed_run.run_once(1, replying(Reply(0, b"connection refused")), application(), images())
    assert run.status == 0
    assert not run.ok


def test_run_one_is_first_hit_and_the_rest_are_warm() -> None:
    """Not 'cold' — see the module docstring and J-07."""
    runs = timed_run.measure(3, replying(Reply(200, verdict_body())), application(), images())
    assert [r.label for r in runs] == ["first-hit", "warm", "warm"]


# --- the report says what it measured -------------------------------------------------


def test_the_header_records_url_time_and_payload() -> None:
    text = timed_run.render(report_from(Reply(200, verdict_body()), runs=3))
    assert "http://localhost:8000" in text
    assert "2026-08-11 09:00:00Z" in text
    assert "3 requested, 3 succeeded" in text
    assert "KB total" in text


def test_the_note_the_operator_supplied_is_carried_into_the_table() -> None:
    text = timed_run.render(
        report_from(Reply(200, verdict_body()), runs=2, note="fly iad, warm 4h")
    )
    assert "fly iad, warm 4h" in text


def test_the_table_explains_that_first_hit_is_not_a_cold_start() -> None:
    text = timed_run.render(report_from(Reply(200, verdict_body()), runs=2))
    assert "only a genuine cold start" in text


def test_every_run_appears_in_the_table_not_only_the_summary() -> None:
    """A p95 with its sample hidden is a number you are asked to trust."""
    replies = [Reply(200, verdict_body(2000 + i * 10)) for i in range(20)]
    text = timed_run.render(report_from(*replies))
    assert "## Every run" in text
    for index in range(1, 21):
        assert f"| {index} |" in text


def test_failed_runs_are_listed_and_excluded_from_the_percentiles() -> None:
    """Computing a p95 only over the successes, without saying so, is the oldest way to
    make a slow service look fast."""
    ok = Reply(200, verdict_body(2000))
    bad = Reply(503, b'{"error": {"code": "provider_unavailable"}}')
    report = report_from(ok, bad, ok, ok)
    text = timed_run.render(report)
    assert len(report.successes) == 3
    assert "1 of 4 runs did not return a verdict" in text
    assert "503" in text


# --- sample mode (J-08) ---------------------------------------------------------------


def test_sample_mode_is_stamped_across_the_top() -> None:
    """Recorded fixtures answer in tens of milliseconds and would produce a beautiful,
    meaningless p95."""
    text = timed_run.render(
        report_from(Reply(200, verdict_body(45)), runs=20, simulated=True,
                    ready_status="sample_mode")
    )
    assert "SAMPLE MODE — NOT A LATENCY MEASUREMENT" in text
    assert "makes no model call" in text


def test_the_gate_verdict_is_withheld_in_sample_mode() -> None:
    text = timed_run.render(
        report_from(Reply(200, verdict_body(45)), runs=20, simulated=True)
    )
    assert "withheld" in text
    assert "within budget" not in text


def test_a_live_server_gets_a_gate_verdict() -> None:
    text = timed_run.render(report_from(Reply(200, verdict_body(2400)), runs=20))
    assert "within budget" in text


def test_a_slow_live_server_is_called_over_budget() -> None:
    """The gate reads whichever clock said more time passed. A server reporting 9.6s
    is over budget even if the caller's own stopwatch somehow said otherwise."""
    text = timed_run.render(report_from(Reply(200, verdict_body(9600)), runs=20))
    assert "OVER BUDGET" in text


def test_a_gate_verdict_from_too_few_runs_says_so() -> None:
    text = timed_run.render(report_from(Reply(200, verdict_body(2400)), runs=5))
    assert f"under the {timed_run.MIN_SAMPLES_FOR_P95}" in text


def test_twenty_runs_carry_no_small_sample_caveat() -> None:
    text = timed_run.render(report_from(Reply(200, verdict_body(2400)), runs=20))
    assert "under the" not in text


def test_no_successful_runs_produces_no_gate_claim() -> None:
    text = timed_run.render(report_from(Reply(0, b"refused"), runs=3))
    assert "no successful runs" in text
    assert "within budget" not in text


# --- LP-126 across a real boundary ----------------------------------------------------


def test_the_server_claiming_more_time_than_the_stopwatch_is_called_impossible() -> None:
    """PRD §232. The server cannot have spent an hour inside a request that took 40ms."""
    report = report_from(Reply(200, verdict_body(3_600_000)), runs=3)
    text = timed_run.render(report)
    assert report.impossible_runs
    assert "The clocks disagree" in text
    assert "Do not quote either number" in text


def test_a_healthy_run_states_the_gap_between_the_two_clocks() -> None:
    report = report_from(Reply(200, verdict_body(1)), runs=5, delay_s=0.005)
    text = timed_run.render(report)
    assert not report.impossible_runs
    assert "Clock check" in text
    assert "never report less time than passed" in text


def test_the_overhead_column_is_the_stopwatch_minus_the_server() -> None:
    run = Run(index=1, status=200, client_ms=2700, server_total_ms=2400)
    assert run.overhead_ms == 300
    assert not run.impossible


def test_a_negative_overhead_is_flagged_on_the_row() -> None:
    report = report_from(Reply(200, verdict_body(3_600_000)), runs=1)
    assert "⚠" in timed_run.render(report)


def test_a_divergence_makes_the_command_exit_non_zero() -> None:
    """The ticket asks for a test that makes a divergence fail. This is the one that
    fails a CI step rather than a pytest run."""
    report = report_from(Reply(200, verdict_body(3_600_000)), runs=2)
    assert report.impossible_runs


# --- output shape ---------------------------------------------------------------------


def test_the_report_is_committable_markdown() -> None:
    text = timed_run.render(report_from(Reply(200, verdict_body()), runs=20))
    assert text.startswith("# Timed run")
    assert text.endswith("\n")
    assert "|--:|" in text


def test_cost_is_totalled_when_the_server_priced_the_runs() -> None:
    text = timed_run.render(report_from(Reply(200, verdict_body()), runs=4))
    assert "$0.3080 total" in text


def test_the_summary_distinguishes_the_two_clocks_in_words() -> None:
    """Two rows of numbers with no explanation is how a reader picks the wrong one."""
    text = timed_run.render(report_from(Reply(200, verdict_body()), runs=3))
    assert "client stopwatch" in text
    assert "server-reported total" in text
    assert "minus render" in text


# --- against the real app over a real ASGI stack ---------------------------------------


def test_it_times_the_real_service_end_to_end(tmp_path: Path) -> None:
    """The check that matters: this script and the running service agree on the wire
    format, the field names, and where the timings live."""
    from fastapi.testclient import TestClient

    from api.config import Config
    from api.main import create_app

    client = TestClient(create_app(config=Config(use_fake_provider=True)))

    def post(path: str, content_type: str, body: bytes) -> Reply:
        response = client.post(path, content=body, headers={"Content-Type": content_type})
        return Reply(response.status_code, response.content)

    pair = [
        (n, (LABELS / n).read_bytes())
        for n in ("tc16_front_back_front.png", "tc16_front_back_back.png")
    ]
    runs = timed_run.measure(3, post, application(), pair)

    assert all(run.ok for run in runs), [r.detail for r in runs]
    assert all(run.server_total_ms is not None for run in runs)
    assert all(run.request_id.startswith("req_") for run in runs)
    assert all("preprocess" in run.stages for run in runs)

    report = Report(url="asgi://test", runs=runs, started_at="now", simulated=True)
    assert not report.impossible_runs, (
        "the server reported more elapsed time than the caller measured (PRD §232)"
    )
    assert "# Timed run" in timed_run.render(report)


def test_the_command_line_refuses_a_run_count_below_one() -> None:
    with pytest.raises(SystemExit):
        timed_run.main(["http://localhost:1", "--runs", "0"])


def test_it_fails_clearly_when_the_url_is_not_a_labelproof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timed_run, "http_get", lambda url, timeout=60.0: Reply(404, b""))
    with pytest.raises(SystemExit, match="Point this at"):
        timed_run.load_payload("http://example.com", "", [])
