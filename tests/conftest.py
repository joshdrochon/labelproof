"""Suite-wide fixtures, the offline guard, and the coverage gate.

Three jobs, and each exists because the suite makes a promise it would otherwise only be
making in prose.

**ENG-3 — the suite runs offline.** Every verb that can put a packet on the wire is
refused, and the guard is installed at import so it also covers collection. This half of
the file came from `wave/ci`, unchanged, and the history is worth keeping because it is
the same mistake twice.

Both branches wrote a guard independently. Both patched `connect`, `connect_ex` and
`getaddrinfo` and both claimed in their own docstrings to block everything. Both leaked:
`socket.gethostbyname()` does not route through `getaddrinfo`, a UDP `sendto()` needs no
`connect` at all, and a session-scoped fixture is installed *after* collection, so a
module-level `create_connection` at the top of a test file sailed straight out. A
reviewer sent real packets through both and got live answers back — 5 bytes to 8.8.8.8,
and `example.com` resolved to a real address.

So there is now one guard rather than two, and this is it. It refuses `connect`,
`connect_ex`, `sendto`, `sendmsg`, `getaddrinfo`, `gethostbyname`, `gethostbyname_ex`
and `gethostbyaddr`; it runs from import time; and loopback is permitted deliberately,
because an in-process server on 127.0.0.1 is not egress. `tests/contract/test_offline.py`
sends a real packet's worth of intent at each of those verbs, so the next version of this
file cannot regress quietly.

**LP-245 — the coverage floor is enforced, not reported.** `pytest_sessionfinish` reads
the live coverage data and fails the run when a rules-engine module drops below its
floor. A floor that only prints a number is a number nobody reads.

**Shared fixtures.** Builders for the domain objects the property, regression, and
contract layers all need. Deliberately small: a fixture that hides which verdict a test
is asserting on makes the test unreadable, and unreadable tests get deleted.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from api.models import (
    Application,
    Commodity,
    ExtractedField,
    FieldName,
    FieldResult,
    Verdict,
)

ROOT = Path(__file__).resolve().parents[1]

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
    """Refuse to resolve a name.

    A resolver query is a packet leaving the machine and it leaks the hostname, so this
    is egress in its own right. It also makes the failure identical whether or not the
    machine has a route out — otherwise a sandboxed CI runner raises a bare `gaierror`
    that looks nothing like the laptop's error.

    An IP literal is allowed through: `getaddrinfo("8.8.8.8", 53)` is pure arithmetic
    and sends nothing. That exemption is correct *here* and catastrophic for reverse
    lookups — see `_check_address`.
    """
    if _policy.allowed or not isinstance(host, str) or host in _LOOPBACK_HOSTS:
        return
    try:
        ipaddress.ip_address(host)
    except ValueError:
        raise _refuse(f"tried to {verb}", repr(host)) from None


def _check_address(host: object, verb: str) -> None:
    """Refuse to reverse-resolve an address.

    Split out from `_check_hostname` because sharing it was a hole. That function lets
    an IP literal through — right for a forward lookup, which does no I/O on one — and
    `gethostbyaddr`'s argument is *always* an IP literal. So the guard patched the
    function and then exempted every possible input to it: `gethostbyaddr("8.8.8.8")`
    went out and came back `('dns.google', ...)` with the guard fully armed.

    Found by `tests/contract/test_offline.py` after that file was rewritten to probe
    each verb separately rather than to probe the one path the implementation happened
    to take. It is the same defect the guard rewrite was fixing, one layer down.
    """
    if _policy.allowed or not isinstance(host, str) or host in _LOOPBACK_HOSTS:
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
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
        # _check_address, not _check_hostname: the latter exempts IP literals, which is
        # every argument this function can take.
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


# --------------------------------------------------------------------------------------
# LP-245 — the coverage floor, enforced
# --------------------------------------------------------------------------------------

#: Per-module statement-coverage floors for the rules engine. These modules decide
#: verdicts; a line of one of them that no test has ever run is a line that has never
#: been checked against the regulation it encodes. Everything here is at 100% and stays
#: there — one documented exception, below.
#:
#: Raise a floor when coverage rises. **Never lower one to make a build pass**; if a
#: line genuinely cannot be reached, say so here in writing, the way `fills.py` does.
RULES_COVERAGE_FLOORS: dict[str, float] = {
    # Tier 3 is the one module here whose answer comes from a judgement rather than a
    # rule, and almost every line of it is a refusal — the allowlist, the verdict filter,
    # the confidence floor, the two budget checks, the exception path. An unexercised
    # line in this file is an unexercised way for a model's opinion to reach a verdict.
    "api/rules/adjudicate.py": 100.0,
    "api/rules/aggregate.py": 100.0,
    "api/rules/abv.py": 100.0,
    "api/rules/commodity.py": 100.0,
    "api/rules/compare.py": 100.0,
    # 98% rather than 100%, deliberately. `fills.parse` looks a unit up in the table,
    # retries with a trailing `s` stripped, and returns unreadable if both miss. That
    # last return is unreachable through `parse`: every alternative the quantity regex
    # can match resolves through one of the two lookups, checked exhaustively in
    # tests/properties/test_fills_properties.py. It is correct defensive code guarding
    # against a future regex alternative with no table row, and the only ways to reach
    # 100% would be to call a private helper directly — testing the implementation
    # rather than the rule — or to delete the guard. Neither is worth a percentage
    # point. `fills.py` belongs to another owner, so a `# pragma: no cover` on the line
    # is not this suite's to add.
    "api/rules/fills.py": 98.0,
    "api/rules/normalize.py": 100.0,
    "api/rules/warning.py": 100.0,
    # 27 CFR 16.22's appearance rules — bold, capitals, prominence, contrast — split out
    # of warning.py. It decides whether the government warning row can be a Match, so it
    # is squarely inside the reason this per-module gate exists and gets the same floor
    # as the module it came from. Measured at 100% of statements and branches.
    "api/rules/typography.py": 100.0,
    "api/rules/thresholds.py": 100.0,
}


#: Whole-project floor, enforced alongside the per-module one. Deliberately below the
#: measured number so that ordinary drift does not fail a build, while a large deletion
#: of tests does.
PROJECT_COVERAGE_FLOOR = 88.0


def _ran_the_whole_suite(session: pytest.Session) -> bool:
    """Did this invocation run the full suite, or a hand-picked selection?

    The gate only applies to the full run. A floor that fires on
    `pytest tests/properties` reports that the rest of the suite did not run — which
    the developer already knows — and the lesson learned is to pass a skip flag, which
    then gets pasted into CI.

    **Compares resolved paths, not the strings the user typed.** The first version
    compared literal selection strings against a small allowlist, so with a rules module
    dropped to 90.9% against a 100% floor:

        pytest tests   -> exit 1, gate fired
        pytest tests/  -> exit 0, SILENT
        pytest ./tests -> exit 0, SILENT

    A trailing slash turned the release gate off, and printed the coverage table on the
    way past so the run still looked measured. Any CI line written `pytest tests/` —
    which is how most people write it — enforced nothing. That is the same shape as an
    eval gate bypassed by one word, in the file whose job is to stop that.
    """
    selected = list(session.config.option.file_or_dir or [])
    if not selected:
        return True
    configured = {(ROOT / path).resolve() for path in session.config.getini("testpaths")}
    configured.add((ROOT / "tests").resolve())
    try:
        chosen = {Path(path).resolve() for path in selected}
    except (OSError, ValueError):  # pragma: no cover - a selection like "tests::foo"
        return False
    return chosen <= configured


def _live_coverage(session: pytest.Session) -> Any:
    """The `Coverage` object pytest-cov is driving, or None.

    Asked of the plugin rather than of `Coverage.current()`. pytest-cov stops coverage
    in its own `pytest_sessionfinish`, and once it has, `current()` is None — so a gate
    that relied on it would silently do nothing depending on hook ordering, which is
    precisely the kind of gate this codebase already got burned by.
    """
    plugin = session.config.pluginmanager.get_plugin("_cov")
    controller = getattr(plugin, "cov_controller", None)
    live = getattr(controller, "cov", None)
    if live is not None:
        return live
    try:
        from coverage import Coverage
    except ImportError:  # pragma: no cover - coverage is a declared dev dependency
        return None
    return Coverage.current()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Enforce the coverage floors (LP-245).

    Reads the live coverage session rather than a report file, so the gate cannot pass
    by measuring a stale artifact. Two floors:

    * every rules-engine module, individually — those modules decide verdicts, and a
      line of one of them that has never run has never been checked against the
      regulation it encodes;
    * the project as a whole, as a backstop.

    Enforced here rather than through `--cov-fail-under` because coverage.py has one
    global threshold and the floor that matters is per-module, and because this hook
    knows whether the whole suite ran.
    """
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")

    if session.config.getoption("--no-cov-gate", default=False):
        # Announced, not silent. A bypass flag ends up pasted into a CI file eventually,
        # and the only thing that stops it living there for a year is that every run
        # says so in the output somebody is already reading.
        if reporter is not None:
            reporter.write_line("")
            reporter.write_line(
                "COVERAGE GATE DISABLED by --no-cov-gate. Coverage was measured and "
                "NOT enforced (LP-245). Do not use this flag in CI.",
                yellow=True,
                bold=True,
            )
        return
    if exitstatus not in (0, pytest.ExitCode.OK):
        return  # already failing; a coverage complaint would only bury the real error
    if not _ran_the_whole_suite(session):
        return

    current = _live_coverage(session)
    if current is None:
        return

    measured = {Path(f).resolve(): f for f in current.get_data().measured_files()}
    failures: list[str] = []
    project_statements = project_covered = 0

    def _measure(path: str) -> tuple[int, int, list[int]]:
        analysis = current._analyze(path)
        total = len(analysis.statements)
        return total, total - len(analysis.missing), sorted(analysis.missing)

    for reported in measured.values():
        try:
            total, covered, _ = _measure(reported)
        except Exception:  # noqa: BLE001, S112 - pragma: no cover - unreadable file, cov already said so
            continue
        project_statements += total
        project_covered += covered

    for relative, floor in sorted(RULES_COVERAGE_FLOORS.items()):
        absolute = (ROOT / relative).resolve()
        if absolute not in measured:
            failures.append(f"{relative}: not measured at all (floor {floor:g}%)")
            continue
        total, covered, missing = _measure(measured[absolute])
        if total == 0:
            continue
        percent = 100.0 * covered / total
        if percent + 1e-9 < floor:
            shown = ", ".join(str(line) for line in missing[:12])
            failures.append(
                f"{relative}: {percent:.1f}% < floor {floor:g}% (uncovered: {shown})"
            )

    if project_statements:
        percent = 100.0 * project_covered / project_statements
        if percent + 1e-9 < PROJECT_COVERAGE_FLOOR:
            failures.append(
                f"project: {percent:.1f}% < floor {PROJECT_COVERAGE_FLOOR:g}%"
            )

    if failures:
        session.exitstatus = 1
        if reporter is not None:
            reporter.write_line("")
            reporter.write_line("COVERAGE FLOOR NOT MET (LP-245):", red=True, bold=True)
            for line in failures:
                reporter.write_line(f"  {line}", red=True)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--no-cov-gate",
        action="store_true",
        default=False,
        help="Measure coverage but do not enforce the floors (LP-245).",
    )


