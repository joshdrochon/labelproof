"""Configuration validation. Fail fast, before the grader's first click."""

import pytest

from api.config import (
    MEASURED_EXTRACTION_MS,
    Config,
    ConfigError,
    measured_latency_ms,
)


def test_defaults_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    config = Config.from_env()
    assert config.extraction_model == Config().extraction_model
    assert config.adjudication_model == "claude-haiku-4-5"


def test_missing_key_warns_rather_than_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LABELPROOF_FAKE_PROVIDER", raising=False)
    assert any("ANTHROPIC_API_KEY" in w for w in Config.from_env().warnings)


def test_fake_provider_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "true")
    assert Config.from_env().warnings == []


def test_provider_timeout_must_fit_inside_the_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout at or above the budget guarantees blown deadlines.

    Both are set explicitly: an unset budget now derives from the timeout, so setting
    only the timeout can no longer produce the inversion this guards against.
    """
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    monkeypatch.setenv("LABELPROOF_PROVIDER_TIMEOUT_MS", "6000")
    monkeypatch.setenv("LABELPROOF_REQUEST_BUDGET_MS", "5000")
    with pytest.raises(ConfigError, match="request budget|REQUEST_BUDGET"):
        Config.from_env()


def test_a_timeout_below_the_models_measured_latency_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check that would have caught the 503s (LP-330).

    A 4000ms timeout against claude-opus-5, whose calls take 9.4–10.1s, is not a tighter
    budget — it is a guarantee that every verification returns 503. It shipped in a
    deployable config and no offline test could see it, because the whole suite stubs the
    provider. So the config refuses to start instead.
    """
    monkeypatch.delenv("LABELPROOF_FAKE_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LABELPROOF_EXTRACTION_MODEL", "claude-opus-5")
    monkeypatch.setenv("LABELPROOF_PROVIDER_TIMEOUT_MS", "4000")
    monkeypatch.setenv("LABELPROOF_REQUEST_BUDGET_MS", "5000")

    with pytest.raises(ConfigError, match="would time out and return 503"):
        Config.from_env()


def test_the_budgets_follow_the_model_rather_than_a_fixed_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swapping the model must not leave a budget behind that it cannot meet."""
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    monkeypatch.delenv("LABELPROOF_PROVIDER_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("LABELPROOF_REQUEST_BUDGET_MS", raising=False)

    monkeypatch.setenv("LABELPROOF_EXTRACTION_MODEL", "claude-opus-5")
    slow = Config.from_env()
    monkeypatch.setenv("LABELPROOF_EXTRACTION_MODEL", "claude-haiku-4-5")
    fast = Config.from_env()

    assert slow.provider_timeout_ms > fast.provider_timeout_ms
    for config in (slow, fast):
        assert config.provider_timeout_ms > measured_latency_ms(config.extraction_model)
        assert config.request_budget_ms > config.provider_timeout_ms


def test_an_unknown_model_gets_a_generous_timeout_not_a_guessed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An untimed model is a reason to wait longer, never to fail closed on every call."""
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    monkeypatch.delenv("LABELPROOF_PROVIDER_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("LABELPROOF_REQUEST_BUDGET_MS", raising=False)
    monkeypatch.setenv("LABELPROOF_EXTRACTION_MODEL", "claude-not-yet-released")

    config = Config.from_env()
    assert config.provider_timeout_ms >= max(MEASURED_EXTRACTION_MS.values())


def test_missing_the_latency_target_warns_but_still_boots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slower than PERF-1 is a product problem, not a reason to refuse to serve."""
    monkeypatch.delenv("LABELPROOF_FAKE_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LABELPROOF_EXTRACTION_MODEL", "claude-opus-5")
    monkeypatch.delenv("LABELPROOF_PROVIDER_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("LABELPROOF_REQUEST_BUDGET_MS", raising=False)

    config = Config.from_env()
    assert config.exceeds_latency_target
    assert any("above the 5000ms target" in w for w in config.warnings)


def test_target_resolution_cannot_drop_below_the_high_res_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below 1568 loses the capability the model was chosen for."""
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    monkeypatch.setenv("LABELPROOF_TARGET_LONG_EDGE_PX", "800")
    with pytest.raises(ConfigError, match="high-resolution"):
        Config.from_env()


def test_non_numeric_value_is_rejected_with_the_variable_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    monkeypatch.setenv("LABELPROOF_MAX_IMAGES", "lots")
    with pytest.raises(ConfigError, match="LABELPROOF_MAX_IMAGES"):
        Config.from_env()


def test_env_example_documents_every_variable() -> None:
    """LP-011: .env.example must list every variable the app reads."""
    from pathlib import Path
    import re

    root = Path(__file__).resolve().parents[1]
    documented = set(re.findall(r"^([A-Z_]+)=", (root / ".env.example").read_text(), re.M))
    used = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', (root / "api/config.py").read_text()))
    used |= set(re.findall(r'_int\("([A-Z_]+)"', (root / "api/config.py").read_text()))
    used |= set(re.findall(r'_float\("([A-Z_]+)"', (root / "api/config.py").read_text()))
    assert used - documented == set(), f"undocumented: {sorted(used - documented)}"
