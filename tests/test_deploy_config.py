"""The deployment configuration, asserted rather than assumed (LP-128 through LP-134).

`fly.toml` and the `Dockerfile` make claims — no cold start, HTTPS only, the demo works,
no secrets in the repository — that are otherwise only checkable by deploying and
looking. Several of them fail in ways that still return 200, which is exactly the class
of failure this project keeps having to design against.

The two that matter most:

**The image must contain what the demos serve.** The Dockerfile deliberately copies a
handful of paths out of `fixtures/` instead of the whole tree, because production has no
business carrying the golden set. The cost of that is a way to be wrong: exclude one file
a demo needs and the build succeeds, the container boots, both health checks pass, and
the grader's one-click sample returns an error. That is the worst available outcome for a
take-home, so the artwork the routes actually name is compared against the artwork the
image actually copies — for `GET /sample` and for `POST /batch/sample` alike.

**Configuration in one file must agree with code in another.** The latency budget against
the model's measured latency, the keep-warm interval against the clamp the script
enforces, the health-check paths against the routes the app registers, the `COPY` set
against what `.dockerignore` removes, the CSP against what the SPA actually does. Each of
those pairs can drift silently, and each has exactly one failure mode: production looks
fine and does not work.

**On what is NOT here.** An earlier version of this file was mostly change-detectors —
reading a literal out of `fly.toml` and asserting it back. Those cannot fail against a
broken deployment, and their green was worth nothing. The ones that remain in that shape
are a deliberate short list guarding values whose *editing* is the risk (HTTPS on, autostop
off, no volume); they are labelled as guards rather than dressed up as correctness tests.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from api.routes.batch import _SAMPLE_ROWS, _sample_artwork
from api.routes.sample import _GOLDEN, _LABELS, servable_images

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


def _artwork_each_demo_names() -> dict[str, list[Path]]:
    """Repository-relative files the two one-click demos ask for, by demo.

    Derived from the shipped code, never restated: `servable_images()` for the picker on
    the single-check screen, and `_SAMPLE_ROWS` walked through `_sample_artwork` for the
    batch — which is the same function `POST /batch/sample` itself calls, so the list here
    cannot describe a different sample than the one a reviewer gets.
    """
    return {
        "GET /sample": [(_LABELS / name).relative_to(ROOT) for name in sorted(servable_images())],
        "POST /batch/sample": [
            path.resolve().relative_to(ROOT)
            for spec in _SAMPLE_ROWS
            for path in _sample_artwork(spec)
        ],
    }


def test_the_runtime_image_carries_both_one_click_samples() -> None:
    """Every image the two demos name is copied into the image, and the golden set with them.

    **Read the Dockerfile, not the disk.** The batch sample shipped broken to production
    asking for four label files and a robustness photograph that no COPY line named, and
    `POST /batch/sample` answered 500 on the deployed URL while `/health` and `/ready`
    stayed green. A green CI run, the full local suite, `scripts/smoke.sh` and a hand
    walkthrough all missed it for one reason: every one of them runs against a source
    checkout, where all 45 fixture images are sitting right there on disk. Only the
    container is missing them. So a test that resolves these paths and calls `.exists()`
    passes on a build nobody can use — the only artefact that can answer the question is
    the COPY list itself.

    The same gap caught the single-check picker when it went from one sample to four while
    this block still named two images and no manifest, and again when the batch sample was
    reworked onto seven products. Both demos are derived here now, so the next one fails
    this test instead of failing in front of a reviewer.
    """
    copied = _copied_paths()

    # The manifest both demos read their applications OUT of, plus every image they name.
    required = {"golden/set.json": [_GOLDEN.relative_to(ROOT)]}
    required.update(_artwork_each_demo_names())

    missing = {
        source: [str(path) for path in paths if not _is_copied(path, copied)]
        for source, paths in required.items()
    }
    missing = {source: paths for source, paths in missing.items() if paths}

    assert not missing, (
        f"the Dockerfile does not copy these into the runtime image: {missing}. Whichever "
        f"demo names them returns an error on the deployed URL while every health check "
        f"passes. Add them to the COPY block in the Dockerfile."
    )


def test_the_application_package_is_copied() -> None:
    assert _is_copied(Path("api"), _copied_paths())


def _dockerignore_rules() -> list[tuple[str, bool]]:
    """(pattern, is_negation) in file order. Order matters — last match wins."""
    rules: list[tuple[str, bool]] = []
    for line in DOCKERIGNORE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("!"):
            rules.append((stripped[1:].rstrip("/"), True))
        else:
            rules.append((stripped.rstrip("/"), False))
    return rules


def excluded_from_build_context(path: str) -> bool:
    """Would Docker exclude this path, given the rules in `.dockerignore`?

    Models Docker's matching (last matching rule wins, `!` re-includes, a directory
    pattern excludes everything beneath it) rather than shelling out to `docker build`,
    so the check runs in CI without a daemon. It is an approximation of Docker's
    behaviour, and the thing it is really guarding is rule *ordering* — the bug where
    someone adds a pattern above the `!.env.example` negation, or drops it, and secrets
    or the sample image quietly change status.
    """
    excluded = False
    for pattern, negated in _dockerignore_rules():
        segments = path.split("/")
        prefixes = ["/".join(segments[: i + 1]) for i in range(len(segments))]
        if any(fnmatch.fnmatch(prefix, pattern) for prefix in prefixes):
            excluded = not negated
    return excluded


@pytest.mark.parametrize(
    ("path", "should_be_excluded"),
    [
        # Secrets. `docker build` reads the working tree, not git, so .gitignore is not
        # the control that matters here — and there is a real .env with a live key in it.
        (".env", True),
        (".env.production", True),
        (".env.local", True),
        ("server.key", True),
        ("cert.pem", True),
        # ...but the template must survive, which is the rule most likely to break when
        # someone tightens the pattern above it.
        (".env.example", False),
        # Test and evaluation material has no business in a production image.
        ("tests/test_api.py", True),
        ("eval/run.py", True),
        # ...with one deliberate exception. `golden/set.json` is PRODUCT SURFACE: the
        # four demos on the landing screen read their applications out of it, so a build
        # without it answers a reviewer's first click with an error while every health
        # check stays green. The photographs and the Tier B manifest beside it are
        # evidence about accuracy and stay out.
        ("golden/set.json", False),
        ("golden/tier_b/manifest.json", True),
        ("golden/tier_b/photos/fireball_back.webp", True),
        (".git/config", True),
        ("web/node_modules/react/index.js", True),
        # ...and everything the service actually runs on must get through.
        ("api/main.py", False),
        ("api/routes/sample.py", False),
        ("assets/samples/old_tom.json", False),
        ("fixtures/labels/tc16_front_back_front.png", False),
        ("web/src/main.tsx", False),
        ("pyproject.toml", False),
    ],
)
def test_the_build_context_excludes_what_it_should(path: str, should_be_excluded: bool) -> None:
    verdict = excluded_from_build_context(path)
    assert verdict == should_be_excluded, (
        f"{path} is {'excluded from' if verdict else 'included in'} the build context; "
        f"expected the opposite. Check the rule ordering in .dockerignore."
    )


def test_every_path_the_dockerfile_copies_survives_the_dockerignore() -> None:
    """The two files have to agree, and nothing makes them.

    A `COPY` of a path that `.dockerignore` excludes fails the build — loudly, which is
    fine. The dangerous direction is a *directory* copy whose contents are partly
    excluded: the build succeeds and the image is missing a file nobody looked for.
    """
    # Only tracked files. Local build artifacts (`__pycache__`, `.venv`) are excluded on
    # purpose and are not what this is about.
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")

    copied = _copied_paths()
    for relative in tracked:
        if not relative or not _is_copied(Path(relative), copied):
            continue
        assert not excluded_from_build_context(relative), (
            f"the Dockerfile copies '{relative}' into the image but .dockerignore "
            f"excludes it, so it will be silently missing at runtime"
        )


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


def test_a_machine_is_never_stopped(fly: dict[str, Any]) -> None:
    """A stopped machine cannot answer a cold click inside any budget.

    Only `auto_stop_machines` is load-bearing. `min_machines_running` is a no-op while
    autostop is off — Fly consults it when deciding what to leave up *while* autostopping
    — so it is not asserted here as though it were doing something.
    """
    assert fly["http_service"]["auto_stop_machines"] == "off", (
        "auto_stop_machines must be off. 'suspend' still charges the grader's first "
        "request for the resume, and the first request is the one being protected."
    )


def test_keepwarm_is_enabled_and_pings_inside_the_cache_ttl(fly: dict[str, Any]) -> None:
    """Cross-checks the deployment's interval against the ceiling keepwarm enforces.

    Above the provider's five-minute ephemeral TTL the pre-warm inverts: every ping pays
    for a cache write that nothing ever reads (LP-324). The clamp lives in the script and
    the value lives here, so the two are compared rather than each asserted alone.
    """
    from scripts.keepwarm import MAX_INTERVAL_S

    env = fly["env"]
    assert env["LABELPROOF_KEEPWARM"] == "1"
    configured = int(env["LABELPROOF_KEEPWARM_INTERVAL_S"])
    assert configured <= MAX_INTERVAL_S, (
        f"fly.toml asks for a {configured}s interval; keepwarm clamps to "
        f"{MAX_INTERVAL_S}s, so the deployed value is a lie about what runs"
    )


def test_the_latency_budget_fits_the_model_production_will_actually_call() -> None:
    """The regression that shipped a production where every real /verify returned 503.

    Cross-checks two sources that cannot see each other: `api/config.py` for the model
    production runs and the deadline it derives for that model, and `fly.toml` for the
    deadline production is actually given. A default sized for one model and silently
    applied to another is invisible in either file alone, which is why it survived.

    (The docstring used to claim a third source, `scripts/smoke.sh`, for the measured
    latency. That was true before smoke.sh was de-duplicated to read the same table out of
    `api/config.py` — it has been two sources ever since, and saying three overstated the
    independence of the check.)
    """
    from api.config import Config, measured_latency_ms

    with FLY_TOML.open("rb") as handle:
        env = tomllib.load(handle)["env"]

    timeout_ms = int(env["LABELPROOF_PROVIDER_TIMEOUT_MS"])
    budget_ms = int(env["LABELPROOF_REQUEST_BUDGET_MS"])

    # The invariant api/config.py enforces at startup — asserted here so a bad pin fails
    # in CI rather than as a ConfigError on a machine that then never boots.
    assert timeout_ms < budget_ms, (
        f"provider timeout {timeout_ms} ms must be below the request budget "
        f"{budget_ms} ms; the app refuses to start otherwise"
    )

    # Whichever model production ends up on, its measured latency has to fit.
    model = Config().extraction_model
    measured = measured_latency_ms(model)
    assert timeout_ms > measured, (
        f"the provider deadline is {timeout_ms} ms and {model} measures {measured} ms. "
        f"Every real verification will hit the deadline and return 503 while /health "
        f"and /ready stay green."
    )

    # And the pins must not fall behind what the app derives for that model.
    #
    # This is the other direction of the same bug. `api/config.py` now sizes both budgets
    # from the model's measured latency, so it is correct by construction — a static pin
    # here is only correct until someone changes the model. Comparing the two turns a
    # stale pin into a CI failure instead of a production timeout, which is the whole
    # reason pinning them is safe at all.
    derived = Config(extraction_model=model)
    assert timeout_ms >= derived.provider_timeout_ms, (
        f"fly.toml pins a {timeout_ms} ms deadline, but api/config.py derives "
        f"{derived.provider_timeout_ms} ms for {model}. The pin is stale — it was "
        f"written for a different model. Update fly.toml, do not lower the derivation."
    )
    assert budget_ms >= derived.request_budget_ms, (
        f"fly.toml pins a {budget_ms} ms budget against a derived "
        f"{derived.request_budget_ms} ms for {model}. Same problem, same fix."
    )


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
    for name in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"):
        assert name in headers, f"{name} is not set at the edge"


def _csp_directives(_fly: dict[str, Any] | None = None) -> dict[str, set[str]]:
    """Parsed from `api/security.py` — the policy that actually reaches a browser."""
    from api.security import CONTENT_SECURITY_POLICY

    raw = CONTENT_SECURITY_POLICY
    directives: dict[str, set[str]] = {}
    for part in raw.split(";"):
        tokens = part.split()
        if tokens:
            directives[tokens[0]] = set(tokens[1:])
    return directives


def _web_sources() -> str:
    files = [ROOT / "web" / "index.html"]
    files += sorted((ROOT / "web" / "src").rglob("*.ts"))
    files += sorted((ROOT / "web" / "src").rglob("*.tsx"))
    files += sorted((ROOT / "web" / "src").rglob("*.css"))
    return "\n".join(f.read_text() for f in files if f.is_file())


def test_the_edge_defines_no_csp_so_the_applications_own_policy_survives(
    fly: dict[str, Any],
) -> None:
    """The bug this test exists for shipped, and nothing caught it.

    `fly.toml` used to set a Content-Security-Policy. Fly's edge does not ADD a response
    header — it REPLACES the application's. So `api/security.CONTENT_SECURITY_POLICY`, and
    every test in `tests/test_security.py` guarding it, described a header no browser ever
    received. Production served the edge's weaker version: `default-src 'self'` rather than
    `'none'`, no `media-src`, no `manifest-src`, no `worker-src`, no
    `upgrade-insecure-requests` — and `style-src 'unsafe-inline'`, which the application
    had DROPPED after testing in a browser that react-dom's style props go through CSSOM
    and do not need it.

    Two files in this repository disagreed about the same policy, the weaker one won by
    virtue of running later, and every assertion pointed at the one that lost.
    """
    headers = fly["http_service"]["http_options"]["response"]["headers"]
    assert "Content-Security-Policy" not in headers, (
        "fly.toml sets a CSP again. The edge REPLACES the application's header rather "
        "than adding to it, so this silently overrides api/security.py and every test "
        "that guards it. Change the policy there, not here."
    )


def test_the_application_still_defines_the_csp() -> None:
    """The other half. Removing it from fly.toml is only safe while the app sends one."""
    from api.security import CONTENT_SECURITY_POLICY

    assert CONTENT_SECURITY_POLICY.strip(), "no CSP anywhere now"
    for directive in ("default-src", "script-src", "style-src", "frame-ancestors"):
        assert directive in CONTENT_SECURITY_POLICY, f"{directive} is missing"


def test_the_served_response_carries_the_applications_csp_verbatim() -> None:
    """Asserted on a real response, because a constant is not a header.

    This is the assertion whose absence let the drift live: everything checked the value
    in `api/security.py` and nothing checked what came back over HTTP.
    """
    from fastapi.testclient import TestClient

    from api.config import Config
    from api.main import create_app
    from api.security import CONTENT_SECURITY_POLICY

    client = TestClient(create_app(config=Config(use_fake_provider=True)))
    served = client.get("/").headers.get("content-security-policy")

    assert served == CONTENT_SECURITY_POLICY, (
        f"the served policy is not the application's.\n  served: {served}\n  "
        f"expected: {CONTENT_SECURITY_POLICY}"
    )


def test_the_spa_needs_no_inline_style_allowance(fly: dict[str, Any]) -> None:
    """The claim that replaced a wrong one.

    An earlier version of this test asserted the opposite — that the SPA's `style={{…}}`
    props REQUIRE `style-src 'unsafe-inline'` or "the page will render unstyled". That was
    checked in a browser and is false: react-dom applies a style prop through
    `node.style.setProperty`, a CSSOM mutation CSP does not govern. CSP governs style
    attributes parsed from markup and `<style>` elements, and the SPA emits neither.

    So the allowance is not needed, and this asserts it stays gone. If someone adds a real
    `<style>` element or a CSS-in-JS runtime, `tests/test_security.py` catches the new
    mechanism and this is where the policy would have to change.
    """
    directives = _csp_directives()
    assert "'unsafe-inline'" not in directives["style-src"], (
        "'unsafe-inline' is back in style-src. The browser evidence says the SPA does "
        "not need it — see test_the_policy_carries_no_unsafe_directive_at_all."
    )
    assert re.search(r"style=\{\{", _web_sources()), (
        "no component sets a style prop any more, so the reasoning above is no longer "
        "being exercised; re-check it before trusting it."
    )


def test_the_csp_is_no_looser_than_the_spa_requires(fly: dict[str, Any]) -> None:
    """The other direction, which is the one that rots quietly.

    `index.html` carries no inline `<script>` and nothing in `web/src` reaches a third-party
    origin — verified here rather than assumed — so script execution and network access
    stay same-origin. If someone adds a CDN, this fails and the CSP has to be widened
    deliberately instead of by accident.
    """
    directives = _csp_directives()
    index = (ROOT / "web" / "index.html").read_text()
    sources = _web_sources()

    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", index), (
        "index.html now has an inline <script>; script-src 'self' will block it"
    )
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'unsafe-eval'" not in directives["script-src"]

    # An XML NAMESPACE is not a network origin. `xmlns='http://www.w3.org/2000/svg'` is
    # an identifier — the browser never dereferences it, and a CSP has nothing to say
    # about it. The inline SVG chevron on the commodity select carries one, and flagging
    # it as external egress would push someone toward deleting a namespace declaration
    # that has to be there for the SVG to render at all.
    sources_without_namespaces = re.sub(r"xmlns(:\w+)?=['\"]?[^'\"\s>]+", "", sources)
    external = {
        match
        for match in re.findall(r"https?://[a-zA-Z0-9.-]+", sources_without_namespaces)
        if "localhost" not in match and "127.0.0.1" not in match
    }
    assert not external, (
        f"web/ now references external origins {sorted(external)}; connect-src 'self' "
        f"will block them. Widen the CSP deliberately or remove the dependency (NET-2 "
        f"says the browser never talks to the provider)."
    )
    assert directives["connect-src"] == {"'self'"}
    assert directives["frame-ancestors"] == {"'none'"}

    # Upload previews and evidence crops are object URLs, so these two are required.
    assert {"data:", "blob:"} <= directives["img-src"]


def test_every_platform_check_hits_a_route_that_exists(fly: dict[str, Any]) -> None:
    """Cross-checks fly.toml against the routes the app actually registers.

    A health check pointed at a path the app does not serve gets a 404 forever: the
    machine never passes, the deploy stalls, and the cause is a typo in a file the
    application code cannot see. Reading the routes off the real app is the only way this
    can fail for the right reason.
    """
    from fastapi.testclient import TestClient

    from api.config import Config
    from api.main import create_app

    app = create_app(config=Config(anthropic_api_key="k"), provider=object())

    with TestClient(app) as client:
        for check in fly["http_service"]["checks"]:
            path = check["path"]
            response = client.request(check.get("method", "GET"), path)

            # Content type, not status code. The SPA catch-all answers 200 with
            # index.html for any unclaimed path, so a typo'd check path — `/healthz` —
            # gets a cheerful 200 from the platform's point of view while measuring
            # nothing at all. A status-code assertion here passes on a broken config,
            # which is the exact class of false-green this file exists to catch.
            assert response.headers.get("content-type", "").startswith("application/json"), (
                f"fly.toml health-checks '{path}', which does not answer with JSON — it "
                f"is being served by the single-page-app fallback. The platform would "
                f"see 200 and report a healthy machine while checking nothing."
            )
            # 200 exactly. Together with the JSON check above this catches a mistyped
            # path in both environments: without a built SPA it is a JSON 404, and with
            # one it is an HTML 200 — neither passes both assertions.
            assert response.status_code == 200, (
                f"'{path}' answered {response.status_code} on a correctly configured "
                f"app; the platform will treat this release as unhealthy and never "
                f"promote it"
            )


def test_both_health_endpoints_are_wired_into_platform_checks(fly: dict[str, Any]) -> None:
    """`/health` is liveness and `/ready` is rotation. Wiring only one of them loses the
    distinction that keeps a provider outage from killing a working container (NET-5)."""
    paths = {check["path"] for check in fly["http_service"]["checks"]}
    assert paths == {"/health", "/ready"}


def test_no_volume_is_mounted(fly: dict[str, Any]) -> None:
    """Uploads and results are ephemeral by policy (SEC-2), and a volume would also add a
    manual step ahead of `fly deploy`, falsifying LP-136's rebuild-from-config claim."""
    assert "mounts" not in fly


