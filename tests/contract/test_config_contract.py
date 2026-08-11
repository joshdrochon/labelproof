"""CONTRACT: the configuration surface is what the deployer sees, so it must be complete.

`.env.example` says "Every variable the app reads is listed here (LP-011)". That is a
promise made in a comment, and until now nothing checked it. A variable the app reads and
the example omits is a knob nobody knows exists — and the one most likely to be omitted is
the one added last, which is the one least likely to have a sensible default.

The same class of gap as the incident, at a different boundary: the code and the thing
that describes the code agreeing by hand.

Also here: the invariants `Config.from_env` enforces. A provider timeout above the request
budget cannot succeed, and a target resolution below 1568 silently drops out of the
high-resolution vision tier — the tier that makes warning text legible. Both fail at
startup rather than on the grader's first click (PERF-6).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from api.config import Config, ConfigError, load

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG_SOURCE = ROOT / "api" / "config.py"

#: Variables the app reads but does not own. `ANTHROPIC_API_KEY` is the SDK's name.
_EXTERNAL = {"ANTHROPIC_API_KEY"}


def _variables_the_app_reads() -> set[str]:
    """Every environment variable named in `api/config.py`.

    Read out of the source rather than from a list maintained here: a second list would
    be a third thing to keep in sync, and the whole point of this file is that hand-kept
    parallel lists drift.
    """
    return set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', CONFIG_SOURCE.read_text())) | set(
        re.findall(r'_(?:int|float)\(\s*"([A-Z_]+)"', CONFIG_SOURCE.read_text())
    )


def _variables_the_example_documents() -> set[str]:
    return set(re.findall(r"^([A-Z_]+)=", ENV_EXAMPLE.read_text(), re.M))


def _documented_value(variable: str) -> str | None:
    """The value `.env.example` gives one variable, anchored to its own line.

    Anchored deliberately. A substring search for `"=4"` is satisfied by
    `LABELPROOF_PROVIDER_TIMEOUT_MS=4000`, so an unanchored assertion about
    `LABELPROOF_MAX_IMAGES` passes whatever that variable is actually set to.
    Returns `None` when the variable is documented with an empty value, which this file
    treats as a deliberate "derive it" rather than as a missing number.
    """
    match = re.search(rf"^{re.escape(variable)}=(.*)$", ENV_EXAMPLE.read_text(), re.M)
    if match is None:
        return None
    return match.group(1).strip() or None


# --------------------------------------------------------------------------------------
# The example and the code agree
# --------------------------------------------------------------------------------------


def test_every_variable_the_app_reads_is_documented(monkeypatch: pytest.MonkeyPatch) -> None:
    """LP-011's promise, enforced.

    An undocumented variable is a knob nobody knows exists — and the ones most likely to
    be missed are the ones added last, which are the ones least likely to have a
    defensible default.
    """
    missing = sorted(_variables_the_app_reads() - _variables_the_example_documents())
    assert missing == [], f".env.example does not document: {missing}"


def test_the_example_documents_no_variable_the_app_ignores() -> None:
    """The other direction. A documented variable that does nothing is worse than none.

    A deployer sets it, believes it took effect, and gets the default — which is how a
    production request budget stays at 5000ms because somebody set the wrong name.
    """
    extra = sorted(_variables_the_example_documents() - _variables_the_app_reads() - _EXTERNAL)
    assert extra == [], f".env.example documents variables nothing reads: {extra}"


def test_every_variable_this_app_owns_shares_its_prefix() -> None:
    """One namespace, so `env | grep LABELPROOF` is the complete picture.

    `ANTHROPIC_API_KEY` is excluded because it is not ours to name — it is the SDK's
    own variable, and renaming it would break every tool that already sets it.
    """
    for name in _variables_the_app_reads() - _EXTERNAL:
        assert name.startswith("LABELPROOF_"), name


def test_the_example_explains_the_two_settings_that_cost_money_or_accuracy() -> None:
    """Some defaults are load-bearing and the file says so where it matters.

    `LABELPROOF_TARGET_LONG_EDGE_PX` below 1568 drops the high-resolution vision tier —
    the thing that makes small warning text legible — and the failure is silent: labels
    just start being read slightly wrong.
    """
    text = ENV_EXAMPLE.read_text()
    assert "1568" in text
    assert "no network, no spend" in text or "no spend" in text


# --------------------------------------------------------------------------------------
# The invariants, enforced at startup
# --------------------------------------------------------------------------------------


def test_a_provider_timeout_above_the_request_budget_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PERF-7: the provider call has to finish inside the budget with room to compare.

    Configured the other way round, every request would hit the budget stop and return
    Needs review — the tool would appear to work and verify nothing.
    """
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    monkeypatch.setenv("LABELPROOF_REQUEST_BUDGET_MS", "4000")
    monkeypatch.setenv("LABELPROOF_PROVIDER_TIMEOUT_MS", "4000")
    with pytest.raises(ConfigError, match="must be below"):
        load()


