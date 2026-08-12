"""The offline guard has to actually refuse (ENG-3).

`tests/conftest.py` claims the suite cannot reach the network. That claim is only worth
something if it is tested against the ways traffic actually leaves a machine, so these
attempt real egress over TCP, over UDP, and through the resolver, and assert every one is
refused before a packet goes out — identical results on a machine with a route to the
internet and on a sandboxed CI runner without one.

The first version of this file tested `connect` and `getaddrinfo` and stopped there. It
also parametrized `("8.8.8.8", 53)` under the id `udp-resolver` while building a
`SOCK_STREAM` socket — a TCP test wearing a UDP label, which made the suite look like it
covered UDP when nothing did. A real UDP DNS query went straight out and came back with a
61-byte reply while the guard was active. That case is now genuinely UDP.

The exception is matched by name rather than imported. pytest loads `conftest.py` as a
top-level module, so `from tests.conftest import NetworkAccessDenied` would produce a
second, unrelated class and every `pytest.raises` here would miss.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CONFTEST = Path(__file__).resolve().parent / "conftest.py"

#: A well-formed DNS query for example.com. Sent for real, at a real resolver: if the
#: guard has a hole this returns an answer rather than raising.
DNS_QUERY = (
    b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    b"\x07example\x03com\x00\x00\x01\x00\x01"
)


def denied(caught: pytest.ExceptionInfo[BaseException]) -> bool:
    return type(caught.value).__name__ == "NetworkAccessDenied"


# --- TCP -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "port"),
    [("1.1.1.1", 443), ("93.184.216.34", 80), ("8.8.8.8", 53)],
    ids=["cloudflare-https", "raw-ip-http", "google-dns-tcp"],
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


def test_create_connection_is_covered_by_the_same_guard() -> None:
    """httpx, requests, and the Anthropic SDK all bottom out here."""
    with pytest.raises(RuntimeError) as caught:
        socket.create_connection(("api.anthropic.com", 443), timeout=1)

    assert denied(caught)


# --- UDP: no connect() involved, which is how it escaped ------------------------------


def test_a_udp_datagram_cannot_be_sent_out() -> None:
    """This exact call reached 1.1.1.1 and got a 61-byte DNS reply through the old guard."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    with sock, pytest.raises(RuntimeError) as caught:
        sock.sendto(DNS_QUERY, ("1.1.1.1", 53))

    assert denied(caught)
    assert "1.1.1.1" in str(caught.value)


def test_udp_sendto_with_flags_is_guarded_on_the_right_argument() -> None:
    """`sendto(data, flags, address)` puts the address third, not second."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with sock, pytest.raises(RuntimeError) as caught:
        sock.sendto(DNS_QUERY, 0, ("1.1.1.1", 53))

    assert denied(caught)


def test_a_connected_udp_socket_is_refused_at_connect() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with sock, pytest.raises(RuntimeError) as caught:
        sock.connect(("1.1.1.1", 53))

    assert denied(caught)


def test_sendmsg_to_an_outside_address_is_refused() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with sock, pytest.raises(RuntimeError) as caught:
        sock.sendmsg([DNS_QUERY], [], 0, ("1.1.1.1", 53))

    assert denied(caught)


# --- name resolution -------------------------------------------------------------------


@pytest.mark.parametrize(
    "resolver",
    ["getaddrinfo", "gethostbyname", "gethostbyname_ex"],
)
@pytest.mark.parametrize("hostname", ["api.anthropic.com", "pypi.org"])
def test_every_resolver_entry_point_is_refused(resolver: str, hostname: str) -> None:
    """`gethostbyname` does not go through `getaddrinfo`; patching one missed the other."""
    call = getattr(socket, resolver)
    with pytest.raises(RuntimeError) as caught:
        call(hostname, 443) if resolver == "getaddrinfo" else call(hostname)

    assert denied(caught)
    assert hostname in str(caught.value)


@pytest.mark.parametrize(
    "target",
    ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "one.one.one.one"],
    ids=["ipv4", "ipv4-alt", "ipv6", "name"],
)
def test_reverse_resolution_is_refused(target: str) -> None:
    """An IP argument is the normal case here, and it was the unguarded one.

    `gethostbyaddr` shared the forward-lookup check, whose IP exemption exists because
    `getaddrinfo("1.2.3.4")` answers from its argument without sending anything. A
    reverse lookup's argument is *always* an address, so the exemption fired on every
    call and the PTR query went out: `socket.gethostbyaddr("8.8.8.8")` returned
    `dns.google` with the guard fully armed.

    The previous version of this test passed `one.one.one.one`, a hostname — the one
    input shape that took the refusal path. That is exactly the failure it was written to
    prevent, one function over: probe the shape the code actually receives, not the shape
    that happens to be handled.
    """
    with pytest.raises(RuntimeError) as caught:
        socket.gethostbyaddr(target)

    assert denied(caught)
    assert target in str(caught.value)


def test_a_forward_lookup_of_a_literal_ip_is_still_allowed() -> None:
    """The exemption `_check_hostname` keeps, and the reason the two checks are separate.

    `getaddrinfo("127.0.0.1", 0)` sends no query — it answers from its argument — so
    refusing it would block work that never touches the network.
    """
    assert socket.getaddrinfo("127.0.0.1", 0)


def test_reverse_resolution_of_loopback_is_still_allowed() -> None:
    """`_check_address` refuses every address except loopback, which is not egress."""
    assert socket.gethostbyaddr("127.0.0.1")


# --- what must keep working ------------------------------------------------------------


def test_loopback_is_still_allowed() -> None:
    """An in-process server is not egress; refusing it would break real tests."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=1) as client:
            assert client.getpeername()[0] == "127.0.0.1"


