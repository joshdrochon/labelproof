"""Rate limiting on the public prototype URL (SEC-9, LP-081, LP-255).

Two things are being proved, and the second is the one that matters:

1. A burst is refused with 429 and a body an agent can read, not a framework default.
2. The limiter cannot break the demo. Health checks are never limited, the sample flow has
   headroom a human cannot exhaust, and — the PRD §225 case — saturating the batch progress
   poller leaves Verify Now's budget completely untouched.

The clock is driven, never slept on. A rate-limit suite that sleeps is slow and flaky, and
`RateLimiter.check` takes `now` precisely so this one is neither (LP-247).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.config import Config
from api.middleware.ratelimit import (
    BATCH_READ_PER_MINUTE,
    BATCH_SAMPLE_PER_MINUTE,
    BATCH_SUBMIT_PER_MINUTE,
    RateLimiter,
    lane_for,
    lanes_for,
)
from api.security import SecurityPolicy, harden

LANES = lanes_for(30)


# --- lane routing ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/health", "exempt"),
        ("GET", "/ready", "exempt"),
        ("POST", "/verify", "verify"),
        ("POST", "/batch", "batch_submit"),
        ("POST", "/batch/job_abc/retry", "batch_submit"),
        # Its own lane. One click starts five real verifications from an EMPTY body, which
        # nothing else in this product does — every other route to the model needs the
        # caller to supply artwork first. Two a minute is still a demo; the bill is bounded
        # by the hourly ceiling in the route, which a per-client lane cannot do.
        ("POST", "/batch/sample", "batch_sample"),
        # The GET is an ordinary read that 404s. Only the POST spends anything.
        ("GET", "/batch/sample", "batch_read"),
        ("GET", "/batch/job_abc", "batch_read"),
        ("GET", "/batch/job_abc/export.csv", "batch_read"),
        # Artwork for the evidence overlay. One item view fetches an image per photograph
        # on top of the polling the status endpoint is already doing, so it has to sit in
        # the generous read lane rather than anywhere near the verification budget.
        ("GET", "/batch/job_abc/items/itm_abc/images/0", "batch_read"),
        # A write, and still the read lane: recording a decision makes no model call. It is
        # a click at click frequency, and the submit lane's ten a minute would rate-limit an
        # agent working briskly down a 300-row queue against themselves.
        ("PATCH", "/batch/job_abc/items/itm_abc/decisions", "batch_read"),
        ("GET", "/batch/manifest-template.csv", "batch_read"),
        ("GET", "/sample", "default"),
        ("GET", "/", "default"),
        ("GET", "/assets/index-abc123.js", "default"),
    ],
)
def test_each_path_draws_on_the_budget_it_should(method: str, path: str, expected: str) -> None:
    assert lane_for(method, path, LANES).name == expected


def test_the_sample_batch_gets_the_tightest_lane_in_the_table() -> None:
    """It is the only endpoint that spends money on an empty body, so it is priced hardest.

    Asserted as an ordering rather than as the number 2, because what has to stay true is
    that nobody widens it back to the submit lane's budget while reading this as a typo.
    """
    lane = lane_for("POST", "/batch/sample", LANES)
    assert lane.name == "batch_sample"
    assert lane.per_minute == BATCH_SAMPLE_PER_MINUTE
    assert lane.per_minute < BATCH_SUBMIT_PER_MINUTE < BATCH_READ_PER_MINUTE


def test_the_sample_lane_cannot_spend_the_submit_budget() -> None:
    """Separate buckets, so a reviewer clicking the sample cannot lock themselves out of
    uploading a real batch, and a script hammering the sample cannot buy more of it by
    exhausting a lane it does not draw on."""
    limiter = RateLimiter(LANES)
    sample = lane_for("POST", "/batch/sample", LANES)
    submit = lane_for("POST", "/batch", LANES)

    for _ in range(BATCH_SAMPLE_PER_MINUTE + 5):
        limiter.check(sample, "1.2.3.4", now=100.0)

    assert limiter.check(sample, "1.2.3.4", now=100.0) > 0.0
    assert limiter.check(submit, "1.2.3.4", now=100.0) == 0.0


def test_health_checks_are_never_limited() -> None:
    """A rate-limited /health lets the limiter take the machine out of rotation itself."""
    assert lane_for("GET", "/health", LANES).unlimited
    assert lane_for("GET", "/ready", LANES).unlimited


def test_verify_budget_comes_from_config_not_a_constant() -> None:
    assert next(lane.per_minute for lane in lanes_for(30) if lane.name == "verify") == 30
    assert next(lane.per_minute for lane in lanes_for(90) if lane.name == "verify") == 90


# --- the bucket ------------------------------------------------------------------------


def test_a_full_minutes_worth_passes_without_waiting() -> None:
    """The demo case: the first 30 requests of a cold session never meet the limiter."""
    limiter = RateLimiter(LANES)
    lane = lane_for("POST", "/verify", LANES)
    assert all(limiter.check(lane, "1.2.3.4", now=100.0) == 0.0 for _ in range(30))


def test_the_thirty_first_request_in_the_same_instant_is_refused() -> None:
    limiter = RateLimiter(LANES)
    lane = lane_for("POST", "/verify", LANES)
    for _ in range(30):
        limiter.check(lane, "1.2.3.4", now=100.0)
    assert limiter.check(lane, "1.2.3.4", now=100.0) > 0.0


def test_the_bucket_refills_over_time() -> None:
    limiter = RateLimiter(LANES)
    lane = lane_for("POST", "/verify", LANES)
    for _ in range(30):
        limiter.check(lane, "1.2.3.4", now=100.0)
    assert limiter.check(lane, "1.2.3.4", now=100.0) > 0.0
    # 30/min is one token every two seconds.
    assert limiter.check(lane, "1.2.3.4", now=102.5) == 0.0


def test_the_bucket_never_refills_past_capacity() -> None:
    """An idle hour must not buy an hour's worth of burst."""
    limiter = RateLimiter(LANES)
    lane = lane_for("POST", "/verify", LANES)
    limiter.check(lane, "1.2.3.4", now=100.0)
    allowed = sum(1 for _ in range(200) if limiter.check(lane, "1.2.3.4", now=4000.0) == 0.0)
    assert allowed == 30


