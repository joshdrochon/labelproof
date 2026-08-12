"""CONTRACT: the suite runs with no network egress, and every escape route is closed (ENG-3).

"CI has no live calls" was true of this project and unenforced. It stayed true by
discipline — every provider is a fake, every test injects one — and discipline is not a
guarantee.

**This file exists because the first guard leaked, and its own tests said it did not.**
Two branches wrote guards independently; both patched `connect`, `connect_ex` and
`getaddrinfo`; both asserted in their docstrings that they blocked everything. A reviewer
sent real traffic through both:

* `socket(AF_INET, SOCK_DGRAM).sendto(b"probe", ("8.8.8.8", 53))` — **5 bytes left the
  machine.** A datagram needs no `connect`.
* `socket.gethostbyname("example.com")` — **resolved, live.** It does not route through
  `getaddrinfo`.
* `gethostbyname_ex` / `gethostbyaddr` — **live answers.**
* a module-level `create_connection(...)` at the top of a test file — **escaped**, because
  a session-scoped fixture is installed after collection.

The old test file had one DNS test, and it exercised `getaddrinfo` — the single resolver
path that *was* patched. That is the shape of a test that cannot fail: it probes exactly
the thing the implementation happens to do.

So every test below sends a real packet's worth of intent at a *different* verb, and each
one names what escaped through it before. The list is the coverage claim: if a verb is not
in this file, the guard is not proven to cover it.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Any

import pytest
from conftest import NetworkAccessDenied
from fastapi.testclient import TestClient

from api.config import Config
from api.main import create_app

pytestmark = pytest.mark.contract

#: A destination that is real, routable, and answers — so a test that passes because the
#: guard worked cannot be confused with one that passes because the network was down.
_REAL_RESOLVER = ("8.8.8.8", 53)
_REAL_HOST = "example.com"


# --------------------------------------------------------------------------------------
# Every egress verb, one test each
# --------------------------------------------------------------------------------------


def test_connecting_a_tcp_socket_is_refused() -> None:
    """The verb everybody patches first."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessDenied):
            sock.connect(("api.anthropic.com", 443))
    finally:
        sock.close()


def test_connect_ex_is_refused() -> None:
    """The non-raising sibling. A guard that patched only `connect` would miss it —
    `connect_ex` returns an error number instead of raising, so a caller using it would
    have seen a plain refusal and retried."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessDenied):
            sock.connect_ex(("api.anthropic.com", 443))
    finally:
        sock.close()


def test_create_connection_is_refused() -> None:
    """The convenience wrapper most client libraries reach for."""
    with pytest.raises(NetworkAccessDenied):
        socket.create_connection(("api.anthropic.com", 443), timeout=1)


def test_sending_a_datagram_is_refused() -> None:
    """REGRESSION: this leaked. Five bytes reached 8.8.8.8:53 with the guard armed.

    A UDP `sendto` needs no `connect`, so a guard built around connection setup does not
    see it at all. It is not a hypothetical path — it is how a DNS query goes out.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(NetworkAccessDenied):
            sock.sendto(b"probe", _REAL_RESOLVER)
    finally:
        sock.close()


