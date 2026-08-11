"""CONTRACT: the suite runs with no network egress, and fails if anything opens a socket (ENG-3).

"CI has no live calls" was true of this project and unenforced. It stayed true by
discipline — every provider is a fake, every test injects one — and discipline is not a
guarantee. One `import requests` in a dependency, one test that reaches for a real client
because the fake was inconvenient, and the suite becomes flaky on a plane and expensive in
CI, with no signal until the bill or the outage.

`tests/conftest.py` blocks every socket operation for the whole session. This file proves
the guard is *armed* rather than merely installed — a guard nobody tests is a guard that
gets refactored into a no-op, which is the second time this codebase would have shipped
one (the SPA fallback that could never fire).

It also proves the guard does not block the things the suite legitimately does: driving
the ASGI app in-process, reading fixtures off disk, and using SQLite.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.config import Config
from api.main import create_app

pytestmark = pytest.mark.contract


# --------------------------------------------------------------------------------------
# The guard is armed
# --------------------------------------------------------------------------------------


def test_opening_a_socket_to_the_outside_world_fails() -> None:
    """The guard, exercised directly. If this passes silently, everything below is theatre."""
    from conftest import NetworkAccessError

    with pytest.raises(NetworkAccessError):
        socket.create_connection(("api.anthropic.com", 443), timeout=1)


def test_connecting_an_already_constructed_socket_fails() -> None:
    """`socket()` then `.connect()` is the lower-level path a client library takes."""
    from conftest import NetworkAccessError

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessError):
            sock.connect(("api.anthropic.com", 443))
    finally:
        sock.close()


def test_resolving_a_hostname_fails() -> None:
    """DNS is blocked too, so a leak fails before it reaches the network.

    A connect that blocked only at TCP would still have leaked the hostname to a
    resolver — and in a corporate network, to a log.
    """
    from conftest import NetworkAccessError

    with pytest.raises(NetworkAccessError):
        socket.getaddrinfo("api.anthropic.com", 443)


def test_even_a_loopback_connection_is_blocked() -> None:
    """No allowlist. Nothing in this suite needs a socket, so nothing gets one.

    A loopback exception is the hole a future "just start a real server for this one
    test" walks through, and the value of the guard is that it has no holes.
    """
    from conftest import NetworkAccessError

    with pytest.raises(NetworkAccessError):
        socket.create_connection(("127.0.0.1", 9), timeout=1)


def test_an_http_client_cannot_reach_the_network() -> None:
    """The layer a leak would actually come through.

    `httpx` is a declared dependency and is what the Anthropic SDK uses. Blocking at
    the socket module is what makes this hold for any client, including one added by a
    dependency upgrade nobody read.
    """
    import httpx
    from conftest import NetworkAccessError

    with pytest.raises((NetworkAccessError, httpx.ConnectError, httpx.TransportError)):
        httpx.get("https://api.anthropic.com/v1/messages", timeout=1)


def test_the_guard_records_what_it_blocked() -> None:
    """A blocked attempt is evidence, not just a failure.

    The message has to name what was reached for, or the developer who hits it sees
    "ConnectionRefused" and starts debugging their network.
    """
    from conftest import BLOCKED_CONNECTIONS, NetworkAccessError

    before = len(BLOCKED_CONNECTIONS)
    with pytest.raises(NetworkAccessError) as raised:
        socket.create_connection(("example.invalid", 80), timeout=1)

    assert len(BLOCKED_CONNECTIONS) > before
    message = str(raised.value)
    assert "no network egress" in message
    assert "example.invalid" in message
    assert "api/provider/fake.py" in message


# --------------------------------------------------------------------------------------
# The guard does not block what the suite legitimately does
# --------------------------------------------------------------------------------------


def test_the_asgi_app_still_serves_over_the_test_client() -> None:
    """`TestClient` drives the app in-process, so the full HTTP stack still runs.

    This is what makes the guard affordable: routing, middleware, validation, error
    handlers and serialization are all exercised, and none of it needs a socket.
    """
    client = TestClient(create_app(config=Config(use_fake_provider=True), provider=None))
    assert client.get("/health").json() == {"status": "ok"}


def test_reading_fixtures_from_disk_still_works() -> None:
    from pathlib import Path

    labels = Path(__file__).resolve().parents[2] / "fixtures" / "labels"
    assert (labels / "tc01_old_tom_clean.png").read_bytes()[:4] == b"\x89PNG"


def test_sqlite_still_works(tmp_path: Any) -> None:
    """The batch store is SQLite on disk. Blocking sockets must not block it."""
    import sqlite3

    connection = sqlite3.connect(tmp_path / "probe.db")
    connection.execute("create table t (x int)")
    connection.execute("insert into t values (1)")
    assert connection.execute("select x from t").fetchone() == (1,)
    connection.close()


# --------------------------------------------------------------------------------------
# Nothing in the suite is configured to go live
# --------------------------------------------------------------------------------------


def test_the_live_adapter_refuses_to_build_a_real_client_without_a_key() -> None:
    """The one construction that could reach the network, and what stops it.

    `AnthropicVisionProvider(config)` with no injected `client` builds a real
    `anthropic.Anthropic`. It does not connect at construction — but it is one method
    call away, and the socket guard would only catch it at the moment of the call, deep
    inside a test that looked like it was testing something else.

    The key check is what makes that unreachable in this suite: no key in the
    environment (asserted below) means the constructor raises before an SDK client
    exists at all. So the two tests are one guard read from both ends.
    """
    from api.config import ConfigError
    from api.provider.anthropic_adapter import AnthropicVisionProvider

    with pytest.raises(ConfigError):
        AnthropicVisionProvider(Config(anthropic_api_key=""))


def test_no_test_module_hard_codes_a_plausible_api_key() -> None:
    """A key literal in a test file is a key that reaches CI logs and git history.

    Test doubles need no credential — every adapter test injects a client instead. A
    string that looks like a key is either a real one somebody pasted or a fake one
    teaching the next person to paste a real one.
    """
    from pathlib import Path

    # Assembled at runtime so this file does not contain the literal it searches for.
    marker = "sk-" + "ant-"
    tests = Path(__file__).resolve().parents[1]
    offenders = [
        f"{module.relative_to(tests)}:{number}"
        for module in tests.rglob("test_*.py")
        for number, line in enumerate(module.read_text().splitlines(), start=1)
        if marker in line
    ]
    assert offenders == [], offenders


def test_no_real_api_key_is_present_in_the_environment() -> None:
    """A key in the environment is how an accidental live call becomes an expensive one.

    Blocked sockets make it harmless, but a key present at all means the suite is one
    guard-removal away from spending money — and it means somebody's real key is in the
    process that runs untrusted test code.
    """
    import os

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    assert not key.startswith("sk-" + "ant-"), "a real API key is set while the suite runs"
