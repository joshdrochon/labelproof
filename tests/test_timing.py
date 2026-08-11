"""Stage latency, cost accounting and the honesty check (LP-063, LP-118, LP-126).

Two things are under test here and they are not the same thing.

**The stages exist and reach the agent.** PRD §Observability requires per-stage latency
surfaced in the UI, which means it has to be in the response body — a number that only
exists in stdout cannot be shown on a result card.

**The number is true.** PRD §232: *"If the number on the screen and the number on the
stopwatch disagree, the stopwatch wins."* So the tests at the bottom of this file put an
independent stopwatch around a real HTTP request and hold the server's own claim to it. A
`total` computed before the slow part, or assembled by adding up the stages, or simply
made up, fails here.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api import logging as applog
from api import timing
from api.config import Config
from api.main import create_app
from api.models import Commodity, Cost, Timings
from api.provider.base import (
    ExtractionRequest,
    ExtractionResponse,
    ProviderUsage,
)
from api.provider.fake import SpecBackedProvider
from fixtures.generator.catalog import by_name

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "fixtures" / "labels"
SAMPLE = ROOT / "assets" / "samples" / "old_tom.json"

FRONT = "tc16_front_back_front.png"
BACK = "tc16_front_back_back.png"


# --- helpers -------------------------------------------------------------------------


class FakeClock:
    """A clock the test drives by hand, so a duration assertion is exact."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlowProvider:
    """A provider that takes a known, real amount of wall time.

    This is the whole mechanism of the honesty check: if the reported total does not
    contain this sleep, the total was measured somewhere it should not have been.
    """

    name = "fake:slow"

    def __init__(self, delay_s: float, spec: str = "tc16_front_back") -> None:
        self.delay_s = delay_s
        self._inner = SpecBackedProvider(by_name(spec))

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        time.sleep(self.delay_s)
        return self._inner.extract(request)


class CountingProvider:
    """Reports token usage including cached reads, so the cost line can be checked."""

    name = "fake:counting"

    def __init__(self, spec: str = "tc16_front_back") -> None:
        self._inner = SpecBackedProvider(by_name(spec))

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        response = self._inner.extract(request)
        response.usage = ProviderUsage(
            input_tokens=9840,
            output_tokens=1120,
            cache_read_tokens=4000,
            model="claude-opus-5",
        )
        return response


def make_client(
    provider: Any = None, *, logs: io.StringIO | None = None, **overrides: Any
) -> TestClient:
    config = Config(use_fake_provider=True, **overrides)
    client = TestClient(create_app(config=config, provider=provider))
    if logs is not None:
        # `create_app` points the logger at stdout. Take it back, after the app is built
        # rather than before, or the capture is silently empty.
        applog.configure(stream=logs)
    return client


def application_json(**overrides: Any) -> str:
    raw = json.loads(SAMPLE.read_text())
    body = {k: v for k, v in raw.items() if not k.startswith("_")}
    body.update(overrides)
    return json.dumps(body)


def label_files(*names: str) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("images", (n, (LABELS / n).read_bytes(), "image/png")) for n in names]


def post_verify(client: TestClient, *names: str) -> Any:
    return client.post(
        "/verify",
        files=label_files(*(names or (FRONT, BACK))),
        data={"application": application_json()},
    )


