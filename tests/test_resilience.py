"""Timeouts, bounded retries, and the circuit breaker (LP-059, LP-060, LP-061).

No test here sleeps or touches a real clock. The clock, the sleep and the jitter source
are all injected, which is the only way a retry suite is both fast and non-flaky.
"""

from __future__ import annotations

import threading

import pytest

from api.provider.base import ProviderError
from api.provider.resilience import (
    BREAKER_OPEN_MESSAGE,
    DEADLINE_MESSAGE,
    CircuitBreaker,
    Deadline,
    RetryPolicy,
    call_with_retries,
)


class FakeClock:
    """A monotonic clock that only moves when the test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000.0


class RecordingSleep:
    """Records sleeps and advances the clock, so a deadline actually elapses."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance_ms(seconds * 1000)


def constant(value: float):
    return lambda: value


# --- Deadline -----------------------------------------------------------------------


def test_deadline_counts_down_and_expires() -> None:
    clock = FakeClock()
    deadline = Deadline(1000, clock=clock)

    assert deadline.remaining_ms == 1000
    assert not deadline.expired

    clock.advance_ms(400)
    assert deadline.remaining_ms == 600
    assert deadline.remaining_seconds() == pytest.approx(0.6)

    clock.advance_ms(700)
    assert deadline.remaining_ms == 0
    assert deadline.expired


def test_deadline_never_reports_negative_time() -> None:
    """Callers pass `remaining_seconds()` to an SDK. A negative timeout is nonsense."""
    clock = FakeClock()
    deadline = Deadline(100, clock=clock)
    clock.advance_ms(5000)
    assert deadline.remaining_ms == 0
    assert deadline.remaining_seconds() == 0.0


# --- RetryPolicy --------------------------------------------------------------------


def test_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(base_delay_ms=100, max_delay_ms=400, multiplier=2.0)
    assert policy.ceiling_ms(1) == 100
    assert policy.ceiling_ms(2) == 200
    assert policy.ceiling_ms(3) == 400
    assert policy.ceiling_ms(4) == 400  # capped, not runaway
    assert policy.ceiling_ms(9) == 400


def test_jitter_spans_the_whole_window() -> None:
    """Full jitter, not half. Four images backing off in lockstep is the failure mode."""
    policy = RetryPolicy(base_delay_ms=100, max_delay_ms=400)
    assert policy.delay_ms(2, constant(0.0)) == 0
    assert policy.delay_ms(2, constant(0.5)) == 100
    assert policy.delay_ms(2, constant(0.999)) == 199


def test_jitter_never_exceeds_the_ceiling() -> None:
    policy = RetryPolicy(base_delay_ms=80, max_delay_ms=600)
    for attempt in range(1, 8):
        for draw in (0.0, 0.25, 0.5, 0.75, 0.9999):
            assert 0 <= policy.delay_ms(attempt, constant(draw)) <= policy.ceiling_ms(attempt)


def test_a_policy_with_no_attempts_is_rejected() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)


# --- call_with_retries --------------------------------------------------------------


def test_a_successful_call_is_made_once() -> None:
    clock = FakeClock()
    calls: list[float] = []

    result = call_with_retries(
        lambda remaining: (calls.append(remaining), "ok")[1],
        policy=RetryPolicy(),
        deadline=Deadline(4000, clock=clock),
        sleep=RecordingSleep(clock),
    )

    assert result == "ok"
    assert len(calls) == 1


def test_a_retryable_failure_is_retried_up_to_the_bound() -> None:
    clock = FakeClock()
    attempts: list[int] = []

    def always_fails(_remaining: float) -> str:
        attempts.append(1)
        raise ProviderError("connection reset", retryable=True)

    with pytest.raises(ProviderError):
        call_with_retries(
            always_fails,
            policy=RetryPolicy(max_attempts=3, base_delay_ms=10, max_delay_ms=20),
            deadline=Deadline(10_000, clock=clock),
            sleep=RecordingSleep(clock),
            rand=constant(0.5),
        )

    assert len(attempts) == 3, "bounded — three attempts, not an unbounded loop"


def test_a_non_retryable_failure_is_not_retried() -> None:
    clock = FakeClock()
    attempts: list[int] = []

    def bad_request(_remaining: float) -> str:
        attempts.append(1)
        raise ProviderError("that image is not an image", retryable=False)

    with pytest.raises(ProviderError, match="not an image"):
        call_with_retries(
            bad_request,
            policy=RetryPolicy(max_attempts=5),
            deadline=Deadline(10_000, clock=clock),
            sleep=RecordingSleep(clock),
        )

    assert len(attempts) == 1


def test_a_retry_succeeds_and_the_result_comes_back() -> None:
    clock = FakeClock()
    state = {"calls": 0}

    def flaky(_remaining: float) -> str:
        state["calls"] += 1
        if state["calls"] < 3:
            raise ProviderError("overloaded", retryable=True)
        return "read the label"

    result = call_with_retries(
        flaky,
        policy=RetryPolicy(max_attempts=3, base_delay_ms=10),
        deadline=Deadline(10_000, clock=clock),
        sleep=RecordingSleep(clock),
        rand=constant(0.5),
    )
    assert result == "read the label"
    assert state["calls"] == 3


def test_each_attempt_gets_less_time_than_the_last() -> None:
    """The deadline is shared. Three four-second attempts do not fit a five-second gate."""
    clock = FakeClock()
    sleep = RecordingSleep(clock)
    remaining_seen: list[float] = []

    def slow_failure(remaining: float) -> str:
        remaining_seen.append(remaining)
        clock.advance_ms(300)
        raise ProviderError("timed out", retryable=True)

    with pytest.raises(ProviderError):
        call_with_retries(
            slow_failure,
            policy=RetryPolicy(max_attempts=3, base_delay_ms=100, max_delay_ms=100),
            deadline=Deadline(4000, clock=clock),
            sleep=sleep,
            rand=constant(0.5),
        )

    assert remaining_seen == sorted(remaining_seen, reverse=True)
    assert remaining_seen[0] > remaining_seen[-1]


