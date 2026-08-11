"""The test suite is not allowed to touch the network (ENG-3).

Every test that would reach the vision provider goes through a recorded fixture or the
fake adapter. That is a stated requirement, and a stated requirement with nothing
enforcing it is a convention — it holds until someone adds one live call to debug
something and forgets to take it out. Then CI is green on a laptop with wifi and red
behind the customer's firewall, which is the exact failure the PRD opens with.

So this refuses the traffic instead of trusting the discipline. Any attempt to reach a
non-loopback address fails, names the address, and says what to use instead.

**Installed at import, not in a fixture.** pytest imports the root conftest before it
collects anything, so the guard is live during collection, during module-level code in
test files, and inside session-scoped fixtures — not only during the call phase. An
autouse fixture covers none of those, and "the guard only runs while a test body is
executing" is a hole shaped exactly like a module-level `requests.get(...)` at the top of
a test file.

**Every egress verb, not just `connect`.** The first version of this file patched
`connect`, `connect_ex` and `getaddrinfo`, and claimed in its own docstring that blocking
`connect` alone "would still leak the hostname". It then leaked hostnames two ways:
`socket.gethostbyname()` does not go through `getaddrinfo`, and a UDP `sendto()` needs no
`connect` at all — a DNS query over UDP sailed straight out and came back with a 61-byte
reply. Both are covered now, and both have a test that sends a real packet's worth of
intent at a real resolver.

CI additionally wraps the whole run in a network namespace (.github/workflows/ci.yml).
That is the stronger proof and it is Linux-only; this guard is the one that also runs on
a developer's machine, where the mistake actually gets made.

Opt out for a test that genuinely needs a socket with `@pytest.mark.allow_network`. There
are none today, and adding one should require an argument.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

import pytest

#: Addresses a test may reach. Loopback only — an in-process server on 127.0.0.1 is not
#: egress, and some libraries bind one to coordinate threads.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


class NetworkAccessDenied(RuntimeError):
    """Something in the test session tried to reach the outside world."""


class _Policy:
    """Whether egress is permitted right now.

    A module-level flag rather than a fixture argument, because the guard has to be
    installed before any fixture exists in order to cover collection.
    """

    allowed = False
    context = "during collection"


_policy = _Policy()


def _refuse(what: str, target: str) -> NetworkAccessDenied:
    return NetworkAccessDenied(
        f"{_policy.context}: {what} {target}.\n"
        "The suite runs offline by design (ENG-3): use api.provider.fake or a recorded "
        "fixture. If a socket is genuinely required, mark the test "
        "@pytest.mark.allow_network and say why in the docstring."
    )


def _host_of(address: object) -> str | None:
    if isinstance(address, tuple) and address and isinstance(address[0], str):
        return address[0]
    return None


def _describe(address: object) -> str:
    if isinstance(address, tuple) and address:
        return f"{address[0]}:{address[1] if len(address) > 1 else '?'}"
    return repr(address)


def _check_socket_target(sock: socket.socket, address: Any, verb: str) -> None:
    if _policy.allowed:
        return
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return  # AF_UNIX / socketpair: local IPC, not egress
    host = _host_of(address)
    if host is not None and host in _LOOPBACK_HOSTS:
        return
    raise _refuse(f"tried to {verb}", _describe(address))


def _check_hostname(host: object, verb: str) -> None:
    """Refuse to resolve a NAME, for the forward lookups.

    A resolver query is a packet leaving the machine and it leaks the hostname, so this
    is egress in its own right. It also makes the failure identical whether or not the
    machine has a route out — otherwise a sandboxed CI runner raises a bare `gaierror`
    that looks nothing like the laptop's error.

    The IP exemption is specific to `getaddrinfo` and `gethostbyname`: handed a literal
    address, both answer from the argument and no query is ever sent, so refusing would
    block work that never touches the network. It is **wrong for reverse lookups** — see
    `_check_address`.
    """
    if _policy.allowed or not isinstance(host, str) or host in _LOOPBACK_HOSTS:
        return
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise _refuse(f"tried to {verb}", repr(host)) from None


def _check_address(host: object, verb: str) -> None:
    """Refuse a reverse lookup. No IP exemption, because the argument is always an IP.

    `gethostbyaddr` took `_check_hostname` for a while, and the IP exemption there fired
    on literally every call — the argument to a reverse lookup parses as an address by
    definition, so the check returned clean every time and the PTR query went out. A live
    `gethostbyaddr("8.8.8.8")` returned `dns.google` with the guard fully armed.

    The exemption is right for the forward direction and catastrophic for this one, which
    is why they are two functions rather than one with a flag: the reason they differ
    belongs in the name.
    """
    if _policy.allowed or not isinstance(host, str) or host in _LOOPBACK_HOSTS:
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass  # a name here is even less legitimate than an address; refuse it too
    raise _refuse(f"tried to {verb}", repr(host))


def _install_guard() -> None:
    """Patch every verb that can put a packet on the wire. Runs once, at import."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_sendmsg = socket.socket.sendmsg
    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_gethostbyname_ex = socket.gethostbyname_ex
    real_gethostbyaddr = socket.gethostbyaddr

    def connect(sock: socket.socket, address: Any) -> None:
        _check_socket_target(sock, address, "connect to")
        real_connect(sock, address)

    def connect_ex(sock: socket.socket, address: Any) -> int:
        _check_socket_target(sock, address, "connect to")
        return real_connect_ex(sock, address)

    def sendto(sock: socket.socket, *args: Any) -> int:
        # sendto(data, address) or sendto(data, flags, address) — the address is last.
        if args:
            _check_socket_target(sock, args[-1], "send a datagram to")
        return real_sendto(sock, *args)

    def sendmsg(sock: socket.socket, *args: Any) -> int:
        # sendmsg(buffers[, ancdata[, flags[, address]]])
        if len(args) >= 4:
            _check_socket_target(sock, args[3], "send a message to")
        return real_sendmsg(sock, *args)

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        _check_hostname(host, "resolve")
        return real_getaddrinfo(host, port, *args, **kwargs)

    def gethostbyname(host: str) -> str:
        _check_hostname(host, "resolve")
        return real_gethostbyname(host)

    def gethostbyname_ex(host: str) -> Any:
        _check_hostname(host, "resolve")
        return real_gethostbyname_ex(host)

    def gethostbyaddr(host: str) -> Any:
        _check_address(host, "reverse-resolve")
        return real_gethostbyaddr(host)

    # Assigned through setattr: patching stdlib methods is exactly what a type checker
    # should object to, and the alternative — eight suppressions, each of which has to
    # name the right error code or it silences nothing and hides the next error behind
    # it — is a maintenance trap. This file's first version got four of those codes
    # wrong.
    for owner, name, replacement in (
        (socket.socket, "connect", connect),
        (socket.socket, "connect_ex", connect_ex),
        (socket.socket, "sendto", sendto),
        (socket.socket, "sendmsg", sendmsg),
        (socket, "getaddrinfo", getaddrinfo),
        (socket, "gethostbyname", gethostbyname),
        (socket, "gethostbyname_ex", gethostbyname_ex),
        (socket, "gethostbyaddr", gethostbyaddr),
    ):
        setattr(owner, name, replacement)


_install_guard()


def pytest_collection_finish(session: pytest.Session) -> None:
    _policy.context = "outside a test (session fixture or import)"


@pytest.fixture(autouse=True)
def _no_network_egress(request: pytest.FixtureRequest) -> Iterator[None]:
    """Name the offending test in the error, and honour `@pytest.mark.allow_network`."""
    opted_out = request.node.get_closest_marker("allow_network") is not None
    previous_allowed, previous_context = _policy.allowed, _policy.context
    _policy.allowed = opted_out
    _policy.context = request.node.nodeid
    try:
        yield
    finally:
        _policy.allowed, _policy.context = previous_allowed, previous_context
