"""Per-request stage latency and cost accounting (OPS-1, OPS-4).

The vendor pilot that came before this one died of *unexplained* slowness — 30 to 40
seconds with nothing to point at. PERF-1 puts a 5-second gate on this product, and a gate
you cannot measure per stage is a gate you cannot defend. So the timings here are not
decoration: they are the evidence for the one requirement that decides whether anyone
adopts the thing.

Two rules govern this module.

**The numbers reach the response body, not just the log.** PRD §Observability requires
stage latency *surfaced in the UI* — every result card states its elapsed time. A timing
that only exists in stdout cannot be shown to the agent who is deciding whether to trust
the tool.

**The clock never flatters.** `total` is measured by the outermost timer in the request,
started before the first byte is parsed and stopped after the last verdict is computed.
It is never assembled by adding the stages up — that would silently omit whatever is not
instrumented, and the gap between "the stages we measured" and "how long it took" is
precisely the number worth knowing. `tests/test_timing.py` holds this to an independent
stopwatch (LP-126, PRD §232: if the screen and the stopwatch disagree, the stopwatch
wins).

### On `preprocess`

PRD §Observability names five stages: upload → preprocess → extract → compare → render.
This pipeline measures `ingest` (sniff, EXIF-orient, strip metadata, re-encode, downscale)
and `quality` (blur/exposure/glare/skew scoring) separately, and both of them *are* the
preprocessing. `preprocess` is therefore a **roll-up of those two**, not a fourth sibling.

Summing every field on `Timings` double-counts. That is stated here, stated in the README,
and asserted by test so it cannot quietly become false.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from api import logging as applog
from api.models import Cost, Timings

#: Stages measured directly by a clock. `preprocess` is absent on purpose — see the
#: module docstring; it is derived, and deriving it twice is how the two copies diverge.
MEASURED_STAGES: tuple[str, ...] = ("ingest", "quality", "extract", "compare", "adjudicate")

#: What `preprocess` rolls up.
PREPROCESS_PARTS: tuple[str, ...] = ("ingest", "quality")

#: Logged on every request even when the measurement is zero (LP-119, LP-124).
#:
#: A missing series in the rollup reads as "we collected no data"; a zero reads as "we
#: measured it and it took no time". Those are different facts, and the pre-gate path —
#: which returns a verdict with *no* extraction, by design (LP-321) — depends on the
#: difference being visible. The three named here are the three PRD §Observability names.
ALWAYS_LOGGED: tuple[str, ...] = ("preprocess", "extract", "compare")

#: Every stage name that can appear in a `stage_complete` line. The rollup uses this to
#: order its table, so a new stage shows up in the report without editing the script.
STAGE_NAMES: tuple[str, ...] = ("preprocess", *MEASURED_STAGES)

Clock = Callable[[], float]


def _ms(seconds: float) -> int:
    """Milliseconds, rounded rather than truncated.

    Truncating every stage independently loses up to a millisecond each time, which is
    how a set of stages ends up summing to more than a total that was truncated once.
    """
    return round(seconds * 1000)


def preprocess_ms(timings: Timings) -> int:
    """The preprocessing roll-up. See the module docstring."""
    return sum(int(getattr(timings, part)) for part in PREPROCESS_PARTS)


def seal(timings: Timings) -> Timings:
    """Fill the derived stages. Call once, after the last measurement lands.

    Mutates and returns the same object so a caller can seal a result's timings in place
    without deciding whether to copy.
    """
    timings.preprocess = preprocess_ms(timings)
    return timings


class RequestTimer:
    """One request's clock, and the only thing that decides what `total` means.

    Constructed at the top of the request handler, before anything is parsed. `total` is
    read off this timer, so it includes every millisecond the request was alive inside the
    application — including work nobody thought to instrument.

    Usage:

        timer = RequestTimer()
        with timer.stage("ingest"):
            ...
        timer.merge_into(result.timings_ms)

    `stage()` accumulates, so calling it twice for the same name across a loop adds up
    rather than overwriting. That is what makes a per-image stage honest.
    """

    def __init__(self, clock: Clock = time.perf_counter) -> None:
        self._clock = clock
        self._started = clock()
        self.timings = Timings()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time a block into `name`. Records the elapsed time even if the block raises."""
        if name not in MEASURED_STAGES:
            raise ValueError(
                f"{name!r} is not a measured stage. Known stages: "
                f"{', '.join(MEASURED_STAGES)}. `preprocess` is derived — see "
                f"api/timing.preprocess_ms."
            )
        started = self._clock()
        try:
            yield
        finally:
            elapsed = _ms(self._clock() - started)
            setattr(self.timings, name, int(getattr(self.timings, name)) + elapsed)

    def elapsed_ms(self) -> int:
        """Wall time since the timer was constructed."""
        return _ms(self._clock() - self._started)

    def remaining_ms(self, budget_ms: float) -> float:
        """What is left of a request budget. Negative once the budget is blown."""
        return budget_ms - (self._clock() - self._started) * 1000

    def seal(self) -> Timings:
        """Stop the clock and return this timer's own timings, derived stages filled."""
        self.timings.total = self.elapsed_ms()
        return seal(self.timings)

    def merge_into(self, timings: Timings) -> Timings:
        """Copy this timer's stages onto the pipeline's timings and stop the clock.

        Only non-zero stages are copied. The pipeline measures `extract` and `compare`
        from inside itself; the request handler measures `ingest` and `quality`. Copying
        zeros over the pipeline's real numbers would erase the two that matter most.
        """
        for name in MEASURED_STAGES:
            mine = int(getattr(self.timings, name))
            if mine:
                setattr(timings, name, mine)
        timings.total = self.elapsed_ms()
        return seal(timings)


