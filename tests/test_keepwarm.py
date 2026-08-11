"""Keep-warm and cache pre-warm behaviour (LP-134, LP-324, LP-132).

Three things here are load-bearing enough to assert rather than trust:

1. A deployed instance answering `simulated: true` is a **failure**, not a healthy
   server. It returns 200 to every status-code check in existence while serving replayed
   fixtures to someone who cannot tell.
2. The pre-warm interval must stay under the provider's cache TTL. Above it, every ping
   pays for a cache write and the optimisation inverts into a cost.
3. A cache that never engages must say so. It fails silently by design — no error, just
   a full-price bill and a latency budget built on a saving that never arrived.
"""

from __future__ import annotations

import contextlib
import io
import json
from typing import Any

import pytest

from api import logging as lp_logging
from scripts import keepwarm


@pytest.fixture(autouse=True)
def _capture() -> io.StringIO:
    stream = io.StringIO()
    lp_logging.configure(stream=stream)
    return stream


def _events(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _responses(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, tuple[int, dict]]) -> None:
    """Answer `_get_json` from a table keyed by path suffix."""

    def fake(url: str, timeout: float = 5.0) -> tuple[int, dict]:
        for suffix, response in mapping.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected keepwarm request to {url}")

    monkeypatch.setattr(keepwarm, "_get_json", fake)


# --- the sample-mode assertion (LP-132) ----------------------------------------------


def test_simulated_provider_is_not_ready(
    monkeypatch: pytest.MonkeyPatch, _capture: io.StringIO
) -> None:
    """Sample mode in production fails the check and is logged as an error.

    `/ready` returns 200 here. That is the whole problem: a platform check matching status
    codes calls this healthy, and every verdict served is a demonstration.
    """
    _responses(
        monkeypatch,
        {
            "/health": (200, {"status": "ok"}),
            "/ready": (200, {"status": "sample_mode", "simulated": True, "model": "none"}),
        },
    )

    assert keepwarm.check_local("http://127.0.0.1:8080") is False

    events = _events(_capture)
    assert any(e["event"] == "keepwarm_simulated_provider" for e in events)


def test_live_provider_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _responses(
        monkeypatch,
        {
            "/health": (200, {"status": "ok"}),
            "/ready": (
                200,
                {"status": "ready", "simulated": False, "provider": "anthropic", "model": "m"},
            ),
        },
    )

    assert keepwarm.check_local("http://127.0.0.1:8080") is True