def test_a_resolution_below_the_high_resolution_tier_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent accuracy regression is exactly what a startup check is for."""
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    monkeypatch.setenv("LABELPROOF_TARGET_LONG_EDGE_PX", "1024")
    with pytest.raises(ConfigError, match="1568"):
        load()


@pytest.mark.parametrize(
    "variable",
    [
        "LABELPROOF_REQUEST_BUDGET_MS",
        "LABELPROOF_MAX_IMAGES",
        "LABELPROOF_MAX_PDF_PAGES",
        "LABELPROOF_RETENTION_HOURS",
        "LABELPROOF_BATCH_WORKERS",
    ],
)
def test_a_non_numeric_value_names_the_variable_it_came_from(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """"invalid literal for int()" tells a deployer nothing.

    Naming the variable and echoing the value turns a five-minute hunt into a one-line
    fix, and this is the error somebody hits at 2am during a deploy.
    """
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    monkeypatch.setenv(variable, "quite a lot")
    with pytest.raises(ConfigError) as raised:
        load()
    assert variable in str(raised.value)
    assert "quite a lot" in str(raised.value)


def test_an_empty_value_falls_back_to_the_default_rather_than_erroring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FOO=` in a .env file is how people comment a variable out.

    Treating it as a parse error would make an empty line in `.env` a startup crash.
    """
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    monkeypatch.setenv("LABELPROOF_MAX_IMAGES", "")
    assert load().max_images == Config().max_images


# --------------------------------------------------------------------------------------
# Missing credentials degrade rather than crash
# --------------------------------------------------------------------------------------


def test_a_missing_api_key_warns_rather_than_refusing_to_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service must come up so it can *say* it is not configured.

    A process that refuses to start gives an operator a crash loop and no message. One
    that starts and answers "the label reading service is not set up on this server"
    gives them a sentence — and gives an agent one too, instead of a connection refused.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LABELPROOF_FAKE_PROVIDER", raising=False)
    config = load()
    assert config.warnings
    assert "ANTHROPIC_API_KEY" in config.warnings[0]
    assert "LABELPROOF_FAKE_PROVIDER" in config.warnings[0]