def test_loopback_udp_is_still_allowed() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.sendto(b"ping", server.getsockname())
            assert server.recv(4) == b"ping"


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


def test_the_refusal_names_the_test_that_caused_it() -> None:
    with pytest.raises(RuntimeError) as caught:
        socket.create_connection(("1.1.1.1", 443), timeout=1)

    assert "test_the_refusal_names_the_test_that_caused_it" in str(caught.value)


@pytest.mark.allow_network
def test_the_opt_out_marker_actually_lifts_the_guard() -> None:
    """Not that we use it — but a marker that silently does nothing is a trap."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.001)
        # Reaching the real connect (timeout / refused / unreachable) proves the guard
        # stood aside. NetworkAccessDenied would mean the marker did nothing.
        with pytest.raises(OSError) as caught:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1, RFC 5737: never routable

    assert not denied(caught)


# --- the phases an autouse fixture cannot reach ----------------------------------------
#
# Egress at import or from a session fixture happens outside any test's call phase. These
# run pytest in a subprocess against a throwaway suite, because that is the only way to
# observe collection failing.


def _run_isolated(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    """Run the real conftest over a two-file throwaway suite, in a fresh interpreter.

    The repository root goes on `PYTHONPATH` because the conftest being copied is the
    whole file, not an extract of the guard. It grew a coverage gate and shared fixtures
    when the `wave/tests` branch merged, and those import `api.models` at module scope —
    so a copy run with only `tmp_path` importable dies with `ModuleNotFoundError` before
    the guard ever installs, and every probe below fails for a reason that has nothing to
    do with the network.

    Copying the whole file is the point: an extract would be a second guard, tested in
    isolation from the one that actually runs.
    """
    (tmp_path / "conftest.py").write_text(CONFTEST.read_text())
    (tmp_path / "test_probe.py").write_text(textwrap.dedent(body))
    environment = dict(os.environ)
    root = str(CONFTEST.resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(
        [root, *([existing] if (existing := environment.get("PYTHONPATH")) else [])]
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path), "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=environment,
    )


def test_egress_at_module_import_is_caught_during_collection(tmp_path: Path) -> None:
    """A `requests.get(...)` at the top of a test file never enters a call phase."""
    result = _run_isolated(
        tmp_path,
        """
        import socket

        ADDR = socket.gethostbyname("example.com")

        def test_nothing() -> None:
            assert ADDR
        """,
    )

    assert result.returncode != 0
    assert "NetworkAccessDenied" in result.stdout + result.stderr


def test_egress_from_a_session_fixture_is_caught(tmp_path: Path) -> None:
    result = _run_isolated(
        tmp_path,
        """
        import socket
        import pytest

        @pytest.fixture(scope="session")
        def warmed() -> str:
            return socket.gethostbyname("example.com")

        def test_uses_it(warmed: str) -> None:
            assert warmed
        """,
    )

    assert result.returncode != 0
    assert "NetworkAccessDenied" in result.stdout + result.stderr


def test_an_ordinary_offline_suite_still_passes(tmp_path: Path) -> None:
    """The guard must be invisible to code that behaves. Otherwise it gets removed."""
    result = _run_isolated(
        tmp_path,
        """
        def test_arithmetic() -> None:
            assert 2 + 2 == 4
        """,
    )

    assert result.returncode == 0, result.stdout + result.stderr
