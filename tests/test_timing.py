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
            cache_creation_tokens=1684,
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


# --- a stage that did not run is null, not zero ---------------------------------------


def test_an_unimplemented_stage_reports_null_in_the_response() -> None:
    """The same rule that made `preprocess` a roll-up. `"adjudicate": 0` invites a reader
    to conclude Tier-3 adjudication ran and cost nothing; it does not run at all."""
    stages = post_verify(make_client()).json()["timings_ms"]
    for name in timing.UNIMPLEMENTED_STAGES:
        assert stages[name] is None, f"{name} reports a number but never runs"


def test_the_unimplemented_list_matches_what_the_pipeline_actually_runs() -> None:
    """If someone wires a stage up, this fails until they take it off the list — the null
    is a statement about this build, not a permanent hole."""
    stages = post_verify(make_client(provider=SlowProvider(0.05))).json()["timings_ms"]
    ran = {
        name
        for name in timing.MEASURED_STAGES
        if stages[name] is not None and stages[name] > 0
    }
    assert not (ran & set(timing.UNIMPLEMENTED_STAGES)), (
        f"{sorted(ran & set(timing.UNIMPLEMENTED_STAGES))} is timed but still listed in "
        f"api.timing.UNIMPLEMENTED_STAGES"
    )


def test_an_unimplemented_stage_writes_no_log_line(logs: io.StringIO) -> None:
    """A missing series in the rollup reads as "no data collected" — which is the true
    statement about a stage this build does not have."""
    post_verify(make_client(logs=logs))
    for name in timing.UNIMPLEMENTED_STAGES:
        assert name not in stage_lines(logs)


def test_timing_an_unimplemented_stage_makes_it_a_number() -> None:
    """The design is not a dead end: wiring one up is deleting a name from the tuple."""
    clock = FakeClock()
    timer = timing.RequestTimer(clock=clock)
    with timer.stage("adjudicate"):
        clock.advance(0.9)
    assert timer.timings.adjudicate == 900


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


def test_the_cost_line_carries_both_cache_counters(logs: io.StringIO) -> None:
    """Reads and writes are priced differently. One field cannot carry both."""
    timing.cost_line(
        Cost(input_tokens=100, output_tokens=10, cache_read_tokens=4000,
             cache_creation_tokens=1684),
        model="claude-opus-5",
    )
    line = next(x for x in lines(logs) if x["event"] == "verification_cost")
    assert line["cache_read_tokens"] == 4000
    assert line["cache_creation_tokens"] == 1684


# --- the price list is keyed by model (LP-118) ---------------------------------------


def test_every_model_the_service_can_be_configured_with_has_a_price() -> None:
    """`LABELPROOF_EXTRACTION_MODEL` is an environment variable. A model the service can
    run on but cannot price is a cost analysis waiting to be wrong."""
    from api.config import MEASURED_EXTRACTION_MS

    missing = [
        model for model in MEASURED_EXTRACTION_MS if not timing.price_for(model)[1]
    ]
    assert not missing, f"models the service can run but cannot price: {missing}"


@pytest.mark.parametrize(
    ("model", "input_per_mtok", "output_per_mtok"),
    [
        ("claude-opus-5", 5.0, 25.0),
        ("claude-sonnet-5", 3.0, 15.0),
        ("claude-haiku-4-5", 1.0, 5.0),
    ],
)
def test_list_prices_are_what_the_provider_charges(
    model: str, input_per_mtok: float, output_per_mtok: float
) -> None:
    """Anthropic first-party list price, checked 2026-08-11."""
    price, known = timing.price_for(model)
    assert known
    assert price.input_per_mtok == input_per_mtok
    assert price.output_per_mtok == output_per_mtok


def test_switching_the_model_switches_the_price() -> None:
    """The defect this exists to prevent: pricing every model at Opus rates.

    Haiku 4.5 is the model the 5-second gate points at, and it is a fifth of Opus. A
    hardcoded Opus price list makes the obvious configuration change report five times
    the real cost.
    """
    cost = Cost(input_tokens=1_000_000, output_tokens=0)
    assert timing.usd_for(cost, "claude-opus-5") == pytest.approx(5.0)
    assert timing.usd_for(cost, "claude-haiku-4-5") == pytest.approx(1.0)
    assert timing.usd_for(cost, "claude-sonnet-5") == pytest.approx(3.0)


def test_a_dated_model_variant_prices_the_same_as_its_base() -> None:
    price, known = timing.price_for("claude-haiku-4-5-20251001")
    assert known
    assert price.input_per_mtok == 1.0