def test_missing_simulated_field_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absence is not permission. If the server does not say whether it is simulated,
    keep-warm does not assume it is not."""
    _responses(
        monkeypatch,
        {"/health": (200, {"status": "ok"}), "/ready": (200, {"status": "ready"})},
    )

    assert keepwarm.check_local("http://127.0.0.1:8080") is False


def test_provider_outage_is_not_ready(
    monkeypatch: pytest.MonkeyPatch, _capture: io.StringIO
) -> None:
    _responses(
        monkeypatch,
        {
            "/health": (200, {"status": "ok"}),
            "/ready": (503, {"error": {"kind": "provider", "code": "provider_unavailable"}}),
        },
    )

    assert keepwarm.check_local("http://127.0.0.1:8080") is False
    assert any(e["event"] == "keepwarm_not_ready" for e in _events(_capture))


# --- the interval must stay under the cache TTL (LP-324) ------------------------------


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("240", 240),
        ("60", 60),
        # Above the provider's five-minute ephemeral TTL, every ping writes a cache entry
        # nothing will ever read. Clamped rather than honoured.
        ("600", keepwarm.MAX_INTERVAL_S),
        ("3600", keepwarm.MAX_INTERVAL_S),
        # Absurdly low is throttled too — a warm-up loop is not a load generator.
        ("1", 30),
        ("not-a-number", keepwarm.DEFAULT_INTERVAL_S),
    ],
)
def test_interval_is_clamped_below_the_cache_ttl(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: int
) -> None:
    monkeypatch.setenv("LABELPROOF_KEEPWARM_INTERVAL_S", configured)
    assert keepwarm.Settings.from_env().interval_s == expected


def test_default_interval_leaves_slack_under_the_ttl() -> None:
    """300s is the provider's ephemeral TTL. The default must clear it with room for a
    slow ping and a retry, not land on the boundary."""
    assert keepwarm.DEFAULT_INTERVAL_S < 300
    assert keepwarm.MAX_INTERVAL_S < 300
    assert 300 - keepwarm.DEFAULT_INTERVAL_S >= 30


# --- the cache must prove it engaged --------------------------------------------------


def test_repeated_cache_absence_is_reported(_capture: io.StringIO) -> None:
    """No read and no write means the prefix is not caching at all.

    The usual cause is a system prompt shorter than the model's minimum cacheable size —
    a threshold that varies by model and produces no error, just a silent full-price bill.
    """
    warmer = keepwarm.CacheWarmer(model="test-model", api_key="k")

    for _ in range(keepwarm.MISS_STREAK_BEFORE_WARNING):
        warmer._note(read=0, written=0)

    events = _events(_capture)
    warnings = [e for e in events if e["event"] == "keepwarm_cache_not_engaging"]
    assert len(warnings) == 1
    assert warnings[0]["reason_code"] == "prefix_below_model_minimum"


def test_a_successful_read_clears_the_streak(_capture: io.StringIO) -> None:
    warmer = keepwarm.CacheWarmer(model="test-model", api_key="k")

    warmer._note(read=0, written=0)
    warmer._note(read=0, written=2000)  # first write after a TTL lapse — expected
    warmer._note(read=2000, written=0)
    warmer._note(read=0, written=0)

    assert not [e for e in _events(_capture) if e["event"] == "keepwarm_cache_not_engaging"]


def test_eviction_is_distinguished_from_never_caching(_capture: io.StringIO) -> None:
    """Once a read has been seen, a later run of misses is eviction, not a broken prefix.
    Different diagnosis, different fix — the log should not conflate them."""
    warmer = keepwarm.CacheWarmer(model="test-model", api_key="k")
    warmer._note(read=2000, written=0)

    for _ in range(keepwarm.MISS_STREAK_BEFORE_WARNING):
        warmer._note(read=0, written=0)

    warnings = [e for e in _events(_capture) if e["event"] == "keepwarm_cache_not_engaging"]
    assert warnings and warnings[0]["reason_code"] == "cache_evicted"


def test_the_warm_request_targets_the_same_cache_entry_as_a_real_extraction() -> None:
    """The test that would have caught the pre-warm warming the wrong entry.

    Prompt caching keys on the whole rendered prefix, not just `system`. The original
    warm request omitted `output_config.format` (which carries the ~2.3k-token extraction
    schema) and `thinking`/`effort` (which render ahead of `system`), so it wrote a
    ~2.1k-token entry, read its own entry back, reported a healthy cache forever, and the
    real request paid a full ~4.4k-token write every single time.

    Asserted by capturing what the adapter actually sends rather than by reading the
    adapter's source, so this fails if `_one_call` changes shape.
    """
    from api.config import Config
    from api.models import Commodity
    from api.provider.anthropic_adapter import AnthropicVisionProvider
    from api.provider.base import ImageInput

    captured: dict[str, Any] = {}

    class Capturing:
        def with_options(self, **_: object) -> Capturing:
            return self

        @property
        def messages(self) -> Capturing:
            return self

        def create(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            raise RuntimeError("captured — the response is not the point")

    config = Config(anthropic_api_key="k", extraction_model="claude-opus-5", effort="low")
    provider = AnthropicVisionProvider(config, client=Capturing())

    # The capturing client raises after recording the request; the response is not what
    # this test is about.
    with contextlib.suppress(Exception):
        provider._one_call(
            ImageInput(index=0, data=b"\x89PNG", media_type="image/png", role="single"),
            Commodity.SPIRITS,
            5.0,
        )

    assert captured, "the adapter did not issue a request; the capture harness is stale"

    warm = keepwarm.cache_parameters(config.extraction_model, config.effort)

    # Everything that renders at or before the cache breakpoint must match exactly.
    for key in ("model", "system", "output_config", "thinking"):
        assert warm.get(key) == captured.get(key), (
            f"the pre-warm and the real extraction disagree on '{key}', so they address "
            f"different cache entries. The warm entry would be read only by the warmer."
        )


def test_the_warm_request_does_not_pin_what_lives_after_the_breakpoint() -> None:
    """`messages` and `max_tokens` sit after the cache breakpoint, so they must NOT be in
    the shared parameter set — pinning them would force the warmer to send an image."""
    warm = keepwarm.cache_parameters("claude-opus-5", "low")
    assert "messages" not in warm
    assert "max_tokens" not in warm


def test_thinking_is_omitted_on_models_that_reject_it() -> None:
    """Haiku 4.5 returns a 400 for `thinking` and `output_config.effort`. The warm request
    follows the adapter's own capability gate rather than a second copy of the rule."""
    warm = keepwarm.cache_parameters("claude-haiku-4-5", "low")
    assert "thinking" not in warm
    assert "effort" not in warm["output_config"]
    # The schema is not optional — it is most of the cached prefix.
    assert warm["output_config"]["format"]["type"] == "json_schema"


