"""Keep-warm and prompt-cache pre-warm (LP-134, LP-324, PERF-6).

The adoption gate is p95 ≤ 5s, and a measured two-image extraction already spends most
of it. Everything else — machine wake, Python import, TLS handshake to the provider,
the provider re-reading a system prompt it has seen a thousand times — has to be gone
before the grader clicks, not amortised over their session. This process removes it.

Three things go cold on an idle service, and they are not the same thing:

**The machine.** Handled by the platform, not here: `fly.toml` sets
`min_machines_running = 1` with `auto_stop_machines = "off"`, so there is no machine to
wake. This loop's local `/health` and `/ready` calls are a second, cheaper line — they
keep the ASGI stack, the JSON encoder and the accept loop hot, and they turn "the
server is wedged" into a log line rather than a discovery the grader makes.

**The provider's prompt cache** (LP-324). `api/provider/anthropic_adapter.py` puts a
`cache_control` breakpoint on the last system block precisely so the seven-field
instruction prefix is read from cache rather than re-processed. An ephemeral cache entry
lives five minutes. Ping more often than that and the grader's first verification is a
cache *read*; ping less often, or not at all, and it is the request that pays for the
*write*. The interval below is four minutes for that reason and no other.

**The TLS connection pool.** Not fixable from here, and worth saying plainly rather than
implying otherwise: the SDK client lives in the uvicorn process, this loop is a separate
process, and httpx expires idle keep-alive connections after a few seconds regardless.
The grader's first request pays one handshake to `api.anthropic.com`. The prompt cache
is the part that was worth buying.

**Honesty about whether it worked.** A prompt cache that silently fails to engage is
worse than no cache, because the latency budget was planned around it. The minimum
cacheable prefix is model-dependent and this system prompt is ~1.7-2.2k tokens — under
some models' floor. So every ping reports what the provider actually did with the cache,
and a run of misses is logged as a warning naming the likely cause. Nothing here assumes
the optimisation worked.

Runs as a sidecar inside the application container (see the Dockerfile's `CMD`). It is a
no-op unless `LABELPROOF_KEEPWARM` is truthy, so `docker build && docker run` spends
nothing, and it never raises into the service: if keep-warm dies, the service serves.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from api import logging as applog

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

#: Anthropic's ephemeral cache entries live five minutes. Four leaves a full minute of
#: slack for a slow ping, a retry and clock drift. Raising this above 300 does not make
#: the pings cheaper — it makes them useless, because every one becomes a cache write.
DEFAULT_INTERVAL_S: int = 240

#: Ceiling on the interval, enforced rather than documented. A misconfigured interval
#: silently converts LP-324 from "the grader reads a warm cache" into "the grader and
#: the keep-warm loop both pay for writes", and nothing about the logs would look wrong.
MAX_INTERVAL_S: int = 290

#: The pre-warm sends no image and asks for no output, so this is not a latency budget —
#: it is a "the provider is unreachable" detector.
WARM_TIMEOUT_S: float = 20.0

#: Consecutive cache misses tolerated before the loop says so. One miss is the first
#: ping of the process (there is nothing to read yet); a run of them means the prefix is
#: not caching at all.
MISS_STREAK_BEFORE_WARNING: int = 3


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    enabled: bool = False
    interval_s: int = DEFAULT_INTERVAL_S
    base_url: str = "http://127.0.0.1:8080"
    warm_cache: bool = True
    api_key: str = ""
    model: str = "claude-opus-5"

    @classmethod
    def from_env(cls) -> Settings:
        port = os.environ.get("PORT", "8080")
        interval = _int("LABELPROOF_KEEPWARM_INTERVAL_S", DEFAULT_INTERVAL_S)
        return cls(
            enabled=_truthy("LABELPROOF_KEEPWARM"),
            # Clamped, not just defaulted — see MAX_INTERVAL_S.
            interval_s=max(30, min(interval, MAX_INTERVAL_S)),
            base_url=os.environ.get("LABELPROOF_KEEPWARM_URL", f"http://127.0.0.1:{port}"),
            warm_cache=_truthy("LABELPROOF_KEEPWARM_CACHE", default=True),
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            model=os.environ.get("LABELPROOF_EXTRACTION_MODEL", "claude-opus-5"),
        )


# --------------------------------------------------------------------------------------
# The local half: is this server actually able to check a label?
# --------------------------------------------------------------------------------------


def _get_json(url: str, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
    """GET a JSON endpoint. A non-2xx is a result, not an exception — `/ready` answers
    503 with a body when the provider is unreachable, and that body is the diagnosis."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return int(response.status), json.loads(body)
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code), json.loads(exc.read().decode("utf-8"))
        except Exception:
            return int(exc.code), {}


