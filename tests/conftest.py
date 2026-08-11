"""Suite-wide fixtures, the offline guard, and the coverage gate.

Three jobs, and each one exists because the suite makes a promise it would otherwise
only be making in prose.

**ENG-3 — the suite runs offline.** Every socket operation is blocked for the whole
session. Not "we use fakes so we probably do not call out" — blocked, so a test that
opens a socket fails loudly rather than passing on a laptop with wifi and failing in
CI. See `_no_network`.

**LP-245 — the coverage floor is enforced, not reported.** `pytest_sessionfinish`
reads the live coverage data and fails the run when a rules-engine module drops below
its floor. A floor that only prints a number is a number nobody reads.

**Shared fixtures.** Builders for the domain objects the property, regression, and
contract layers all need. Deliberately small: a fixture that hides which verdict a
test is asserting on makes the test unreadable, and unreadable tests get deleted.
"""

from __future__ import annotations

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


# --------------------------------------------------------------------------------------
# ENG-3 — no network egress, asserted rather than assumed
# --------------------------------------------------------------------------------------


class NetworkAccessError(RuntimeError):
    """A test tried to open a socket. The suite is offline by construction (ENG-3)."""


#: Attempts recorded during the session, so `tests/contract/test_offline.py` can prove
#: the guard is armed rather than merely installed.
BLOCKED_CONNECTIONS: list[str] = []


def _blocked(what: str) -> NetworkAccessError:
    BLOCKED_CONNECTIONS.append(what)
    return NetworkAccessError(
        f"This test suite runs with no network egress (ENG-3), and something tried to "
        f"{what}. Every provider call must go through an offline fake — see "
        f"api/provider/fake.py. If a test genuinely needs a socket it must be marked "
        f"`@pytest.mark.allow_network` and justified in the judgment log."
    )


@pytest.fixture(autouse=True, scope="session")
def _no_network() -> Iterator[None]:
    """Block every socket operation for the whole session.

    Patched at the `socket` module rather than at each HTTP client, because the point
    is to catch the call we did not anticipate. `httpx`, `requests`, the Anthropic SDK,
    and a stray `urllib` in a dependency all bottom out here.

    Starlette's `TestClient` drives the ASGI app in-process and never reaches this, so
    the full HTTP stack is still exercised — see `tests/e2e/`.
    """
    real_socket = socket.socket
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard_connect(self: Any, address: Any) -> Any:
        raise _blocked(f"connect a socket to {address!r}")

    def guard_connect_ex(self: Any, address: Any) -> Any:
        raise _blocked(f"connect a socket to {address!r}")

    def guard_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        raise _blocked(f"open a connection to {address!r}")

    def guard_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        raise _blocked(f"resolve the hostname {host!r}")

    socket.socket.connect = guard_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guard_connect_ex  # type: ignore[method-assign]
    socket.create_connection = guard_create_connection  # type: ignore[assignment]
    socket.getaddrinfo = guard_getaddrinfo  # type: ignore[assignment]

    try:
        yield
    finally:
        socket.socket = real_socket  # type: ignore[misc]
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.create_connection = real_create_connection  # type: ignore[assignment]
        socket.getaddrinfo = real_getaddrinfo  # type: ignore[assignment]


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
    """
    selected = list(session.config.option.file_or_dir or [])
    testpaths = [str(ROOT / path) for path in session.config.getini("testpaths")]
    return selected in ([], testpaths, [str(ROOT / "tests")], ["tests"])


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
    if session.config.getoption("--no-cov-gate", default=False):
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
        analysis = current._analyze(path)  # noqa: SLF001
        total = len(analysis.statements)
        return total, total - len(analysis.missing), sorted(analysis.missing)

    for absolute, reported in measured.items():
        try:
            total, covered, _ = _measure(reported)
        except Exception:  # pragma: no cover - unreadable file, already reported by cov
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
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
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


def make_field(value: str | None, *, confidence: float = 0.95, legible: bool = True) -> ExtractedField:
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