# --------------------------------------------------------------------------------------
# Shared builders
# --------------------------------------------------------------------------------------


@pytest.fixture
def spirits_application() -> Application:
    """The Old Tom application — the brief's own sample, fully compliant."""
    return Application(
        commodity=Commodity.SPIRITS,
        brand_name="OLD TOM DISTILLERY",
        class_type="Kentucky Straight Bourbon Whiskey",
        alcohol_content=45.0,
        net_contents="750 mL",
        producer_name="Old Tom Distillery",
        producer_address="Bardstown, Kentucky",
        country_of_origin=None,
        is_import=False,
    )


def make_field(
    value: str | None, *, confidence: float = 0.95, legible: bool = True
) -> ExtractedField:
    """One extracted field, with the defaults a clean reading would have."""
    return ExtractedField(value=value, confidence=confidence, legible=legible)


def make_result(field: FieldName, verdict: Verdict) -> FieldResult:
    """A `FieldResult` carrying only what the aggregator reads.

    Aggregation is a function of `(field, verdict)` and nothing else — that is the
    property the aggregate tests assert — so the rest is filled with placeholders
    rather than plausible-looking noise that would read as significant.
    """
    return FieldResult(
        field=field,
        verdict=verdict,
        extracted=None,
        expected=None,
        confidence=1.0,
        rationale="",
    )


