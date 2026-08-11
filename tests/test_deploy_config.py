"""The deployment configuration, asserted rather than assumed (LP-128 through LP-134).

`fly.toml` and the `Dockerfile` make claims — no cold start, HTTPS only, the demo works,
no secrets in the repository — that are otherwise only checkable by deploying and
looking. Several of them fail in ways that still return 200, which is exactly the class
of failure this project keeps having to design against.

The two that matter most:

**The image must contain what `/sample` serves.** The Dockerfile deliberately copies a
handful of paths out of `fixtures/` instead of the whole tree, because production has no
business carrying the golden set. The cost of that is a way to be wrong: exclude one file
the demo needs and the build succeeds, the container boots, both health checks pass, and
the grader's one-click sample returns an error. That is the worst available outcome for a
take-home, so the manifest the route actually serves is compared against the manifest the
image actually copies.

**The keep-warm settings must survive editing.** `min_machines_running`,
`auto_stop_machines` and the pre-warm interval are the whole of PERF-6. They are three
lines of TOML that look like tuning knobs and are actually the adoption gate.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from api.routes.sample import _IMAGES, _LABELS, _SAMPLE_JSON

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
FLY_TOML = ROOT / "fly.toml"
DOCKERIGNORE = ROOT / ".dockerignore"


@pytest.fixture(scope="module")
def fly() -> dict[str, Any]:
    with FLY_TOML.open("rb") as handle:
        config: dict[str, Any] = tomllib.load(handle)
    return config


def _copied_paths() -> list[str]:
    """Source paths the runtime stage copies in, normalised.

    `COPY --from=web` lines are excluded: those carry build output, not repository files,
    so there is nothing in the working tree to compare them against.
    """
    text = DOCKERFILE.read_text()
    # Fold line continuations so a multi-line COPY reads as one instruction.
    text = re.sub(r"\\\n\s*", " ", text)

    sources: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        if "--from=" in stripped:
            continue
        tokens = [t for t in stripped.split()[1:] if not t.startswith("--")]
        # The last token is the destination.
        sources.extend(tokens[:-1])
    return sources


def _is_copied(relative: Path, copied: list[str]) -> bool:
    """True when some COPY source covers this repository-relative path."""
    wanted = relative.as_posix()
    for source in copied:
        source = source.rstrip("/")
        if wanted == source or wanted.startswith(source + "/"):
            return True
    return False


# --- the image carries what the product serves (LP-129, UX-1) -------------------------


def test_the_runtime_image_carries_the_one_click_sample() -> None:
    """Every file `GET /sample` serves is copied into the image.

    Read from `api.routes.sample` rather than restated here, so adding a third demo image
    fails this test instead of failing in front of a reviewer.
    """
    copied = _copied_paths()

    required = [_SAMPLE_JSON.relative_to(ROOT)]
    required += [(_LABELS / name).relative_to(ROOT) for name, _ in _IMAGES]

    missing = [str(path) for path in required if not _is_copied(path, copied)]
    assert not missing, (
        f"the Dockerfile does not copy {missing} into the runtime image. The one-click "
        f"demo will return an error on the deployed URL while every health check passes."
    )


def test_the_application_package_is_copied() -> None:
    assert _is_copied(Path("api"), _copied_paths())


def test_the_image_does_not_carry_the_test_or_eval_trees() -> None:
    """Production has no business shipping the golden set, the eval harness, or the test
    suite. Excluded at the context boundary so an over-broad COPY fails the build."""
    ignored = DOCKERIGNORE.read_text().splitlines()
    entries = {
        line.strip().rstrip("/")
        for line in ignored
        if line.strip() and not line.startswith("#")
    }

    for tree in ("tests", "eval", "golden"):
        assert tree in entries, f"{tree}/ is not excluded from the build context"


def test_no_secret_reaches_the_build_context() -> None:
    """There is a real .env with a live key in the working tree. `docker build` reads the
    working tree, not git, so .gitignore is not the control that matters here."""
    ignored = {
        line.strip() for line in DOCKERIGNORE.read_text().splitlines() if line.strip()
    }
    assert ".env" in ignored
    assert ".env.*" in ignored


def test_no_secret_is_baked_into_the_image_or_config() -> None:
    """The API key arrives from the platform secret store at runtime (SEC-6, LP-128).

    A build argument would be recorded in image history; a value in `[env]` would be
    readable by anyone who can pull the image.
    """
    dockerfile = DOCKERFILE.read_text()
    assert "ARG ANTHROPIC" not in dockerfile
    assert re.search(r"ANTHROPIC_API_KEY\s*=\s*\S", dockerfile) is None

    fly_text = FLY_TOML.read_text()
    # Mentioned only in the comment explaining how to set it as a secret.
    assert re.search(r"^\s*ANTHROPIC_API_KEY\s*=", fly_text, re.MULTILINE) is None


# --- no cold-start ambush (PERF-6, LP-134) --------------------------------------------


def test_a_machine_is_always_running(fly: dict[str, Any]) -> None:
    """The adoption gate is 5s from a cold click. A stopped machine cannot meet it."""
    service = fly["http_service"]
    assert service["min_machines_running"] >= 1
    assert service["auto_stop_machines"] == "off", (
        "auto_stop_machines must be off. 'suspend' still charges the grader's first "
        "request for the resume, and the first request is the one being protected."
    )


def test_keepwarm_is_enabled_and_pings_inside_the_cache_ttl(fly: dict[str, Any]) -> None:
    """Above the provider's five-minute ephemeral TTL, the pre-warm inverts: every ping
    pays for a cache write that nothing ever reads (LP-324)."""
    env = fly["env"]
    assert env["LABELPROOF_KEEPWARM"] == "1"
    assert int(env["LABELPROOF_KEEPWARM_INTERVAL_S"]) < 300


def test_production_cannot_be_put_into_sample_mode_by_omission(fly: dict[str, Any]) -> None:
    """Pinned, not defaulted. A production instance replaying built-in fixtures answers
    200 to every check and hands a reviewer demonstration verdicts (LP-132)."""
    assert fly["env"]["LABELPROOF_FAKE_PROVIDER"] == "0"


# --- transport and health (SEC-6, LP-083, LP-133, LP-132) -----------------------------


def test_https_only(fly: dict[str, Any]) -> None:
    assert fly["http_service"]["force_https"] is True


def test_hsts_is_set_at_the_edge(fly: dict[str, Any]) -> None:
    headers = fly["http_service"]["http_options"]["response"]["headers"]
    hsts = headers["Strict-Transport-Security"]
    assert "max-age=" in hsts
    age = int(re.search(r"max-age=(\d+)", hsts).group(1))  # type: ignore[union-attr]
    # Below a year a browser will not preload, which is most of the point.
    assert age >= 31_536_000
    assert "includeSubDomains" in hsts


def test_the_other_security_headers_are_present(fly: dict[str, Any]) -> None:
    headers = fly["http_service"]["http_options"]["response"]["headers"]
    for name in (
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Content-Security-Policy",
    ):
        assert name in headers, f"{name} is not set at the edge"


def test_the_csp_allows_what_the_spa_actually_does(fly: dict[str, Any]) -> None:
    """A policy that blocks the app is worse than no policy: the reviewer sees a blank
    page and no error. `EvidenceOverlay` positions boxes with a style attribute, and
    upload previews are object URLs."""
    csp = fly["http_service"]["http_options"]["response"]["headers"]["Content-Security-Policy"]
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "blob:" in csp and "data:" in csp
    # The browser never talks to the model provider (NET-2) — same-origin only.
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_both_health_endpoints_are_wired_into_platform_checks(fly: dict[str, Any]) -> None:
    """`/health` is liveness and `/ready` is rotation. Wiring only one of them loses the
    distinction that keeps a provider outage from killing a working container (NET-5)."""
    paths = {check["path"] for check in fly["http_service"]["checks"]}
    assert paths == {"/health", "/ready"}


def test_the_region_is_us_east(fly: dict[str, Any]) -> None:
    """The users are a US federal agency in Washington DC, and the provider's traffic
    terminates in us-east (LP-127)."""
    assert fly["primary_region"] == "iad"


def test_no_volume_is_mounted(fly: dict[str, Any]) -> None:
    """Uploads and results are ephemeral by policy (SEC-2), and a volume would also add a
    manual step ahead of `fly deploy`, falsifying LP-136's rebuild-from-config claim."""
    assert "mounts" not in fly