def check_local(base_url: str) -> bool:
    """Ping `/health` and `/ready`; report whether this server can really verify.

    `/ready` answering 200 is not sufficient. It also answers 200 in sample mode, with
    `simulated: true` — a server that can replay the built-in examples and nothing else.
    A deployed instance in that state looks healthy to any status-code check and returns
    demonstration verdicts to a grader who has no way to know they are demonstrations.
    That is a deployment failure, and this treats it as one (LP-132).
    """
    healthy = False
    try:
        status, _ = _get_json(f"{base_url}/health")
        healthy = status == 200
        if not healthy:
            applog.warn("keepwarm_unhealthy", status=status)
    except Exception:
        applog.warn("keepwarm_unhealthy", reason_code="unreachable")
        return False

    try:
        status, body = _get_json(f"{base_url}/ready")
    except Exception:
        applog.warn("keepwarm_not_ready", reason_code="unreachable")
        return False

    if status != 200:
        applog.warn("keepwarm_not_ready", status=status, code=str(_error_code(body)))
        return False

    # Fails closed, and the distinction matters. `simulated` absent is not the same as
    # `simulated: false`: it means this server did not answer the question. Treating
    # silence as "live" is how a sample-mode instance gets its prompt cache warmed with
    # real money while looking healthy in the logs. The asymmetry rule applies to
    # operations too — err toward flagging.
    simulated = body.get("simulated")
    if simulated is not False:
        applog.error(
            "keepwarm_simulated_provider",
            status=status,
            reason_code=(
                "sample_mode_in_production" if simulated is True else "simulated_not_reported"
            ),
            model=str(body.get("model", "unknown")),
        )
        return False

    return healthy


def _error_code(body: dict[str, Any]) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code", "unknown"))
    return "unknown"


# --------------------------------------------------------------------------------------
# The provider half: pre-warm the prompt cache (LP-324)
# --------------------------------------------------------------------------------------


