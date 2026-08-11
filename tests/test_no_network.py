"""The offline guard has to actually refuse (ENG-3).

`tests/conftest.py` claims the suite cannot reach the network. That claim is only worth
something if it is tested, so these attempt real egress and assert it is refused before a
packet leaves — no timeouts, no waiting, and identical results on a machine with a route
to the internet and on a sandboxed CI runner without one.

The exception is matched by name rather than imported. pytest loads `conftest.py` as a
top-level module, so `from tests.conftest import NetworkAccessDenied` would produce a
second, unrelated class and every `pytest.raises` here would miss.
"""

from __future__ import annotations

import socket

import pytest


def denied(caught: pytest.ExceptionInfo[BaseException]) -> bool:
    return type(caught.value).__name__ == "NetworkAccessDenied"


@pytest.mark.parametrize(
    ("host", "port"),
    [("1.1.1.1", 443), ("93.184.216.34", 80), ("8.8.8.8", 53)],
    ids=["dns-resolver", "raw-ip", "udp-resolver"],
)
def test_connecting_to_an_outside_address_is_refused(host: str, port: int) -> None:
    with pytest.raises(RuntimeError) as caught:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))

    assert denied(caught)
    assert host in str(caught.value)


def test_connect_ex_is_guarded_too() -> None:
    """`connect_ex` returns an errno instead of raising, so it needs its own patch."""
    with pytest.raises(RuntimeError) as caught:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect_ex(("1.1.1.1", 443))

    assert denied(caught)


@pytest.mark.parametrize(
    "hostname",
    ["api.anthropic.com", "pypi.org", "registry.npmjs.org"],
)
def test_even_resolving_a_hostname_is_refused(hostname: str) -> None:
    """A DNS query is a packet leaving the machine, and it leaks the hostname."""
    with pytest.raises(RuntimeError) as caught:
        socket.getaddrinfo(hostname, 443)

    assert denied(caught)
    assert hostname in str(caught.value)


def test_create_connection_is_covered_by_the_same_guard() -> None:
    """httpx, requests, and the Anthropic SDK all bottom out here."""
    with pytest.raises(RuntimeError) as caught:
        socket.create_connection(("api.anthropic.com", 443), timeout=1)

    assert denied(caught)


def test_loopback_is_still_allowed() -> None:
    """An in-process server is not egress; refusing it would break real tests."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=1) as client:
            assert client.getpeername()[0] == "127.0.0.1"


def test_unix_socket_pairs_are_not_treated_as_egress() -> None:
    """anyio and asyncio use socketpair internally; blocking it would break the suite."""
    left, right = socket.socketpair()
    with left, right:
        left.send(b"ok")
        assert right.recv(2) == b"ok"


def test_the_refusal_says_what_to_do_instead() -> None:
    """A guard that only says "denied" gets worked around; this one points at the fake."""
    with pytest.raises(RuntimeError) as caught:
        socket.create_connection(("api.anthropic.com", 443), timeout=1)

    message = str(caught.value)
    assert "api.provider.fake" in message
    assert "recorded fixture" in message
    assert "allow_network" in message
