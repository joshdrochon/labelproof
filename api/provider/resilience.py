"""Timeouts, bounded retries, and a circuit breaker for the provider path (ENG-4).

Three different failures wear the same face — "the model did not answer" — and each
needs a different mechanism:

* **A slow call** is handled by a `Deadline`. Every attempt is given the time that is
  actually left in the request budget, never a fixed timeout that could outlive it. This
  is what keeps LP-079 honest: over budget returns a plain sentence, never a hang.
* **A flaky call** is handled by `RetryPolicy` — bounded attempts with full-jitter
  exponential backoff. Jitter is not decoration; four images retrying in lockstep is a
  self-inflicted thundering herd against the same rate limit.
* **A dead provider** is handled by `CircuitBreaker`. Once the service has failed enough
  times in a row, spending the whole 5-second budget rediscovering that on every request
  is worse than saying so immediately. An open breaker answers in microseconds with a
  sentence a compliance agent can act on (TC-21, NET-3).

Everything here is deterministic under injection: the clock, the sleep, and the jitter
source are all parameters, so the tests never sleep and never flake.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from api import logging as lp_logging
from api.provider.base import ProviderError

Clock = Callable[[], float]

#: Said when the breaker is open. It names the state of the world and what to do next,
#: because "provider_unavailable: circuit open" is engineer-speak and the reader is a
#: compliance agent with a label in front of them (UX-6).
BREAKER_OPEN_MESSAGE: Final[str] = (
    "The label reading service has been failing repeatedly, so this label was not "
    "sent for checking. Nothing has been checked and no application data has been "
    "changed. Try again in a few minutes, or check this label by eye."
)

#: Said when the request budget runs out mid-flight.
DEADLINE_MESSAGE: Final[str] = (
    "The label reading service did not answer in the time this check is allowed to "
    "take. Nothing has been checked and no application data has been changed. Try "
    "again, or check this label by eye."
)


class Deadline:
    """A wall-clock budget shared by every attempt in one logical call.

    Budgeted against the 5s gate from `Config` (LP-059). The point of threading one
    deadline through retries — rather than giving each attempt its own timeout — is that
    three 4-second attempts do not add up to twelve seconds of a five-second request.
    """

    def __init__(self, budget_ms: int, *, clock: Clock = time.monotonic) -> None:
        self.budget_ms = max(0, budget_ms)
        self._clock = clock
        self._started = clock()

    def _elapsed_ms_exact(self) -> float:
        return (self._clock() - self._started) * 1000

    @property
    def elapsed_ms(self) -> int:
        # Rounded, not truncated: floating-point clock arithmetic turns 400ms into
        # 399.9999, and truncating there hands out a millisecond nobody has.
        return round(self._elapsed_ms_exact())

    @property
    def remaining_ms(self) -> int:
        return max(0, round(self.budget_ms - self._elapsed_ms_exact()))

    @property
    def expired(self) -> bool:
        return self.remaining_ms <= 0

    def remaining_seconds(self) -> float:
        return self.remaining_ms / 1000.0


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retries with full-jitter exponential backoff (LP-060).

    Full jitter — `uniform(0, cap)` rather than `cap` or `cap/2 + uniform(0, cap/2)` —
    because the workload is a fan-out: four images fail together against one rate limit,
    and any scheme that keeps a floor under the delay keeps them synchronised. Full
    jitter spreads them across the whole window.

    Defaults are sized for a 5-second request, not for a background job. Three attempts
    with a 600ms ceiling is what fits; a minute of patient backoff does not.
    """

    max_attempts: int = 3
    base_delay_ms: int = 80
    max_delay_ms: int = 600
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

    def ceiling_ms(self, attempt: int) -> int:
        """The un-jittered upper bound for the delay after `attempt` (1-based)."""
        raw = self.base_delay_ms * (self.multiplier ** (attempt - 1))
        return int(min(float(self.max_delay_ms), raw))

    def delay_ms(self, attempt: int, rand: Callable[[], float] = random.random) -> int:
        """Full jitter: a uniform draw from `[0, ceiling]`."""
        return int(self.ceiling_ms(attempt) * rand())