@pytest.fixture
def field_result_builder() -> Any:
    return make_result


def png_bytes(
    color: tuple[int, int, int] = (250, 248, 242), size: tuple[int, int] = (1600, 2200)
) -> bytes:
    """A real PNG, large enough and sharp enough to clear the quality pre-gate.

    Two things a test that wants to reach the provider needs, and a flat rectangle has
    neither. Ingest sniffs magic bytes, decodes and re-encodes, so `b"not a png"` is
    rejected at the door. Then the pre-gate (LP-321) scores the image: a single flat
    colour has zero Laplacian variance, reads as hopelessly blurred, and returns
    Unreadable without ever choosing a provider.

    So the drawing below is deliberate rather than decorative — it puts edges in the
    frame so the image is *scored as readable* and the request continues into the path
    under test. Deterministic, because a fixture that occasionally scores differently
    is a flaky test (ENG-3, LP-246).
    """
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    for row in range(0, size[1], 40):
        draw.rectangle([40, row, size[0] - 40, row + 18], fill=(20, 20, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def upload(name: str, data: bytes | None = None) -> list[tuple[str, tuple[str, bytes, str]]]:
    """One multipart image part, in the shape `TestClient.post(files=...)` wants."""
    return [("images", (name, data if data is not None else png_bytes(), "image/png"))]


def fixture_upload(*names: str) -> list[tuple[str, tuple[str, bytes, str]]]:
    """Real generated label images, by fixture filename."""
    labels = ROOT / "fixtures" / "labels"
    return [("images", (n, (labels / n).read_bytes(), "image/png")) for n in names]


def underexposed_label_png() -> bytes:
    """A real label, sharp, and too dark to read.

    Built from the rendered fixture and dimmed, rather than from a flat dark rectangle.
    A flat rectangle has zero Laplacian variance, so the quality gate reports it as
    *blurred* and the retake reason tells the agent to hold the camera steadier — the
    wrong advice, and a test asserting on it would be asserting the wrong thing.
    """
    import io

    import numpy as np
    from PIL import Image

    from fixtures.generator.catalog import by_name
    from fixtures.generator.render import render

    label = np.asarray(render(by_name("tc01_old_tom_clean")).convert("RGB"))
    dark = (label.astype(np.float32) * 0.06).clip(0, 255).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(dark).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def underexposed_label() -> bytes:
    return underexposed_label_png()


@pytest.fixture
def uploads() -> Any:
    """`upload` as a fixture, so test modules need no cross-package import.

    `tests/` is not a package — adding `__init__.py` files to make it one changes how
    pytest resolves every existing module in the suite, which is not a change worth
    making to share two helpers.
    """
    return upload


@pytest.fixture
def fixture_uploads() -> Any:
    return fixture_upload