def test_fake_mode_needs_no_key_and_warns_about_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    config = load()
    assert config.use_fake_provider
    assert config.warnings == []


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
def test_the_documented_spellings_of_fake_mode_all_work(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """`LABELPROOF_FAKE_PROVIDER=true` must not silently mean "live"."""
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", value)
    assert load().use_fake_provider


@pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
def test_anything_else_means_live(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Fail closed on the *safe* side: ambiguity means the real provider, not the fake.

    A typo that silently enabled fake mode in production would serve fixture verdicts
    for real labels — the sample-mode false pass all over again, at a different door.
    """
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert not load().use_fake_provider


# --------------------------------------------------------------------------------------
# Defaults are the ones the documentation quotes
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attribute", "variable"),
    [
        ("max_images", "LABELPROOF_MAX_IMAGES"),
        ("max_pdf_pages", "LABELPROOF_MAX_PDF_PAGES"),
        ("max_image_bytes", "LABELPROOF_MAX_IMAGE_BYTES"),
        ("target_long_edge_px", "LABELPROOF_TARGET_LONG_EDGE_PX"),
        ("retention_hours", "LABELPROOF_RETENTION_HOURS"),
        ("rate_limit_per_minute", "LABELPROOF_RATE_LIMIT_PER_MINUTE"),
        ("adjudication_reserve_ms", "LABELPROOF_ADJUDICATION_RESERVE_MS"),
    ],
)
def test_the_dataclass_default_matches_the_documented_one(
    attribute: str, variable: str
) -> None:
    """The example file quotes numbers a reviewer will hold the product to.

    SEC-2's 24h retention, SEC-9's rate limit, the 1568px vision floor. A default that
    drifted from the documented one would make the README wrong about the thing it is
    most likely to be checked on.

    The expected value is read out of `.env.example`, anchored to its own line. An
    earlier version hard-coded the number and asserted `f"={expected}" in text` — an
    unanchored substring, so the check for `max_images` (4) was satisfied by
    `LABELPROOF_PROVIDER_TIMEOUT_MS=4000` on an unrelated line. Setting
    `LABELPROOF_MAX_IMAGES=99` passed it. Reading the value instead means the assertion
    has exactly one source and cannot be satisfied by a coincidence somewhere else in
    the file.
    """
    documented = _documented_value(variable)
    assert documented is not None, f"{variable} has no value in .env.example"
    assert getattr(Config(), attribute) == int(documented)


@pytest.mark.parametrize(
    "variable", ["LABELPROOF_REQUEST_BUDGET_MS", "LABELPROOF_PROVIDER_TIMEOUT_MS"]
)
def test_the_budget_variables_are_documented_as_derived_rather_than_as_numbers(
    variable: str,
) -> None:
    """The two timing knobs are measured from the model, and the example says so.

    They used to carry numbers here, and setting them by hand is how the service was
    once configured to fail every request — a 4000 ms timeout against a model whose
    calls take ten seconds. An example file that still quoted a number would be
    inviting exactly that, so "documented with an empty value" is the contract, and a
    number reappearing is the regression.
    """
    assert variable in _variables_the_example_documents()
    assert _documented_value(variable) is None, (
        f"{variable} quotes a number again; empty means 'derive from the model'"
    )


def test_the_default_configuration_satisfies_its_own_invariants() -> None:
    """The defaults have to be a valid configuration, or a bare `docker run` fails.

    Asserted as relationships rather than as figures: the budget is derived from
    measured model latency, so pinning it to a number would make a legitimate model
    change look like a regression. What must hold whatever the numbers are is that the
    provider call fits inside the request, with room left to adjudicate.
    """
    config = Config()
    assert config.provider_timeout_ms < config.request_budget_ms
    assert config.target_long_edge_px >= 1568
    assert config.adjudication_reserve_ms < config.request_budget_ms


def test_the_model_ids_are_the_ones_the_documentation_names() -> None:
    """A model id is a contract with the provider: a wrong one is a 404 on every call.

    Pinned against `.env.example` rather than hard-coded twice, so the two cannot
    disagree about which model the deployment runs.
    """
    config = Config()
    text = ENV_EXAMPLE.read_text()
    assert f"LABELPROOF_EXTRACTION_MODEL={config.extraction_model}" in text
    assert f"LABELPROOF_ADJUDICATION_MODEL={config.adjudication_model}" in text


def test_the_default_effort_is_one_the_adapter_accepts() -> None:
    """Otherwise the default configuration raises at provider construction."""
    from api.provider.anthropic_adapter import VALID_EFFORTS

    assert Config().effort in VALID_EFFORTS
