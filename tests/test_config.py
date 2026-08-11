"""Configuration validation. Fail fast, before the grader's first click."""

import pytest

from api.config import Config, ConfigError


def test_defaults_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    config = Config.from_env()
    assert config.extraction_model == "claude-opus-5"
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
    """A timeout at or above the budget guarantees blown deadlines."""
    monkeypatch.setenv("LABELPROOF_FAKE_PROVIDER", "1")
    monkeypatch.setenv("LABELPROOF_PROVIDER_TIMEOUT_MS", "6000")
    with pytest.raises(ConfigError, match="request budget|REQUEST_BUDGET"):
        Config.from_env()


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