def test_clients_do_not_share_a_bucket() -> None:
    limiter = RateLimiter(LANES)
    lane = lane_for("POST", "/verify", LANES)
    for _ in range(30):
        limiter.check(lane, "1.2.3.4", now=100.0)
    assert limiter.check(lane, "1.2.3.4", now=100.0) > 0.0
    assert limiter.check(lane, "5.6.7.8", now=100.0) == 0.0


def test_a_saturated_batch_poller_leaves_verify_untouched() -> None:
    """PRD §225 at the transport layer.

    One shared budget would have the progress poller spend the agent's verification
    allowance during a 300-item job, so Verify Now would 429 while a batch ran — false
    before the priority lane ever got a say.
    """
    limiter = RateLimiter(LANES)
    poll = lane_for("GET", "/batch/job_abc", LANES)
    verify = lane_for("POST", "/verify", LANES)

    for _ in range(BATCH_READ_PER_MINUTE):
        limiter.check(poll, "1.2.3.4", now=100.0)
    assert limiter.check(poll, "1.2.3.4", now=100.0) > 0.0, "poller should now be limited"

    assert all(limiter.check(verify, "1.2.3.4", now=100.0) == 0.0 for _ in range(30))


def test_a_saturated_verify_budget_leaves_batch_submission_alone() -> None:
    limiter = RateLimiter(LANES)
    verify = lane_for("POST", "/verify", LANES)
    submit = lane_for("POST", "/batch", LANES)
    for _ in range(31):
        limiter.check(verify, "1.2.3.4", now=100.0)
    allowed = [limiter.check(submit, "1.2.3.4", now=100.0) for _ in range(BATCH_SUBMIT_PER_MINUTE)]
    assert all(wait == 0.0 for wait in allowed)