def test_sending_a_message_is_refused() -> None:
    """`sendmsg` is the scatter-gather form of the same hole."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(NetworkAccessDenied):
            sock.sendmsg([b"probe"], [], 0, _REAL_RESOLVER)
    finally:
        sock.close()


def test_getaddrinfo_is_refused() -> None:
    """The one resolver path the old guard covered. Kept, but no longer the only one."""
    with pytest.raises(NetworkAccessDenied):
        socket.getaddrinfo("api.anthropic.com", 443)


def test_gethostbyname_is_refused() -> None:
    """REGRESSION: this leaked. `example.com` resolved to a live address.

    `gethostbyname` calls into the resolver directly and never touches `getaddrinfo`,
    so the old guard's DNS claim was false for the older of the two APIs — the one a
    naive `socket.gethostbyname(host)` in a dependency would use.
    """
    with pytest.raises(NetworkAccessDenied):
        socket.gethostbyname(_REAL_HOST)


def test_gethostbyname_ex_is_refused() -> None:
    """REGRESSION: this leaked, with a live answer."""
    with pytest.raises(NetworkAccessDenied):
        socket.gethostbyname_ex(_REAL_HOST)


def test_gethostbyaddr_is_refused() -> None:
    """REGRESSION: this leaked. Reverse resolution is egress too, and it leaks an address."""
    with pytest.raises(NetworkAccessDenied):
        socket.gethostbyaddr("8.8.8.8")


def test_an_http_client_cannot_reach_the_network() -> None:
    """The layer a leak would actually come through.

    `httpx` is a declared dependency and is what the Anthropic SDK uses. Guarding the
    `socket` module is what makes this hold for any client, including one added by a
    dependency upgrade nobody read.
    """
    import httpx

    with pytest.raises((NetworkAccessDenied, httpx.ConnectError, httpx.TransportError)):
        httpx.get("https://api.anthropic.com/v1/messages", timeout=1)


# --------------------------------------------------------------------------------------
# The refusal is legible
# --------------------------------------------------------------------------------------


def test_the_refusal_names_the_address_and_the_way_out() -> None:
    """A blocked attempt is evidence, not just a failure.

    Without the address in the message, a developer who hits this sees a bare exception
    and starts debugging their own network. Without the escape hatch named, they patch
    the guard.
    """
    with pytest.raises(NetworkAccessDenied) as raised:
        socket.create_connection(("example.invalid", 80), timeout=1)

    message = str(raised.value)
    assert "example.invalid" in message
    assert "offline by design" in message
    assert "allow_network" in message


def test_the_refusal_names_the_test_that_caused_it() -> None:
    """Inside a test, the message carries the node id.

    A CI log that says "something tried to reach the network" is a hunt across a
    thousand tests; one that names the node id is a fix.
    """
    with pytest.raises(NetworkAccessDenied) as raised:
        socket.create_connection(("example.invalid", 80), timeout=1)
    assert "test_the_refusal_names_the_test_that_caused_it" in str(raised.value)


# --------------------------------------------------------------------------------------
# The guard is live before any test runs
# --------------------------------------------------------------------------------------

#: Evaluated at import of this module — i.e. during collection, before any fixture has
#: run. The old guard was a session-scoped fixture and was therefore installed *after*
#: this point, so a module-level call like this one escaped entirely.
try:
    socket.create_connection((_REAL_HOST, 80), timeout=1)
except NetworkAccessDenied:
    _GUARDED_AT_IMPORT = True
except OSError:  # pragma: no cover - only on a machine with no route out at all
    _GUARDED_AT_IMPORT = False


def test_the_guard_is_installed_before_collection() -> None:
    """REGRESSION: module-level network calls escaped the old session-scoped fixture.

    The probe above runs while this file is being imported. If the guard were installed
    by a fixture, it would not exist yet and the call would go out — which is exactly
    what a `requests.get(...)` at the top of a test file does.
    """
    assert _GUARDED_AT_IMPORT, (
        "the guard was not active during collection; a module-level network call escaped"
    )


# --------------------------------------------------------------------------------------
# The guard does not block what the suite legitimately does
# --------------------------------------------------------------------------------------


def test_loopback_is_permitted_deliberately() -> None:
    """An in-process server on 127.0.0.1 is not egress, and the guard says so.

    This is a documented allowance rather than an oversight: some libraries bind a
    loopback socket to coordinate threads, and refusing that would break them without
    preventing a single byte from leaving the machine. Asserted so the allowance is
    visible and deliberate rather than discovered later and mistaken for a hole.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(listener.getsockname())
        finally:
            client.close()
    finally:
        listener.close()


def test_unix_sockets_are_permitted() -> None:
    """Local IPC is not egress. `socketpair` is how a lot of test tooling talks to itself."""
    left, right = socket.socketpair()
    try:
        left.send(b"ping")
        assert right.recv(4) == b"ping"
    finally:
        left.close()
        right.close()


def test_the_asgi_app_still_serves_over_the_test_client() -> None:
    """`TestClient` drives the app in-process, so the full HTTP stack still runs.

    This is what makes the guard affordable: routing, middleware, validation, error
    handlers and serialization are all exercised, and none of it needs egress.
    """
    client = TestClient(create_app(config=Config(use_fake_provider=True), provider=None))
    assert client.get("/health").json() == {"status": "ok"}


def test_reading_fixtures_from_disk_still_works() -> None:
    labels = Path(__file__).resolve().parents[2] / "fixtures" / "labels"
    assert (labels / "tc01_old_tom_clean.png").read_bytes()[:4] == b"\x89PNG"


def test_sqlite_still_works(tmp_path: Any) -> None:
    """The batch store is SQLite on disk. Blocking egress must not block it."""
    import sqlite3

    connection = sqlite3.connect(tmp_path / "probe.db")
    connection.execute("create table t (x int)")
    connection.execute("insert into t values (1)")
    assert connection.execute("select x from t").fetchone() == (1,)
    connection.close()


# --------------------------------------------------------------------------------------
# The opt-out exists, is narrow, and is unused
# --------------------------------------------------------------------------------------