@pytest.fixture
def logs() -> io.StringIO:
    stream = io.StringIO()
    applog.configure(stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in stream.getvalue().splitlines():
        if raw.strip():
            out.append(json.loads(raw))
    return out


def stage_lines(stream: io.StringIO) -> dict[str, int]:
    return {
        line["stage"]: line["duration_ms"]
        for line in lines(stream)
        if line["event"] == "stage_complete"
    }


# --- RequestTimer --------------------------------------------------------------------


def test_a_stage_records_the_time_the_block_took() -> None:
    clock = FakeClock()
    timer = timing.RequestTimer(clock=clock)
    with timer.stage("extract"):
        clock.advance(2.61)
    assert timer.timings.extract == 2610


def test_stages_accumulate_rather_than_overwrite() -> None:
    """A per-image stage runs more than once. Two images is two ingests, not the last one."""
    clock = FakeClock()
    timer = timing.RequestTimer(clock=clock)
    for _ in range(3):
        with timer.stage("ingest"):
            clock.advance(0.02)
    assert timer.timings.ingest == 60


def test_a_stage_that_raises_is_still_measured() -> None:
    """A slow failure is the most interesting latency there is. It must not vanish."""
    clock = FakeClock()
    timer = timing.RequestTimer(clock=clock)
    with pytest.raises(ValueError), timer.stage("extract"):
        clock.advance(4.0)
        raise ValueError("provider blew up")
    assert timer.timings.extract == 4000


def test_an_unknown_stage_name_is_refused() -> None:
    """Typos become permanently empty series in the rollup. Fail at the call site."""
    timer = timing.RequestTimer()
    with pytest.raises(ValueError, match="not a measured stage"), timer.stage("preproces"):
        pass


def test_preprocess_cannot_be_timed_directly_because_it_is_derived() -> None:
    timer = timing.RequestTimer()
    with pytest.raises(ValueError, match="derived"), timer.stage("preprocess"):
        pass


def test_total_is_the_outer_clock_not_the_sum_of_stages() -> None:
    """Uninstrumented work belongs in `total`. That gap is the point of measuring."""
    clock = FakeClock()
    timer = timing.RequestTimer(clock=clock)
    with timer.stage("extract"):
        clock.advance(1.0)
    clock.advance(0.5)  # work nobody instrumented
    timings = timer.seal()
    assert timings.extract == 1000
    assert timings.total == 1500


def test_remaining_budget_counts_down_and_goes_negative() -> None:
    clock = FakeClock()
    timer = timing.RequestTimer(clock=clock)
    clock.advance(1.2)
    assert timer.remaining_ms(5000) == pytest.approx(3800)
    clock.advance(4.0)
    assert timer.remaining_ms(5000) < 0


# --- preprocess is a roll-up ---------------------------------------------------------


def test_preprocess_is_ingest_plus_quality() -> None:
    """PRD §Observability names `preprocess`; this pipeline measures its two halves."""
    timings = Timings(ingest=41, quality=18)
    timing.seal(timings)
    assert timings.preprocess == 59


def test_preprocess_is_not_a_separately_measured_stage() -> None:
    """The roll-up has exactly one definition. Two would eventually disagree."""
    assert "preprocess" not in timing.MEASURED_STAGES
    assert timing.PREPROCESS_PARTS == ("ingest", "quality")


def test_merge_into_never_overwrites_the_pipelines_own_stages() -> None:
    """The route measures ingest/quality; the pipeline measures extract/compare."""
    clock = FakeClock()
    timer = timing.RequestTimer(clock=clock)
    with timer.stage("ingest"):
        clock.advance(0.04)

    from_pipeline = Timings(extract=2610, compare=2)
    timer.merge_into(from_pipeline)

    assert from_pipeline.extract == 2610
    assert from_pipeline.compare == 2
    assert from_pipeline.ingest == 40


# --- the stage lines the rollup reads ------------------------------------------------


def test_emit_writes_one_line_per_stage(logs: io.StringIO) -> None:
    timing.emit(Timings(ingest=40, quality=18, preprocess=58, extract=2610, compare=2))
    written = stage_lines(logs)
    assert written["preprocess"] == 58
    assert written["extract"] == 2610
    assert written["compare"] == 2


def test_the_prd_named_stages_are_logged_even_at_zero(logs: io.StringIO) -> None:
    """A zero means 'measured, took no time'. A missing series means 'no data'."""
    timing.emit(Timings())
    written = stage_lines(logs)
    assert set(written) == {"preprocess", "extract", "compare"}
    assert all(value == 0 for value in written.values())


def test_stage_lines_carry_the_outcome(logs: io.StringIO) -> None:
    timing.emit(Timings(extract=900), ok=False)
    assert all(
        line["ok"] is False for line in lines(logs) if line["event"] == "stage_complete"
    )


def test_stage_lines_carry_no_label_content() -> None:
    """The allowlist is the mechanism; this asserts emit() stays inside it."""
    from api.logging import ALLOWED_FIELDS

    assert set(timing.STAGE_NAMES) <= {
        "preprocess", "ingest", "quality", "extract", "compare", "adjudicate"
    }
    assert {"stage", "duration_ms", "ok"} <= ALLOWED_FIELDS


# --- cost (LP-118) -------------------------------------------------------------------


def test_cost_line_carries_tokens_in_out_and_dollars(logs: io.StringIO) -> None:
    timing.cost_line(
        Cost(input_tokens=9840, output_tokens=1120, usd=0.0772), model="claude-opus-5"
    )
    line = next(x for x in lines(logs) if x["event"] == "verification_cost")
    assert line["input_tokens"] == 9840
    assert line["output_tokens"] == 1120
    assert line["usd"] == pytest.approx(0.0772)
    assert line["model"] == "claude-opus-5"


def test_the_price_list_is_borrowed_from_the_adapter_not_copied() -> None:
    """One price list. `estimated_usd` is the only place the numbers live."""
    from api.provider.anthropic_adapter import estimated_usd

    cost = Cost(input_tokens=9840, output_tokens=1120, cache_read_tokens=4000)
    expected = estimated_usd(
        ProviderUsage(input_tokens=9840, output_tokens=1120, cache_read_tokens=4000)
    )
    assert timing.usd_for(cost) == expected


def test_cached_reads_are_priced_rather_than_free() -> None:
    """Provider `input_tokens` excludes cache reads. Dropping them under-claims cost."""
    without = timing.usd_for(Cost(input_tokens=1000, output_tokens=100))
    with_cache = timing.usd_for(
        Cost(input_tokens=1000, output_tokens=100, cache_read_tokens=100_000)
    )
    assert with_cache > without


def test_a_request_that_spent_nothing_is_priced_at_zero() -> None:
    assert timing.usd_for(Cost()) == 0.0


def test_pricing_never_fails_a_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cost line is worth showing and never worth a 500."""
    import builtins

    real_import = builtins.__import__

    def explode(name: str, *args: Any, **kwargs: Any) -> Any:
        if "anthropic_adapter" in name:
            raise ImportError("no sdk here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", explode)
    assert timing.usd_for(Cost(input_tokens=100, output_tokens=10)) == 0.0


# --- the stages reach the agent (LP-063, PRD §Observability) --------------------------


def test_the_response_body_carries_the_prd_named_stages() -> None:
    """Surfaced in the UI means present in the body, not only in stdout."""
    response = post_verify(make_client())
    assert response.status_code == 200
    stages = response.json()["timings_ms"]
    for name in ("preprocess", "extract", "compare", "total"):
        assert name in stages


def test_preprocess_in_the_response_is_the_roll_up() -> None:
    stages = post_verify(make_client()).json()["timings_ms"]
    assert stages["preprocess"] == stages["ingest"] + stages["quality"]


def test_preprocess_is_a_real_measurement_not_a_placeholder() -> None:
    """Decoding, stripping and downscaling two label PNGs is never free."""
    stages = post_verify(make_client()).json()["timings_ms"]
    assert stages["preprocess"] > 0


def test_the_cost_block_reaches_the_agent() -> None:
    body = post_verify(make_client(provider=CountingProvider())).json()
    assert body["cost"]["input_tokens"] == 9840
    assert body["cost"]["output_tokens"] == 1120
    assert body["cost"]["cache_read_tokens"] == 4000
    assert body["cost"]["usd"] > 0


def test_a_verification_writes_its_stage_and_cost_lines(logs: io.StringIO) -> None:
    post_verify(make_client(provider=CountingProvider(), logs=logs))
    events = {line["event"] for line in lines(logs)}
    assert "stage_complete" in events
    assert "verification_cost" in events
    assert {"preprocess", "extract", "compare"} <= set(stage_lines(logs))


def test_the_pregate_path_still_reports_its_stages() -> None:
    """Zero model calls is a latency result too, and the one to be proud of (LP-321)."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1200, 1600), (16, 16, 16)).save(buffer, format="PNG")
    blank = buffer.getvalue()

    client = make_client()
    response = client.post(
        "/verify",
        files=[("images", ("hopeless.png", blank, "image/png"))],
        data={"application": application_json()},
    )
    assert response.status_code == 200
    stages = response.json()["timings_ms"]
    assert stages["preprocess"] == stages["ingest"] + stages["quality"]
    assert stages["total"] >= stages["preprocess"]


# --- LP-126: the stopwatch wins ------------------------------------------------------


def test_the_reported_total_matches_an_independent_stopwatch() -> None:
    """PRD §232. The server's claim is held to a clock it does not control.

    In-process there is no network, so the two numbers should agree closely. The
    tolerance covers multipart encoding and JSON serialisation on the test's side of the
    boundary — not a fabricated total, which this would catch at any tolerance.
    """
    client = make_client()

    stopwatch = time.perf_counter()
    response = client.post(
        "/verify",
        files=label_files(FRONT, BACK),
        data={"application": application_json()},
    )
    observed_ms = (time.perf_counter() - stopwatch) * 1000

    claimed_ms = response.json()["timings_ms"]["total"]

    assert claimed_ms <= observed_ms + 5, (
        f"The server claims {claimed_ms}ms but the stopwatch says {observed_ms:.0f}ms. "
        f"The screen must never report less time than actually passed (PERF-2)."
    )
    assert claimed_ms >= observed_ms * 0.5, (
        f"The server claims {claimed_ms}ms against a stopwatch of {observed_ms:.0f}ms. "
        f"More than half the request is unaccounted for, so `total` is not measuring "
        f"the whole request (PRD §232)."
    )


def test_the_total_contains_the_slow_part() -> None:
    """The failure this catches: `total` computed before the extraction it is timing."""
    delay_s = 0.4
    client = make_client(provider=SlowProvider(delay_s))

    stopwatch = time.perf_counter()
    response = client.post(
        "/verify",
        files=label_files(FRONT, BACK),
        data={"application": application_json()},
    )
    observed_ms = (time.perf_counter() - stopwatch) * 1000

    stages = response.json()["timings_ms"]
    assert stages["extract"] >= delay_s * 1000 * 0.9
    assert stages["total"] >= stages["extract"]
    assert stages["total"] <= observed_ms + 5


def test_the_total_is_never_less_than_any_stage_it_contains() -> None:
    stages = post_verify(make_client(provider=SlowProvider(0.2))).json()["timings_ms"]
    for name in ("preprocess", "extract", "compare", "ingest", "quality"):
        assert stages["total"] >= stages[name], f"{name} exceeds the total that contains it"


def test_the_measured_stages_never_exceed_the_total() -> None:
    """`preprocess` is excluded — it is a roll-up of two stages already in the sum."""
    stages = post_verify(make_client(provider=SlowProvider(0.2))).json()["timings_ms"]
    measured = sum(stages[name] for name in timing.MEASURED_STAGES)
    assert measured <= stages["total"] + 5


def test_the_logged_duration_is_the_same_number_the_agent_sees(logs: io.StringIO) -> None:
    """One measurement, two audiences. A log that disagrees with the screen is worse
    than no log — it makes the discrepancy look like a second, real reading."""
    body = post_verify(make_client(provider=SlowProvider(0.15), logs=logs)).json()
    logged = next(x for x in lines(logs) if x["event"] == "verify_complete")
    assert logged["duration_ms"] == body["timings_ms"]["total"]


def test_the_request_id_on_screen_is_the_request_id_in_the_log(logs: io.StringIO) -> None:
    """Correlation is the whole point of having an id (OPS-5)."""
    body = post_verify(make_client(logs=logs)).json()
    logged = next(x for x in lines(logs) if x["event"] == "verify_complete")
    assert logged["request_id"] == body["request_id"]


def test_a_verification_response_carries_the_commodity_it_was_asked_about() -> None:
    """Guard on the fixture helper itself — a silently wrong application would make
    every timing above measure the wrong pipeline."""
    body = post_verify(make_client()).json()
    assert body["fields"], "no fields compared, so nothing above measured a verification"
    assert json.loads(application_json())["commodity"] == Commodity.SPIRITS.value


# --- the documented stage table matches the shipped one (LP-125) ----------------------


def _readme_stage_table() -> dict[str, str]:
    """The stage table under `### Timings`, as {field: description}."""
    import pathlib
    import re

    readme = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()
    section = readme[readme.index("### Timings") :]
    # Stop at the next top-level heading. Not at "---": the table separator row is
    # made of dashes and would truncate the section before its first data row.
    end = section.find("\n## ")
    section = section[:end] if end != -1 else section

    rows: dict[str, str] = {}
    for match in re.finditer(r"^\| `([a-z_]+)` \| (.+?) \|$", section, re.M):
        rows[match.group(1)] = match.group(2)
    return rows


def test_the_readme_documents_every_stage_the_api_returns() -> None:
    """An agent reading the docs must not meet a field in the response that is not there,
    and must not go looking for one that is."""
    documented = set(_readme_stage_table())
    shipped = set(Timings.model_fields)
    assert documented == shipped, (
        f"README.md's stage table disagrees with `api.models.Timings`. "
        f"Undocumented: {sorted(shipped - documented)}. "
        f"Documented but not shipped: {sorted(documented - shipped)}."
    )


def test_the_readme_warns_that_the_stage_column_does_not_add_up() -> None:
    """`preprocess` is a roll-up. Someone will try to sum the column; say so first."""
    import pathlib

    readme = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "do not add the column up" in readme
    assert "double-count" in readme


def test_the_readme_says_total_is_measured_rather_than_summed() -> None:
    import pathlib

    readme = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "measured, not derived" in readme


def test_the_documented_example_is_arithmetically_consistent() -> None:
    """The JSON sample in the README is the first thing anyone copies. If its own
    numbers do not hold together, nothing after it will be believed."""
    import json
    import pathlib
    import re

    readme = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()
    block = re.search(r'"timings_ms": (\{.*?\})', readme, re.S)
    assert block, "README.md no longer shows an example timings block"
    example = json.loads(re.sub(r"\s+", " ", block.group(1)))

    assert example["preprocess"] == example["ingest"] + example["quality"]
    assert example["total"] >= sum(
        example[name] for name in timing.MEASURED_STAGES
    )