@dataclass
class CacheWarmer:
    """Writes and then re-reads the extraction prompt's cached prefix.

    The request is built from the adapter's own `SYSTEM_BLOCKS`, imported rather than
    copied. Prompt caching is a byte-exact prefix match, so a transcribed copy of the
    system prompt would drift on the first prompt edit and produce a warmer that
    diligently warms an entry nothing ever reads.

    What is deliberately *omitted* from the warm request, and why it is still the same
    cache entry:

    - **No image, and a placeholder user message.** The cache breakpoint sits on the last
      system block, so everything after it is outside the cached prefix. The real
      request's image and commodity text land there.
    - **No `output_config.format`.** The structured-output schema is not part of the
      cached prefix (the prefix is tools → system → messages), and `max_tokens: 0` is
      rejected outright when a response format is set.
    - **No `thinking` and no `effort`.** Toggling thinking invalidates the *messages*
      cache, not the tools+system cache this warms. Omitting them also keeps the warmer
      working across extraction models with different thinking parameters, which matters
      because the model is a config value.

    `max_tokens: 0` runs prefill and returns immediately with no content and no output
    tokens billed — the cache write without a generation to pay for or throw away.
    """

    model: str
    api_key: str
    _client: Any = field(default=None, repr=False)
    _miss_streak: int = 0
    _ever_read: bool = False

    def client(self) -> Any:
        if self._client is None:
            import anthropic

            # `max_retries=0`: a warm ping that fails is retried on the next tick, four
            # minutes from now. Silent SDK backoff inside a warm-up call buys nothing and
            # hides the failure from the logs.
            self._client = anthropic.Anthropic(api_key=self.api_key, max_retries=0)
        return self._client

    def warm(self) -> bool:
        from api.provider.anthropic_adapter import SYSTEM_BLOCKS

        started = time.perf_counter()
        try:
            message = self.client().with_options(timeout=WARM_TIMEOUT_S).messages.create(
                model=self.model,
                max_tokens=0,
                system=SYSTEM_BLOCKS,
                messages=[{"role": "user", "content": "warmup"}],
            )
        except Exception as exc:
            applog.warn(
                "keepwarm_cache_failed",
                provider="anthropic",
                model=self.model,
                reason_code=type(exc).__name__,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return False

        usage = getattr(message, "usage", None)
        read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        written = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        uncached = int(getattr(usage, "input_tokens", 0) or 0)

        applog.log(
            "keepwarm_cache",
            provider="anthropic",
            model=self.model,
            duration_ms=int((time.perf_counter() - started) * 1000),
            cache_read_tokens=read,
            input_tokens=uncached,
            count=written,
            reason_code=("cache_read" if read else "cache_written" if written else "cache_absent"),
        )

        self._note(read=read, written=written)
        return True

    def _note(self, *, read: int, written: int) -> None:
        """Say so when the cache is not engaging, instead of letting it look like it is.

        `written == 0 and read == 0` is the failure that matters: the provider neither
        stored the prefix nor served it. The usual cause is that the prefix is shorter
        than the model's minimum cacheable size — a threshold that varies by model and
        produces no error, just a bill at full price and a latency budget built on a
        saving that never arrived.
        """
        if read:
            self._ever_read = True
            self._miss_streak = 0
            return

        if written:
            # The first ping after a process start, or after a TTL lapse. Expected once.
            self._miss_streak = 0
            return

        self._miss_streak += 1
        if self._miss_streak >= MISS_STREAK_BEFORE_WARNING:
            applog.warn(
                "keepwarm_cache_not_engaging",
                provider="anthropic",
                model=self.model,
                count=self._miss_streak,
                reason_code=(
                    "prefix_below_model_minimum" if not self._ever_read else "cache_evicted"
                ),
            )
            self._miss_streak = 0


# --------------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------------


def run(settings: Settings, *, ticks: int | None = None, sleep: Any = time.sleep) -> int:
    """Tick until stopped. `ticks` bounds the loop so the behaviour is testable.

    Nothing in here is allowed to take the service down with it. Keep-warm is an
    optimisation; a broken optimisation that also stops label verification would be a
    strictly worse outcome than a cold start.
    """
    applog.log(
        "keepwarm_started",
        model=settings.model,
        duration_ms=settings.interval_s * 1000,
        ok=settings.warm_cache and bool(settings.api_key),
    )

    warmer: CacheWarmer | None = None
    if settings.warm_cache and settings.api_key:
        warmer = CacheWarmer(model=settings.model, api_key=settings.api_key)
    elif settings.warm_cache:
        # No key means the deployment is already broken in a way `/ready` reports. Say it
        # once at startup rather than once every four minutes forever.
        applog.warn("keepwarm_cache_disabled", reason_code="no_api_key")

    completed = 0
    while ticks is None or completed < ticks:
        try:
            ready = check_local(settings.base_url)
            if ready and warmer is not None:
                warmer.warm()
        except Exception as exc:  # pragma: no cover - the loop must not be killable
            applog.error("keepwarm_tick_failed", reason_code=type(exc).__name__)

        completed += 1
        if ticks is not None and completed >= ticks:
            break
        sleep(settings.interval_s)

    return completed


def main() -> int:
    settings = Settings.from_env()
    applog.configure()

    if not settings.enabled:
        # The default. Local `docker run` and CI must not make paid API calls, so
        # keep-warm is opt-in and says why it is not running.
        applog.log("keepwarm_disabled", reason_code="not_enabled")
        return 0

    # Give uvicorn a moment to bind before the first `/health` call, so the first line in
    # the log is not a spurious "unreachable".
    time.sleep(5)

    try:
        run(settings)
    except KeyboardInterrupt:  # pragma: no cover
        applog.log("keepwarm_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