def test_the_retry_hint_is_long_enough_to_be_worth_waiting() -> None:
    """A hint that is too short sends a client straight back into another 429.

    The previous version asserted `>= 1.0`, which restates `max(1.0, ...)` in the
    implementation and passes for a hardcoded constant. This asserts the property that
    matters: waiting the advertised time actually gets you served.
    """
    limiter = RateLimiter(LANES)
    lane = lane_for("POST", "/verify", LANES)
    for _ in range(30):
        limiter.check(lane, "1.2.3.4", now=100.0)

    wait = limiter.check(lane, "1.2.3.4", now=100.0)
    assert wait > 0.0

    # A hair before the advertised moment: still refused.
    assert limiter.check(lane, "1.2.3.4", now=100.0 + wait - 0.01) > 0.0
    # At it: served. A hardcoded `Retry-After` fails both halves.
    assert limiter.check(lane, "1.2.3.4", now=100.0 + wait) == 0.0


def test_bucket_table_does_not_grow_without_bound() -> None:
    """A spoofable client key must not be a memory exhaustion primitive."""
    limiter = RateLimiter(LANES, max_clients=50)
    lane = lane_for("GET", "/", LANES)
    for index in range(500):
        limiter.check(lane, f"10.0.0.{index}", now=100.0 + index)
    assert limiter.tracked <= 50


def test_evicted_clients_are_only_the_ones_that_had_refilled() -> None:
    """Eviction must never hand a limit back to a client that is actively spending."""
    limiter = RateLimiter(LANES, max_clients=4)
    lane = lane_for("POST", "/verify", LANES)
    for _ in range(30):
        limiter.check(lane, "attacker", now=100.0)
    for index in range(20):
        limiter.check(lane, f"passer-by-{index}", now=100.0)
    assert limiter.check(lane, "attacker", now=100.0) > 0.0


# --- through the real HTTP stack -------------------------------------------------------


def _hardened(**policy_overrides: Any) -> tuple[FastAPI, SecurityPolicy]:
    """A minimal app with the real security stack, and routes that do nothing expensive.

    Deliberately not `create_app`: the limiter is transport-layer, so a real verification
    would only add seconds and a fixture dependency to a test about counting requests.
    """
    app = FastAPI()

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/sample")
    def sample() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.post("/verify")
    def verify() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/batch/{job_id}")
    def batch(job_id: str) -> JSONResponse:
        return JSONResponse({"ok": True})

    config = Config(use_fake_provider=True, **policy_overrides)
    policy = harden(app, config)
    return app, policy


@pytest.fixture(autouse=True)
def _restore_containment() -> Any:
    """`harden` installs process-wide traceback containment; put it back afterwards."""
    from api import security

    yield
    security.remove_log_containment()


def test_a_burst_gets_429_with_a_sentence_not_a_status_line() -> None:
    app, _ = _hardened(rate_limit_per_minute=3)
    client = TestClient(app)

    statuses = [client.post("/verify").status_code for _ in range(6)]
    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses

    refused = client.post("/verify")
    assert refused.status_code == 429
    body = refused.json()
    assert body["error"]["kind"] == "user"
    assert body["error"]["code"] == "too_many_requests"
    assert body["error"]["next_step"] == "retry"
    # Plain language, in the agents' vocabulary — no jargon, no framework wording (UX-6).
    message = body["error"]["message"]
    assert "nothing has been checked" in message.lower()
    assert "429" not in message


def test_a_429_carries_retry_after_and_a_request_id() -> None:
    app, _ = _hardened(rate_limit_per_minute=1)
    client = TestClient(app)
    client.post("/verify")
    refused = client.post("/verify")
    assert refused.status_code == 429
    # Not `>= 1` — that restates `max(1.0, ...)` and passes for a hardcoded 9999. At one
    # request per minute the bucket refills in exactly 60s, so the hint has to be near it.
    assert 30 <= int(refused.headers["retry-after"]) <= 60
    assert refused.headers["x-request-id"].startswith("req_")
    assert refused.headers["cache-control"] == "no-store"


