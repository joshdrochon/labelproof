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

from api import logging as applog
from api.models import Timings

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
