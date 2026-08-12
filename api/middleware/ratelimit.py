"""Per-client rate limiting on the public prototype URL (SEC-9, LP-081).

**Lanes, not one bucket.** A single limit across every path fails PRD §225 before the
priority lane is even consulted: `GET /batch/{id}` is polled every second or two while a
300-item job runs, so one shared 30/min budget is spent by the progress poller and the
agent's next Verify Now gets a 429. "Verify Now still works during a batch" would then be
false at the transport layer. Separate buckets make the poller physically incapable of
spending the verification budget, and a test asserts exactly that.

**Health checks are exempt.** A rate-limited `/health` means the platform's own prober can
take the machine out of rotation under the load the limiter exists to survive.

**A bucket, not a window.** Fixed windows let a client send twice the limit across a
boundary and — the failure that matters here — 429 a grader whose click lands at second 59
of a window they had already spent. A token bucket that starts full means the first minute's
worth of requests never wait, which is the demo case, while the sustained rate still settles
at the configured limit.

State is in-process. On the single machine The build spec pinned that is exactly right; at N
machines the effective ceiling becomes N times the limit, which the README states rather than
solving with a Redis this prototype's egress table should not carry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from api import errors
from api import logging as applog
from api.middleware.asgi import (
    ASGIApp,
    Receive,
    Scope,
    Send,
    client_key,
    send_error,
)

#: `POST /batch` hands back a job ID in well under a second, but each one can enqueue a
#: thousand applications. Ten a minute is more submissions than any real session makes and
#: still refuses a script.
BATCH_SUBMIT_PER_MINUTE = 10

#: Progress polling. Four a second is far above what the UI does and far below a flood.
BATCH_READ_PER_MINUTE = 240

#: The SPA's own assets, `/sample`, and anything else. Generous enough that no human
#: clicking around ever meets it; low enough to stop a crawler.
DEFAULT_PER_MINUTE = 600

#: Buckets are cheap but not free, and the client key can be attacker-influenced when a
#: proxy header is trusted. Past this many, idle buckets are dropped.
MAX_TRACKED_CLIENTS = 20_000

#: Below this much of a token, a refill is not worth keeping the bucket alive for.
_FULL_EPSILON = 1e-9


@dataclass
class _Bucket:
    """Tokens plus the moment they were last counted. One per (lane, client)."""

    tokens: float
    updated: float


@dataclass(frozen=True)
class Lane:
    """A named budget and the traffic that draws on it."""

    name: str
    per_minute: int

    @property
    def unlimited(self) -> bool:
        return self.per_minute <= 0


def lanes_for(verify_per_minute: int) -> tuple[Lane, ...]:
    """The lane table. Order is match order; `default` is last and matches everything."""
    return (
        Lane("exempt", 0),
        Lane("verify", max(1, verify_per_minute)),
        Lane("batch_submit", BATCH_SUBMIT_PER_MINUTE),
        Lane("batch_read", BATCH_READ_PER_MINUTE),
        Lane("default", DEFAULT_PER_MINUTE),
    )


def normalise_path(path: str) -> str:
    """Reduce a request path to the form the lane table matches against.

    `//verify`, `/./verify` and `/VERIFY` all reach the same route through a normalising
    proxy while reading, character for character, as something the lane table has never
    heard of — so they would draw on the 600/min default lane instead of the 30/min verify
    lane. Starlette 404s all three today, so this is not exploitable in the shipped app; it
    becomes exploitable the moment anything that normalises sits in front, and the fix costs
    a few string operations per request.

    Case is folded because the lane table decides a *budget*, not a route. Being generous
    about what draws on the expensive lane is free; being strict is a hole.
    """
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/" + "/".join(segments).lower() if segments else "/"


def lane_for(method: str, path: str, lanes: tuple[Lane, ...]) -> Lane:
    """Which budget this request draws on.

    Kept as a function of `(method, path)` rather than a regex table so the routing rule is
    readable next to the reason for it. `path` is normalised here rather than by the caller
    so no caller can forget.
    """
    path = normalise_path(path)
    by_name = {lane.name: lane for lane in lanes}
    if path in ("/health", "/ready"):
        return by_name["exempt"]
    if path == "/verify":
        return by_name["verify"]
    if path == "/batch":
        return by_name["batch_submit" if method == "POST" else "batch_read"]
    if path.startswith("/batch/"):
        # `POST /batch/{id}/retry` re-queues failed items — it starts work, so it draws on
        # the submit budget rather than the generous read one.
        return by_name["batch_submit" if method == "POST" else "batch_read"]
    return by_name["default"]


def too_many_requests() -> errors.UserError:
    """The 429 body, in the words `api/main.py` already uses for this status.

    Plain language with a next step, like every other error this app emits — a grader who
    meets the limiter should read a sentence, not `429 Too Many Requests` (UX-6).
    """
    return errors.UserError(
        "This tool is handling too many requests right now. Wait a moment and submit "
        "again — nothing has been checked.",
        next_step="retry",
        code="too_many_requests",
    )


class RateLimiter:
    """The buckets, separated from the middleware so they can be tested without HTTP.

    Every method takes `now` so the tests drive the clock instead of sleeping. A rate-limit
    suite that sleeps is a rate-limit suite that is slow and flaky, and this one runs inside
    the CI budget (LP-247).
    """

    def __init__(self, lanes: tuple[Lane, ...], *, max_clients: int = MAX_TRACKED_CLIENTS):
        self.lanes = lanes
        self.max_clients = max_clients
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    def check(self, lane: Lane, client: str, *, now: float | None = None) -> float:
        """Spend one token. Returns 0.0 when allowed, or seconds to wait when refused."""
        if lane.unlimited:
            return 0.0

        moment = time.monotonic() if now is None else now
        capacity = float(lane.per_minute)
        per_second = capacity / 60.0
        key = (lane.name, client)

        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_clients:
                self._evict(moment)
            bucket = _Bucket(tokens=capacity, updated=moment)
            self._buckets[key] = bucket
        else:
            elapsed = max(0.0, moment - bucket.updated)
            bucket.tokens = min(capacity, bucket.tokens + elapsed * per_second)
            bucket.updated = moment

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0

        # Ceiling, so a caller told "1 second" is not told it again a millisecond later.
        return max(1.0, (1.0 - bucket.tokens) / per_second)

    def _evict(self, now: float) -> None:
        """Make room by dropping the buckets with the least left to say.

        Eviction order is *fullness*, never age. A full bucket carries no information —
        forgetting it and re-creating it full are the same thing — while a nearly-empty one
        is the record of the client the limiter is currently holding back. Evicting by age
        would look reasonable and would hand a fresh allowance to whichever client is
        hitting hardest, because the loudest client's bucket is touched constantly and the
        quiet ones age out around it. That inverts the whole point.
        """
        fill: list[tuple[float, tuple[str, str]]] = []
        for key, bucket in self._buckets.items():
            capacity = float(
                next((lane.per_minute for lane in self.lanes if lane.name == key[0]), 0)
            )
            if capacity <= 0:
                fill.append((1.0, key))
                continue
            elapsed = max(0.0, now - bucket.updated)
            projected = min(capacity, bucket.tokens + elapsed * (capacity / 60.0))
            fill.append((projected / capacity, key))

        for fraction, key in fill:
            if fraction >= 1.0 - _FULL_EPSILON:
                del self._buckets[key]

        if len(self._buckets) < self.max_clients:
            return

        # Pathological: every tracked client is actively spending. Drop the fullest
        # remaining buckets — the ones nearest to being unlimited anyway — down to 90% of
        # the ceiling, so the table has room to accept new clients without thrashing.
        target = max(1, (self.max_clients * 9) // 10)
        remaining = sorted(
            (entry for entry in fill if entry[1] in self._buckets),
            key=lambda entry: entry[0],
            reverse=True,
        )
        for _, key in remaining:
            if len(self._buckets) <= target:
                break
            del self._buckets[key]

    @property
    def tracked(self) -> int:
        return len(self._buckets)


class RateLimitMiddleware:
    """Refuses a flood before any work happens, in the error taxonomy (SEC-9)."""

    def __init__(self, app: ASGIApp, *, per_minute: int = 30, ip_header: str = ""):
        self.app = app
        self.ip_header = ip_header
        self.limiter = RateLimiter(lanes_for(per_minute))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "/")
        lane = lane_for(str(scope.get("method") or "GET").upper(), path, self.limiter.lanes)
        if lane.unlimited:
            await self.app(scope, receive, send)
            return

        client = client_key(scope, self.ip_header)
        wait = self.limiter.check(lane, client)
        if wait <= 0.0:
            await self.app(scope, receive, send)
            return

        retry_after = int(wait) if wait == int(wait) else int(wait) + 1
        # A rejected request never reaches the app factory's request-context middleware, so
        # it would otherwise carry no correlation ID at all. The ID an agent reads off the
        # screen has to be the ID in the log even when the answer is "not right now".
        request_id = applog.new_request_id()
        applog.warn(
            "rate_limited",
            kind="user",
            code="too_many_requests",
            stage=lane.name,
            status=429,
        )
        await send_error(
            send,
            too_many_requests(),
            status=429,
            extra_headers={
                "Retry-After": str(retry_after),
                "X-Request-ID": request_id,
                "Cache-Control": "no-store",
            },
        )


__all__ = [
    "BATCH_READ_PER_MINUTE",
    "BATCH_SUBMIT_PER_MINUTE",
    "DEFAULT_PER_MINUTE",
    "Lane",
    "RateLimitMiddleware",
    "RateLimiter",
    "lane_for",
    "lanes_for",
    "normalise_path",
    "too_many_requests",
]