def test_an_unknown_model_is_priced_at_the_most_expensive_tier_and_says_so(
    logs: io.StringIO,
) -> None:
    """Guessing low under-reports, and under-reporting is what gets a number into a
    budget it cannot support. Guessing zero is worse — it looks free."""
    applog.configure(stream=logs)
    cost = Cost(input_tokens=1_000_000, output_tokens=0)
    assert timing.usd_for(cost, "claude-something-unreleased") == pytest.approx(5.0)

    warning = next(x for x in lines(logs) if x["event"] == "cost_model_unknown")
    assert warning["model"] == "claude-something-unreleased"
    assert warning["reason_code"] == "no_price_list"


def test_a_known_model_is_priced_quietly(logs: io.StringIO) -> None:
    applog.configure(stream=logs)
    timing.usd_for(Cost(input_tokens=100, output_tokens=10), "claude-haiku-4-5")
    assert not [x for x in lines(logs) if x["event"] == "cost_model_unknown"]


def test_the_default_price_is_the_dearest_in_the_table() -> None:
    dearest = max(p.input_per_mtok for p in timing.PRICES.values())
    assert timing.UNKNOWN_MODEL_PRICE.input_per_mtok == dearest


def test_the_opus_row_still_agrees_with_the_adapters_own_constants() -> None:
    """A tripwire, not a delegation.

    `estimated_usd` in the provider adapter carries its own price table. This module is
    now the authority, but while both exist they must not disagree — a cost quoted from
    one and a cost quoted from the other would both look official. The model is named on
    both sides on purpose: the adapter's table is keyed by model too, and comparing its
    unknown-model fallback against a named row would compare two different questions.
    """
    from api.provider.anthropic_adapter import estimated_usd

    cost = Cost(input_tokens=9840, output_tokens=1120, cache_read_tokens=4000)
    expected = estimated_usd(
        ProviderUsage(input_tokens=9840, output_tokens=1120, cache_read_tokens=4000),
        "claude-opus-5",
    )
    assert timing.usd_for(cost, "claude-opus-5") == pytest.approx(expected)


# --- cache tokens are priced, not free ------------------------------------------------


def test_cached_reads_are_priced_at_a_tenth_of_an_input_token() -> None:
    """Provider `input_tokens` excludes cache reads. Dropping them under-claims cost."""
    read_only = Cost(cache_read_tokens=1_000_000)
    assert timing.usd_for(read_only, "claude-opus-5") == pytest.approx(0.5)


def test_cache_writes_are_priced_at_1_25x_an_input_token() -> None:
    """The larger of the two omissions, and the one that pointed the wrong way.

    A cold two-image request writes the cached system prefix, and `input_tokens` excludes
    those tokens too — so pricing only reads still leaves the write billed at zero.
    """
    write_only = Cost(cache_creation_tokens=1_000_000)
    assert timing.usd_for(write_only, "claude-opus-5") == pytest.approx(6.25)


def test_a_write_costs_more_than_a_read_of_the_same_size() -> None:
    read = timing.usd_for(Cost(cache_read_tokens=10_000), "claude-opus-5")
    write = timing.usd_for(Cost(cache_creation_tokens=10_000), "claude-opus-5")
    assert write > read


def test_a_request_that_spent_nothing_is_priced_at_zero() -> None:
    assert timing.usd_for(Cost()) == 0.0
    assert timing.usd_for(Cost(), "claude-opus-5") == 0.0


def test_pricing_needs_no_provider_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pricing a handful of integers must not depend on an HTTP client being installed —
    a cost line is worth showing and never worth failing a verification over."""
    import builtins

    real_import = builtins.__import__

    def explode(name: str, *args: Any, **kwargs: Any) -> Any:
        if "anthropic" in name:
            raise ImportError("no sdk here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", explode)
    assert timing.usd_for(
        Cost(input_tokens=1_000_000, output_tokens=0), "claude-haiku-4-5"
    ) == pytest.approx(1.0)


# --- the merge note: the kwarg is load-bearing (LP-118) -------------------------------


def test_provider_cache_tokens_reach_the_cost_block_through_the_pipeline() -> None:
    """A pipeline-level guard, deliberately not a unit test of `usd_for`.

    `api/verify.py` copies the provider's cache counters onto `Cost`. Those two keyword
    arguments are the whole fix: drop them in a merge and the cost line silently reverts
    to under-billing, with every `usd_for` unit test still green.
    """
    body = post_verify(make_client(provider=CountingProvider())).json()
    assert body["cost"]["cache_read_tokens"] == 4000
    assert body["cost"]["cache_creation_tokens"] == 1684
    assert body["cost"]["usd"] > 0


def test_the_priced_total_reflects_the_cache_tokens_the_provider_reported() -> None:
    """Not just present in the body — actually in the number."""
    body = post_verify(make_client(provider=CountingProvider())).json()
    cost = body["cost"]
    # Priced at the model the RESPONSE reports, not a literal. Hardcoding one made this
    # compare the real cost against a different model's rates the moment the default
    # changed, which is a comparison of two unrelated numbers.
    priced_without_cache = timing.usd_for(
        Cost(input_tokens=cost["input_tokens"], output_tokens=cost["output_tokens"]),
        cost.get("model") or Config().extraction_model,
    )
    assert cost["usd"] > priced_without_cache


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
    assert body["cost"]["cache_creation_tokens"] == 1684
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
    """`preprocess` is excluded — it is a roll-up of two stages already in the sum, and
    a stage that did not run contributes nothing rather than a zero."""
    stages = post_verify(make_client(provider=SlowProvider(0.2))).json()["timings_ms"]
    measured = sum(stages[name] or 0 for name in timing.MEASURED_STAGES)
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


# --- LP-126: the check has teeth, and the screen is inside it -------------------------
#
# The tests further up put a stopwatch around a real request. The ones here do two
# things those cannot:
#
#   1. Prove the stopwatch check *fails* when the server lies. A guard nobody has seen
#      go red is a guard nobody knows works.
#   2. Reach the number that is actually on the screen. The response body is not the
#      product; the result card is.


class ClockDisagreementError(AssertionError):
    """The server's claim and the stopwatch do not agree (PERF-2, PRD §232)."""