def test_an_expired_deadline_stops_before_calling_anything() -> None:
    clock = FakeClock()
    deadline = Deadline(1000, clock=clock)
    clock.advance_ms(2000)

    def must_not_run(_remaining: float) -> str:  # pragma: no cover — asserted below
        raise AssertionError("called past the deadline")

    with pytest.raises(ProviderError) as exc:
        call_with_retries(
            must_not_run, policy=RetryPolicy(), deadline=deadline, sleep=RecordingSleep(clock)
        )
    assert str(exc.value) == DEADLINE_MESSAGE
    assert exc.value.retryable is False


def test_we_never_sleep_past_the_budget() -> None:
    """TC-21/NET-3: over budget is a sentence, never a wait that outlives the request."""
    clock = FakeClock()
    sleep = RecordingSleep(clock)

    def slow_failure(_remaining: float) -> str:
        clock.advance_ms(900)
        raise ProviderError("timed out", retryable=True)

    with pytest.raises(ProviderError) as exc:
        call_with_retries(
            slow_failure,
            policy=RetryPolicy(max_attempts=5, base_delay_ms=500, max_delay_ms=500),
            deadline=Deadline(1000, clock=clock),
            sleep=sleep,
            rand=constant(1.0),
        )

    assert str(exc.value) == DEADLINE_MESSAGE
    assert sleep.calls == [], "there was no room to back off, so we did not"
    assert clock.now - 1000.0 <= 1.0


def test_the_degradation_message_is_plain_language() -> None:
    """A compliance agent reads this, not an engineer (UX-6)."""
    for message in (DEADLINE_MESSAGE, BREAKER_OPEN_MESSAGE):
        assert "Nothing has been checked" in message
        assert "no application data has been changed" in message
        for jargon in ("circuit", "timeout", "exception", "HTTP", "retry_after", "None"):
            assert jargon not in message


# --- CircuitBreaker -----------------------------------------------------------------


def test_the_breaker_opens_after_repeated_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=3, clock=FakeClock())
    assert breaker.state == CircuitBreaker.CLOSED

    for _ in range(2):
        breaker.record_failure()
    assert breaker.state == CircuitBreaker.CLOSED
    breaker.before_call()  # still letting calls through

    breaker.record_failure()
    assert breaker.state == CircuitBreaker.OPEN


def test_an_open_breaker_refuses_without_calling_the_provider() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, clock=clock)
    breaker.record_failure()

    def must_not_run(_remaining: float) -> str:  # pragma: no cover — asserted below
        raise AssertionError("called through an open breaker")

    with pytest.raises(ProviderError) as exc:
        call_with_retries(
            must_not_run,
            policy=RetryPolicy(),
            deadline=Deadline(4000, clock=clock),
            breaker=breaker,
            sleep=RecordingSleep(clock),
        )

    assert str(exc.value) == BREAKER_OPEN_MESSAGE
    assert exc.value.retryable is False
    assert clock.now == 1000.0, "an open breaker answers immediately — it never hangs"


def test_success_closes_a_recovering_breaker() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_after_ms=5000, clock=clock)
    breaker.record_failure()
    assert breaker.state == CircuitBreaker.OPEN

    clock.advance_ms(5000)
    assert breaker.state == CircuitBreaker.HALF_OPEN

    result = call_with_retries(
        lambda _remaining: "ok",
        policy=RetryPolicy(),
        deadline=Deadline(4000, clock=clock),
        breaker=breaker,
        sleep=RecordingSleep(clock),
    )
    assert result == "ok"
    assert breaker.state == CircuitBreaker.CLOSED
    assert breaker.failures == 0


def test_only_one_probe_gets_through_while_half_open() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_after_ms=5000, clock=clock)
    breaker.record_failure()
    clock.advance_ms(5000)

    breaker.before_call()  # the probe
    with pytest.raises(ProviderError, match="failing repeatedly"):
        breaker.before_call()


def test_a_failed_probe_reopens_the_breaker_for_the_full_window() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_after_ms=5000, clock=clock)
    breaker.record_failure()
    clock.advance_ms(5000)
    assert breaker.state == CircuitBreaker.HALF_OPEN

    breaker.before_call()
    breaker.record_failure()

    assert breaker.state == CircuitBreaker.OPEN
    clock.advance_ms(4999)
    assert breaker.state == CircuitBreaker.OPEN
    clock.advance_ms(1)
    assert breaker.state == CircuitBreaker.HALF_OPEN


def test_a_non_retryable_failure_does_not_trip_the_breaker() -> None:
    """A 400 is our bug, not an outage. Opening over it would hide the real cause."""
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, clock=clock)

    for _ in range(5):
        with pytest.raises(ProviderError):
            call_with_retries(
                lambda _r: (_ for _ in ()).throw(ProviderError("bad request", retryable=False)),
                policy=RetryPolicy(),
                deadline=Deadline(4000, clock=clock),
                breaker=breaker,
                sleep=RecordingSleep(clock),
            )

    assert breaker.state == CircuitBreaker.CLOSED


def test_the_breaker_counts_correctly_under_concurrency() -> None:
    """Images extract in parallel, so several threads report into one breaker."""
    breaker = CircuitBreaker(failure_threshold=1000, clock=FakeClock())
    barrier = threading.Barrier(8)

    def report() -> None:
        barrier.wait()
        for _ in range(50):
            breaker.record_failure()

    threads = [threading.Thread(target=report) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert breaker.failures == 400