def test_a_429_is_as_hardened_as_a_200() -> None:
    """The error paths are what an attacker reads. They get the same headers."""
    app, _ = _hardened(rate_limit_per_minute=1)
    client = TestClient(app)
    client.post("/verify")
    refused = client.post("/verify")
    assert "content-security-policy" in refused.headers
    assert refused.headers["x-content-type-options"] == "nosniff"


def test_health_survives_a_flood_that_would_have_limited_verify() -> None:
    app, _ = _hardened(rate_limit_per_minute=1)
    client = TestClient(app)
    assert all(client.get("/health").status_code == 200 for _ in range(200))


def test_the_sample_demo_is_never_throttled_by_a_human() -> None:
    """A grader clicking 'Try a sample' repeatedly must not be able to break the demo."""
    app, _ = _hardened(rate_limit_per_minute=30)
    client = TestClient(app)
    assert all(client.get("/sample").status_code == 200 for _ in range(60))


def test_verify_still_works_while_the_batch_poller_is_being_limited() -> None:
    """PRD §225, end to end through the stack rather than against the bucket."""
    app, _ = _hardened(rate_limit_per_minute=30)
    client = TestClient(app)

    seen = {client.get("/batch/job_abc").status_code for _ in range(BATCH_READ_PER_MINUTE + 20)}
    assert 429 in seen, "the poller lane should be exhaustible"

    assert all(client.post("/verify").status_code == 200 for _ in range(30))


def test_the_default_lane_is_generous_enough_for_a_page_load() -> None:
    """Was `assert DEFAULT_PER_MINUTE >= 100` against a constant of 600 — a test that could
    not fail. This drives an SPA cold load and several reloads instead."""
    app, _ = _hardened(rate_limit_per_minute=30)
    client = TestClient(app)

    # index.html plus the hashed bundle and stylesheet, five times over, which is more
    # reloading than any grader does.
    refused = 0
    for _ in range(5):
        for path in ("/", "/assets/index.js", "/assets/index.css", "/sample"):
            if client.get(path).status_code == 429:
                refused += 1
    assert refused == 0


def test_a_client_supplied_header_is_ignored_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default must fail CLOSED, and it did not.

    The first version defaulted to trusting `Fly-Client-IP`, reasoning that on Fly the
    socket peer is the proxy. Off Fly that header is client-supplied, so rotating it bought
    an unlimited number of buckets: measured at 200 requests against a 3/min limit — 200
    allowed, 0 refused, with nothing in the logs to say the limiter had stopped working.
    A control that fails open silently is not a control.
    """
    monkeypatch.delenv("LABELPROOF_CLIENT_IP_HEADER", raising=False)
    assert SecurityPolicy.from_config(Config()).client_ip_header == ""

    app, policy = _hardened(rate_limit_per_minute=3)
    assert policy.client_ip_header == ""
    client = TestClient(app)

    statuses = [
        client.post("/verify", headers={"Fly-Client-IP": f"203.0.113.{n}"}).status_code
        for n in range(1, 40)
    ]
    assert 429 in statuses, "rotating a header must not buy a fresh budget by default"

    ipv6 = [
        client.post("/verify", headers={"Fly-Client-IP": f"2001:db8::{n}"}).status_code
        for n in range(1, 20)
    ]
    assert set(ipv6) == {429}, "already limited on the socket peer, header notwithstanding"


def test_a_trusted_header_separates_users_when_an_operator_opts_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Fly every request has the same socket peer, so the opt-in has to work too.

    Fly's proxy overwrites this header on every request, which is what makes trusting it
    sound *there* and nowhere else.
    """
    monkeypatch.setenv("LABELPROOF_CLIENT_IP_HEADER", "fly-client-ip")
    app, policy = _hardened(rate_limit_per_minute=2)
    assert policy.client_ip_header == "fly-client-ip"
    client = TestClient(app)

    for _ in range(3):
        client.post("/verify", headers={"Fly-Client-IP": "203.0.113.1"})
    blocked = client.post("/verify", headers={"Fly-Client-IP": "203.0.113.1"})
    assert blocked.status_code == 429

    other = client.post("/verify", headers={"Fly-Client-IP": "203.0.113.99"})
    assert other.status_code == 200, "a second agent must not inherit the first one's limit"