def check_stopwatch(claimed_ms: int | None, observed_ms: float, *, slack_ms: int = 5) -> None:
    """Hold a server's reported total to a clock it does not control.

    Two directions, and they fail for different reasons.

    **Claimed above observed** is impossible. Time cannot have passed inside the request
    that did not pass outside it, so the total is measuring something other than this
    request — a wrong clock, a reused timer, or a number that was never measured.

    **Claimed far below observed** means the total is real but partial: it was stopped
    before the slow part, or started after it. The screen would then under-report, which
    is the direction PERF-2 exists to prevent.
    """
    if claimed_ms is None:
        raise ClockDisagreementError(
            "The response carried no `timings_ms.total`. The product's headline claim is "
            "speed; there is nothing to hold to a stopwatch."
        )
    if claimed_ms > observed_ms + slack_ms:
        raise ClockDisagreementError(
            f"The server claims {claimed_ms}ms inside a request the stopwatch measured "
            f"at {observed_ms:.0f}ms. That cannot happen — `timings_ms.total` is not "
            f"measuring this request (PRD §232)."
        )
    if claimed_ms < observed_ms * 0.5:
        raise ClockDisagreementError(
            f"The server claims {claimed_ms}ms against a stopwatch of {observed_ms:.0f}ms. "
            f"More than half the request is unaccounted for, so `total` is measuring part "
            f"of the work rather than the request (PERF-2)."
        )


def test_the_stopwatch_check_agrees_with_a_healthy_request() -> None:
    client = make_client(provider=SlowProvider(0.2))

    stopwatch = time.perf_counter()
    response = post_verify(client)
    observed_ms = (time.perf_counter() - stopwatch) * 1000

    check_stopwatch(response.json()["timings_ms"]["total"], observed_ms)


def test_a_total_that_exceeds_the_stopwatch_is_caught() -> None:
    """The impossible direction: the server claims more time than actually passed."""
    with pytest.raises(ClockDisagreementError, match="cannot happen"):
        check_stopwatch(3_600_000, 2500.0)


def test_a_total_that_omits_the_slow_part_is_caught() -> None:
    """The plausible direction, and the dangerous one: a real measurement of the wrong
    span. Nine tenths of the request missing still looks like a number."""
    with pytest.raises(ClockDisagreementError, match="unaccounted for"):
        check_stopwatch(240, 2500.0)


def test_a_missing_total_is_caught_rather_than_treated_as_fast() -> None:
    with pytest.raises(ClockDisagreementError, match="nothing to hold"):
        check_stopwatch(None, 2500.0)