class CircuitBreaker:
    """Opens after repeated failures; recovers by letting exactly one probe through.

    Only *retryable* failures move the breaker. A malformed request or a bad API key
    fails every time and fails instantly — opening the breaker over it would replace one
    immediate plain-language error with a different immediate plain-language error while
    hiding the real cause. The breaker exists to stop us burning the request budget on a
    service that is down, and a 400 does not burn the budget.

    Thread-safe: images extract concurrently, so several threads report into one breaker.
    """

    CLOSED: Final[str] = "closed"
    OPEN: Final[str] = "open"
    HALF_OPEN: Final[str] = "half_open"

    def __init__(
        self,
        *,
        failure_threshold: int = 4,
        reset_after_ms: int = 15_000,
        clock: Clock = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self.failure_threshold = failure_threshold
        self.reset_after_ms = reset_after_ms
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    # --- state ----------------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._opened_at is None:
            return self.CLOSED
        elapsed_ms = (self._clock() - self._opened_at) * 1000
        return self.HALF_OPEN if elapsed_ms >= self.reset_after_ms else self.OPEN

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    # --- the gate -------------------------------------------------------------------

    def before_call(self) -> None:
        """Raise rather than let a doomed call through. Never blocks, never sleeps."""
        with self._lock:
            state = self._state_locked()
            if state == self.CLOSED:
                return
            if state == self.HALF_OPEN and not self._probe_in_flight:
                self._probe_in_flight = True
                lp_logging.log("circuit_breaker", status=self.HALF_OPEN, provider="anthropic")
                return
        raise ProviderError(BREAKER_OPEN_MESSAGE, retryable=False)

    def record_success(self) -> None:
        with self._lock:
            reopened = self._opened_at is not None
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False
        if reopened:
            lp_logging.log("circuit_breaker", status=self.CLOSED, provider="anthropic")

    def record_failure(self) -> None:
        with self._lock:
            was_probe = self._probe_in_flight
            self._probe_in_flight = False
            if was_probe:
                # The probe failed. Straight back to open, full timer.
                self._opened_at = self._clock()
                opened = True
            else:
                self._failures += 1
                opened = self._failures >= self.failure_threshold and self._opened_at is None
                if opened:
                    self._opened_at = self._clock()
        if opened:
            lp_logging.warn("circuit_breaker", status=self.OPEN, provider="anthropic")


def call_with_retries[T](
    fn: Callable[[float], T],
    *,
    policy: RetryPolicy,
    deadline: Deadline,
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    provider: str = "anthropic",
) -> T:
    """Run `fn` until it succeeds, the attempts run out, or the budget does.

    `fn` is handed the seconds remaining in the budget and is expected to enforce that
    as its own timeout — a retry wrapper that lets one attempt run forever has not
    bounded anything.

    Raises `ProviderError` and nothing else. Callers translate it into a degradation
    message; there is no path here that returns a stack trace or hangs.
    """
    last: ProviderError | None = None

    for attempt in range(1, policy.max_attempts + 1):
        if deadline.expired:
            raise ProviderError(DEADLINE_MESSAGE, retryable=False) from last

        if breaker is not None:
            breaker.before_call()

        try:
            result = fn(deadline.remaining_seconds())
        except ProviderError as err:
            last = err
            if err.retryable and breaker is not None:
                breaker.record_failure()
            if not err.retryable or attempt == policy.max_attempts:
                raise

            delay_ms = policy.delay_ms(attempt, rand)
            lp_logging.warn(
                "provider_retry",
                provider=provider,
                attempt=attempt,
                duration_ms=delay_ms,
                reason_code="retryable_error",
            )
            if deadline.remaining_ms <= delay_ms:
                # Sleeping would spend the rest of the budget waiting rather than
                # working. Say so now instead of timing out silently later.
                raise ProviderError(DEADLINE_MESSAGE, retryable=False) from err
            sleep(delay_ms / 1000.0)
        else:
            if breaker is not None:
                breaker.record_success()
            return result

    # Unreachable: the loop either returns or raises. Kept so the type is honest.
    raise last or ProviderError(DEADLINE_MESSAGE, retryable=False)