def test_the_deployment_names_a_trusted_client_ip_header(fly: dict[str, Any]) -> None:
    """Without this, the rate limiter fails open on the deployed URL (SEC-9).

    Measured on production before it was set, same lane and same second:

        constant  X-Forwarded-For: 203.0.113.7   -> 400 x8, then 429 429 429 429
        rotating  X-Forwarded-For: 198.51.100.N  -> 400 x12, never throttled

    The container runs uvicorn with `--proxy-headers --forwarded-allow-ips='*'`, and
    uvicorn overwrites `scope["client"]` with the leftmost X-Forwarded-For entry. So
    `client_key`'s fallback — documented in its own docstring as unspoofable because it
    reads the socket peer — was reading a client-supplied header. Rotating it bought
    unlimited buckets on every lane including `/verify`.

    Asserted here because no unit test can reach it: the suite drives the ASGI app
    directly and uvicorn is not in the loop. The defect lives entirely between the two.
    """
    env = fly["env"]
    header = env.get("LABELPROOF_CLIENT_IP_HEADER", "")

    assert header, (
        "fly.toml does not set LABELPROOF_CLIENT_IP_HEADER. On Fly, uvicorn's "
        "--proxy-headers makes scope['client'] client-controlled, so the rate limiter "
        "identifies callers by a header they choose. Set it to fly-client-ip."
    )
    assert header.lower() != "x-forwarded-for", (
        "x-forwarded-for is an append-only chain and client_key takes the leftmost "
        "entry, which is whatever the client sent. It is the one header that must not "
        "be trusted here."
    )
    assert header.lower() == "fly-client-ip", (
        f"{header!r} is not a header this platform overwrites. Fly sets fly-client-ip on "
        f"every request; anything else is only safe if some hop provably rewrites it."
    )