def emit(timings: Timings, *, ok: bool = True, **fields: object) -> None:
    """Write one `stage_complete` line per stage (OPS-1).

    This is what `scripts/rollup.py` reads to produce p50/p95 per stage. `total` is not
    emitted here — it rides on the request's own completion line, which is the outermost
    measurement and the one PERF-1 is stated against.
    """
    for name in STAGE_NAMES:
        duration = int(getattr(timings, name))
        if duration or name in ALWAYS_LOGGED:
            applog.log(
                "stage_complete", stage=name, duration_ms=duration, ok=ok, **fields
            )


@dataclass(frozen=True)
class ModelPrice:
    """US dollars per million tokens, at list price.

    Cache multipliers are properties of the API rather than of a model: a cached read
    costs a tenth of an input token, and writing an entry costs 1.25x (5-minute TTL).
    They are fields rather than constants so a model that ever prices them differently
    can say so here instead of somewhere else.
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_read_multiplier: float = 0.1
    cache_write_multiplier: float = 1.25

    def usd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> float:
        per_token = 1 / 1_000_000
        return round(
            input_tokens * self.input_per_mtok * per_token
            + output_tokens * self.output_per_mtok * per_token
            + cache_read_tokens
            * self.input_per_mtok
            * self.cache_read_multiplier
            * per_token
            + cache_creation_tokens
            * self.input_per_mtok
            * self.cache_write_multiplier
            * per_token,
            6,
        )


#: List price per model, keyed by id prefix (OPS-4).
#:
#: **Keyed by model, and that is the whole point.** The provider adapter hardcodes Opus 5's
#: rates in three module constants that `estimated_usd` applies to whatever it is handed.
#: `LABELPROOF_EXTRACTION_MODEL` is an environment variable, and the model the 5-second
#: gate actually points at is Haiku 4.5 — so the obvious configuration change silently
#: made every cost line 5x too high, with nothing to catch it.
#:
#: Anthropic first-party list price, checked 2026-08-11. Sonnet 5 additionally carries an
#: introductory rate of $2/$10 through 2026-08-31; the list rate is used here on purpose —
#: a cost analysis built on a rate that expires in three weeks is a cost analysis with a
#: short shelf life, and over-stating is the safe direction for a number someone budgets
#: against.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-5": ModelPrice(input_per_mtok=5.0, output_per_mtok=25.0),
    "claude-sonnet-5": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0),
    "claude-haiku-4-5": ModelPrice(input_per_mtok=1.0, output_per_mtok=5.0),
}

#: What an unrecognised model is priced at. Deliberately the most expensive tier known.
#:
#: Guessing low would under-report, and under-reporting is the direction that gets a
#: number into a budget it cannot support. Pricing an unknown model at zero — the other
#: obvious option — is worse still: it looks like the call was free.
UNKNOWN_MODEL_PRICE: ModelPrice = max(
    PRICES.values(), key=lambda price: price.input_per_mtok
)


def price_for(model: str) -> tuple[ModelPrice, bool]:
    """The price list for `model`, and whether it was actually recognised.

    Prefix match, because a pinned id and its dated variants are the same model at the
    same price.
    """
    for known, price in PRICES.items():
        if model.startswith(known):
            return price, True
    return UNKNOWN_MODEL_PRICE, False


def usd_for(cost: Cost, model: str = "") -> float:
    """List-price cost of one verification (OPS-4).

    Never raises. A cost line is worth showing and never worth failing a verification
    over — but an unknown model is logged loudly rather than silently priced as Opus.
    """
    tokens = (
        cost.input_tokens,
        cost.output_tokens,
        cost.cache_read_tokens,
        cost.cache_creation_tokens,
    )
    if not any(tokens):
        return 0.0

    price, known = price_for(model)
    if model and not known:
        applog.warn(
            "cost_model_unknown",
            model=model,
            reason_code="no_price_list",
            usd=0.0,
        )
    return price.usd(
        input_tokens=cost.input_tokens,
        output_tokens=cost.output_tokens,
        cache_read_tokens=cost.cache_read_tokens,
        cache_creation_tokens=cost.cache_creation_tokens,
    )


def cost_line(cost: Cost, *, model: str = "", **fields: object) -> None:
    """The per-request cost line (LP-118, OPS-4).

    A dedicated event rather than three more keys on `verify_complete`, because the Cost
    Analysis deliverable is produced by grepping one event name out of a log file and
    summing a column. Tokens and dollars are the only two things this line is for.

    Cached reads and cache writes are carried separately because they are priced
    separately — a tenth of an input token, and 1.25x an input token respectively. The
    provider's `input_tokens` excludes both, so folding them in would misprice a
    warm-cache request and dropping them makes those tokens free.
    """
    applog.log(
        "verification_cost",
        input_tokens=cost.input_tokens,
        output_tokens=cost.output_tokens,
        cache_read_tokens=cost.cache_read_tokens,
        cache_creation_tokens=cost.cache_creation_tokens,
        usd=cost.usd if cost.usd else usd_for(cost, model),
        model=model,
        **fields,
    )