def test_warm_request_survives_a_provider_outage(_capture: io.StringIO) -> None:
    """A provider failure is logged and swallowed. Keep-warm is an optimisation; it must
    never be the reason label verification stops."""

    class Exploding:
        def with_options(self, **_: object) -> Exploding:
            return self

        @property
        def messages(self) -> Exploding:
            return self

        def create(self, **_: object) -> None:
            raise RuntimeError("connection refused")

    warmer = keepwarm.CacheWarmer(model="test-model", api_key="k", _client=Exploding())

    assert warmer.warm() is False
    assert any(e["event"] == "keepwarm_cache_failed" for e in _events(_capture))


# --- the loop is not killable ---------------------------------------------------------


def test_a_failing_tick_does_not_stop_the_loop(
    monkeypatch: pytest.MonkeyPatch, _capture: io.StringIO
) -> None:
    def explode(_: str) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(keepwarm, "check_local", explode)
    settings = keepwarm.Settings(enabled=True, interval_s=30, warm_cache=False)

    assert keepwarm.run(settings, ticks=3, sleep=lambda _: None) == 3
    assert len([e for e in _events(_capture) if e["event"] == "keepwarm_tick_failed"]) == 3


def test_cache_warm_is_skipped_when_the_server_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No point warming a prompt cache for a server that cannot answer — and in sample
    mode, warming would spend real money on behalf of a deployment that is already
    broken."""
    monkeypatch.setattr(keepwarm, "check_local", lambda _: False)

    calls: list[int] = []

    class Counting(keepwarm.CacheWarmer):
        def warm(self) -> bool:
            calls.append(1)
            return True

    settings = keepwarm.Settings(enabled=True, interval_s=30, warm_cache=True, api_key="k")
    monkeypatch.setattr(keepwarm, "CacheWarmer", Counting)

    keepwarm.run(settings, ticks=2, sleep=lambda _: None)
    assert calls == []


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch, _capture: io.StringIO) -> None:
    """`docker run` must not make paid API calls. Keep-warm is opt-in and says so."""
    monkeypatch.delenv("LABELPROOF_KEEPWARM", raising=False)
    # main() reconfigures logging for the sidecar process; keep the captured stream.
    monkeypatch.setattr(lp_logging, "configure", lambda *args, **kwargs: None)

    assert keepwarm.main() == 0
    assert any(e["event"] == "keepwarm_disabled" for e in _events(_capture))