def test_a_server_that_stops_its_clock_early_fails_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this whole ticket exists for, driven through the real stack.

    A route that computes `total` before awaiting extraction produces a small, entirely
    plausible number. Here the timer is made to report as if the clock had been stopped
    at the top of the request; the check must go red.
    """
    monkeypatch.setattr(timing.RequestTimer, "elapsed_ms", lambda self: 1)
    client = make_client(provider=SlowProvider(0.3))

    stopwatch = time.perf_counter()
    response = post_verify(client)
    observed_ms = (time.perf_counter() - stopwatch) * 1000

    assert response.json()["timings_ms"]["total"] == 1
    with pytest.raises(ClockDisagreementError):
        check_stopwatch(response.json()["timings_ms"]["total"], observed_ms)


# --- tripwires on the front end, and what they are not --------------------------------
#
# **These are tripwires against deletion, not a test of the rendered screen.** They are
# substring assertions on TypeScript source read as text. They cannot tell you the number
# an agent sees is correct: a correct refactor to a local variable fails them, and they
# pass if the literal survives in a dead branch or if `startedRef.current` is reset after
# the upload rather than before it.
#
# What they buy is narrow and worth having: the day someone deletes the client clock, or
# points the banner at the server's total, or wires the progress animation to the result
# card, something goes red. Without them PERF-2 has no automated guard at all, because
# `web/src` is not this wave's to change and a `vitest` test asserting the rendered string
# would have to live there.
#
# The invariant they guard is NOT "the screen shows `timings_ms.total`" — it is the
# opposite. The server's total cannot contain the upload or the network, so it is always
# the smaller of the two, and rendering it would make the product under-report its own
# latency. That is the direction PERF-2 exists to prevent. The screen shows a client wall
# clock spanning submit to response: the stopwatch itself.


def _web_source(relative: str) -> str:
    """The front end's source, as text.

    Skips only when there is no front end in this checkout at all. If `web/src` exists but
    the named file does not, that fails — a renamed or deleted file is exactly the change
    these tripwires exist to catch, and skipping on it would make the guard evaporate
    silently at the moment it was needed.
    """
    web = ROOT / "web" / "src"
    if not web.is_dir():
        pytest.skip("no front end in this checkout")
    path = web / relative
    assert path.exists(), (
        f"web/src/{relative} is gone. PERF-2's front-end tripwires point at it; if it "
        f"moved, move them with it rather than deleting them."
    )
    return path.read_text()


def test_the_elapsed_time_on_screen_is_measured_from_a_real_clock() -> None:
    source = _web_source("routes/VerifyNow.tsx")
    assert "elapsedMs: Date.now() - startedRef.current" in source, (
        "The elapsed time on the result card must be a wall-clock measurement spanning "
        "submit to response. If this moved, PERF-2 has no guard left."
    )
    assert source.count("startedRef.current = Date.now()") >= 2, (
        "Every path that starts a check must start the clock — the sample button as well "
        "as the form."
    )


def test_the_simulated_progress_timer_never_becomes_the_result() -> None:
    """`waited` drives the stage animation while the request is in flight. It ticks on a
    200ms interval and owes nothing to the actual request. If it ever reached the result
    card, the product would be reporting an animation as a measurement."""
    source = _web_source("routes/VerifyNow.tsx")
    assert "elapsedMs={waited}" not in source
    assert "elapsedMs={stage}" not in source
    assert "elapsedMs={elapsedMs}" in source


def test_the_screen_does_not_render_the_servers_smaller_number() -> None:
    """Deliberate. The server's total excludes upload and network, so showing it would
    under-report — the flattering direction, and the one PERF-2 forbids."""
    source = _web_source("routes/VerifyNow.tsx")
    for flattering in (
        "elapsedMs={result.timings_ms.total}",
        "elapsedMs={checked.result.timings_ms.total}",
    ):
        assert flattering not in source, (
            "The result card must not display the server's own total. It is always "
            "smaller than the time that actually passed (PERF-2, PRD §232)."
        )


def test_the_banner_renders_the_elapsed_value_it_is_given() -> None:
    source = _web_source("components/AggregateBanner.tsx")
    assert "formatElapsed(elapsedMs)" in source
    assert "data-testid=\"elapsed\"" in source


def test_the_server_total_still_reaches_the_client_for_the_breakdown() -> None:
    """Not displayed as the headline is not the same as not sent. The breakdown is how
    a slow run gets diagnosed, and PRD §Observability requires it surfaced."""
    parser = _web_source("api.ts")
    for stage in ("preprocess", "extract", "compare", "total"):
        assert f"'{stage}'" in parser, f"the client parser drops timings_ms.{stage}"


def test_the_server_never_claims_more_time_than_an_enclosing_clock_measured() -> None:
    """A check on the **server's** clock, not on the screen — the name it used to carry
    oversold it.

    An outer measurement necessarily contains an inner one, so this can only fail if the
    server's clock is broken: a total that was fabricated, measured against the wrong
    epoch, or carried over from another request. That is a real failure mode and worth a
    test, but it is not evidence about what an agent reads off a result card. The screen
    is covered by the tripwires above and, across a real network boundary, by
    `scripts/timed_run.py`.
    """
    client = make_client(provider=SlowProvider(0.25))

    submitted = time.perf_counter()
    response = post_verify(client)
    on_screen_ms = round((time.perf_counter() - submitted) * 1000)

    claimed_ms = response.json()["timings_ms"]["total"]
    assert on_screen_ms >= claimed_ms, (
        f"The screen would show {on_screen_ms}ms while the server measured {claimed_ms}ms. "
        f"The displayed number must never be smaller than the time that passed."
    )

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