@pytest.mark.allow_network
def test_the_opt_out_marker_actually_opts_out() -> None:
    """The escape hatch works, so a future genuine need has a supported route.

    Proven against a refusal rather than against a real connection: what is being
    asserted is that the marker lifts the policy, not that this machine has a route out.
    """
    from conftest import _policy

    assert _policy.allowed is True


def test_no_other_test_in_the_suite_opts_out_of_the_guard() -> None:
    """Two marked tests, and each one is a proof that the marker itself works.

    An opt-out that spreads is a guard that has been turned off a test at a time. If
    this list ever grows, the addition needs an argument in the judgment log.

    The second entry arrived with the `wave/ci` merge. Both branches wrote a probe suite
    for the offline guard, and neither is a subset of the other — `test_no_network.py`
    covers `sendto` with flags, a connected UDP socket, the two lookups that are still
    *allowed*, and egress at import and from a session fixture (each in a subprocess);
    this file covers an HTTP client, sqlite, the ASGI test client, and the API-key
    checks. So both are kept, and each marks exactly one test: the one asserting that
    `@pytest.mark.allow_network` lifts the policy at all. That is the only justified use
    of the marker, and it is still the only use in the suite. Consolidating the two
    probe suites into one file is worth doing; losing half the probes to do it is not.
    """
    tests = Path(__file__).resolve().parents[1]
    marked = sorted(
        str(module.relative_to(tests))
        for module in tests.rglob("test_*.py")
        for line in module.read_text().splitlines()
        if line.strip() == "@pytest.mark.allow_network"
    )
    # By file, not by line number: pinning the line makes an unrelated edit above it a
    # failure, and a test that cries wolf about its own line numbers gets deleted.
    assert marked == ["contract/test_offline.py", "test_no_network.py"], marked


# --------------------------------------------------------------------------------------
# Nothing in the suite is configured to go live
# --------------------------------------------------------------------------------------


def test_the_live_adapter_refuses_to_build_a_real_client_without_a_key() -> None:
    """The one construction that could reach the network, and what stops it.

    `AnthropicVisionProvider(config)` with no injected `client` builds a real
    `anthropic.Anthropic`. It does not connect at construction — but it is one method
    call away, and the guard would only catch it at the moment of the call, deep inside
    a test that looked like it was testing something else.
    """
    from api.config import ConfigError
    from api.provider.anthropic_adapter import AnthropicVisionProvider

    with pytest.raises(ConfigError):
        AnthropicVisionProvider(Config(anthropic_api_key=""))


#: A key that could actually authenticate: the prefix plus enough trailing material to
#: be real. Assembled at runtime so this file does not contain the literal it hunts for.
#:
#: The length bound is the whole point. Matching the bare prefix flags `sk-ant-test`,
#: which several suites use as an obviously-inert placeholder in `monkeypatch.setenv` —
#: and a check that fires on every placeholder gets an exemption list, then gets
#: disabled. What must never appear is a string long enough to be a credential.
_KEY_PATTERN = re.compile("sk-" + r"ant-[A-Za-z0-9_-]{24,}")


def test_no_test_module_hard_codes_a_usable_api_key() -> None:
    """A key literal in a test file is a key that reaches CI logs and git history.

    Test doubles need no credential — every adapter test injects a client instead. A
    string long enough to authenticate is either one somebody pasted or a fake one
    teaching the next person to paste a real one.
    """
    tests = Path(__file__).resolve().parents[1]
    offenders = [
        f"{module.relative_to(tests)}:{number}"
        for module in tests.rglob("test_*.py")
        for number, line in enumerate(module.read_text().splitlines(), start=1)
        if _KEY_PATTERN.search(line)
    ]
    assert offenders == [], offenders


def test_no_usable_api_key_is_present_in_the_environment() -> None:
    """A key in the environment is how an accidental live call becomes an expensive one.

    A blocked guard makes it harmless, but a key present at all means the suite is one
    guard-removal away from spending money — and it means somebody's real key is in the
    process that runs untrusted test code.
    """
    import os

    assert not _KEY_PATTERN.match(os.environ.get("ANTHROPIC_API_KEY", "")), (
        "a usable API key is set while the suite runs"
    )


def test_the_key_check_would_catch_a_real_key() -> None:
    """The pattern has teeth.

    A length-bounded check is one edit away from being a check that matches nothing.
    Both directions are asserted here so that loosening it to accept placeholders
    cannot quietly loosen it to accept credentials.
    """
    assert _KEY_PATTERN.search("sk-" + "ant-api03-" + "A" * 90)
    assert not _KEY_PATTERN.search("sk-" + "ant-test")
