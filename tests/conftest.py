"""The test suite is not allowed to touch the network (ENG-3).

Every test that would reach the vision provider goes through a recorded fixture or the
fake adapter. That is a stated requirement, and a stated requirement with nothing
enforcing it is a convention — it holds until someone adds one live call to debug
something and forgets to take it out. Then CI is green on a laptop with wifi and red
behind the customer's firewall, which is the exact failure the PRD opens with.

So this refuses the connection instead of trusting the discipline. Any attempt to open a
socket to a non-loopback address fails the test that made it, names the address, and says
what to use instead.

CI wraps the whole run in a network namespace as well (.github/workflows/ci.yml). That is
the stronger proof, and it is Linux-only; this guard is the one that also runs on a
developer's machine, where the mistake actually gets made.

Opt out for a test that genuinely needs a socket with `@pytest.mark.allow_network`. There
are none today, and adding one should require an argument.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

import pytest

#: Addresses a test may connect to. Loopback only — an in-process server on 127.0.0.1 is
#: not egress, and some libraries bind one to coordinate threads.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


class NetworkAccessDenied(RuntimeError):
    """A test tried to open a socket to the outside world."""


def _describe(address: object) -> str:
    if isinstance(address, tuple) and address:
        return f"{address[0]}:{address[1] if len(address) > 1 else '?'}"
    return repr(address)


def _host_of(address: object) -> str | None:
    if isinstance(address, tuple) and address and isinstance(address[0], str):
        return address[0]
    return None


@pytest.fixture(autouse=True)
def _no_network_egress(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Fail any test that opens a socket to a non-loopback address."""
    if request.node.get_closest_marker("allow_network") is not None:
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(sock: socket.socket, address: Any) -> None:
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return  # AF_UNIX / socketpair: local IPC, not egress
        host = _host_of(address)
        if host is not None and host in _LOOPBACK_HOSTS:
            return
        raise NetworkAccessDenied(
            f"{request.node.nodeid} tried to connect to {_describe(address)}.\n"
            "The suite runs offline by design (ENG-3): use api.provider.fake or a "
            "recorded fixture. If a socket is genuinely required, mark the test "
            "@pytest.mark.allow_network and say why in the docstring."
        )

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        guard(sock, address)
        real_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
        guard(sock, address)
        return real_connect_ex(sock, address)

    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        """Refuse the DNS lookup too.

        A resolver query is a packet leaving the machine, so blocking only `connect`
        would still leak the hostname. Refusing here also makes the failure identical
        whether or not the machine has a route out — a bare `socket.gaierror` from a
        sandboxed CI runner would otherwise look nothing like the laptop's error.
        """
        if isinstance(host, str) and host not in _LOOPBACK_HOSTS:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise NetworkAccessDenied(
                    f"{request.node.nodeid} tried to resolve {host!r}.\n"
                    "The suite runs offline by design (ENG-3): use api.provider.fake or "
                    "a recorded fixture. If a socket is genuinely required, mark the "
                    "test @pytest.mark.allow_network and say why in the docstring."
                ) from None
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