def test_trusting_a_header_is_announced_at_startup(
    monkeypatch: pytest.MonkeyPatch, capfd: Any
) -> None:
    """A README paragraph is not where an operator discovers their rate limiter is off."""
    from api import logging as applog

    monkeypatch.setenv("LABELPROOF_CLIENT_IP_HEADER", "x-forwarded-for")
    applog.configure()
    _hardened(rate_limit_per_minute=30)

    lines = [
        json.loads(line) for line in capfd.readouterr().out.splitlines() if line.startswith("{")
    ]
    warned = [line for line in lines if line.get("event") == "rate_limit_trusts_client_header"]
    assert warned, "trusting a header must be visible at boot"
    assert warned[-1]["code"] == "client_supplied_header"
    assert set(warned[-1]) <= applog.ALLOWED_FIELDS | {"ts"}


def test_a_proxy_overwritten_header_is_announced_differently(
    monkeypatch: pytest.MonkeyPatch, capfd: Any
) -> None:
    """`fly-client-ip` is sound behind Fly; `x-forwarded-for` never is. Say which."""
    from api import logging as applog

    monkeypatch.setenv("LABELPROOF_CLIENT_IP_HEADER", "fly-client-ip")
    applog.configure()
    _hardened(rate_limit_per_minute=30)

    lines = [
        json.loads(line) for line in capfd.readouterr().out.splitlines() if line.startswith("{")
    ]
    warned = [line for line in lines if line.get("event") == "rate_limit_trusts_client_header"]
    assert warned and warned[-1]["code"] == "proxy_overwritten_header"


def test_nothing_is_announced_when_the_safe_default_is_in_use(
    monkeypatch: pytest.MonkeyPatch, capfd: Any
) -> None:
    from api import logging as applog

    monkeypatch.delenv("LABELPROOF_CLIENT_IP_HEADER", raising=False)
    applog.configure()
    _hardened(rate_limit_per_minute=30)

    lines = [
        json.loads(line) for line in capfd.readouterr().out.splitlines() if line.startswith("{")
    ]
    assert not [
        line for line in lines if line.get("event") == "rate_limit_trusts_client_header"
    ]


@pytest.mark.parametrize(
    "path",
    ["//verify", "/./verify", "/VERIFY", "/batch/../verify", "/verify/", "//.//verify"],
)
def test_a_normalising_proxy_cannot_move_verify_into_the_cheap_lane(path: str) -> None:
    """All of these 404 in Starlette today, so this is not exploitable in the shipped app.

    It becomes exploitable the instant anything that normalises paths sits in front, and the
    consequence is that `/verify` — the expensive route, the one the 30/min budget exists
    for — draws on the 600/min default lane instead.
    """
    assert lane_for("POST", path, LANES).name == "verify"


def test_normalisation_does_not_move_anything_into_the_wrong_lane() -> None:
    assert lane_for("GET", "//health//", LANES).name == "exempt"
    assert lane_for("GET", "/batch/JOB_ABC/export.csv", LANES).name == "batch_read"
    assert lane_for("POST", "//batch", LANES).name == "batch_submit"
    # A path that merely starts with the same letters is not the verify route.
    assert lane_for("POST", "/verifysomething", LANES).name == "default"
    assert lane_for("GET", "/", LANES).name == "default"


def test_the_rate_limit_event_carries_no_content(capfd: Any) -> None:
    """The limiter logs through the allowlist like everything else (SEC-4)."""
    from api import logging as applog

    applog.configure()
    app, _ = _hardened(rate_limit_per_minute=1)
    client = TestClient(app)
    client.post("/verify")
    client.post("/verify")

    out = capfd.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    limited = [line for line in lines if line.get("event") == "rate_limited"]
    assert limited, "the refusal should be observable"
    assert all(set(line) <= applog.ALLOWED_FIELDS | {"ts"} for line in limited)
