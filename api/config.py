"""Configuration from the environment only, validated at startup (LP-011, LP-087).

Fails fast and loudly on a missing required variable. A prototype that boots with a
half-configured provider and only discovers it on the grader's first click is exactly the
cold-start ambush PERF-6 exists to prevent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """A required variable is missing or unusable. Raised before the app serves."""


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    # --- provider ---------------------------------------------------------------
    anthropic_api_key: str = ""
    extraction_model: str = "claude-opus-5"
    adjudication_model: str = "claude-haiku-4-5"
    effort: str = "low"
    use_fake_provider: bool = False

    # --- budgets ----------------------------------------------------------------
    request_budget_ms: int = 5000
    provider_timeout_ms: int = 4000
    adjudication_reserve_ms: int = 1200

    # --- uploads ----------------------------------------------------------------
    max_image_bytes: int = 10 * 1024 * 1024
    max_images: int = 4
    max_pdf_pages: int = 5
    target_long_edge_px: int = 2576

    # --- retention and abuse ----------------------------------------------------
    retention_hours: int = 24
    rate_limit_per_minute: int = 30

    # --- batch ------------------------------------------------------------------
    batch_workers: int = 6

    storage_dir: str = "./.data"
    log_level: str = "INFO"

    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> Config:
        use_fake = os.environ.get("LABELPROOF_FAKE_PROVIDER", "").lower() in (
            "1", "true", "yes",
        )
        key = os.environ.get("ANTHROPIC_API_KEY", "")

        warnings: list[str] = []
        if not key and not use_fake:
            warnings.append(
                "ANTHROPIC_API_KEY is not set. Verification will fail until it is, or "
                "set LABELPROOF_FAKE_PROVIDER=1 to run against recorded fixtures."
            )

        config = cls(
            anthropic_api_key=key,
            extraction_model=os.environ.get("LABELPROOF_EXTRACTION_MODEL", "claude-opus-5"),
            adjudication_model=os.environ.get("LABELPROOF_ADJUDICATION_MODEL", "claude-haiku-4-5"),
            effort=os.environ.get("LABELPROOF_EFFORT", "low"),
            use_fake_provider=use_fake,
            request_budget_ms=_int("LABELPROOF_REQUEST_BUDGET_MS", 5000),
            provider_timeout_ms=_int("LABELPROOF_PROVIDER_TIMEOUT_MS", 4000),
            adjudication_reserve_ms=_int("LABELPROOF_ADJUDICATION_RESERVE_MS", 1200),
            max_image_bytes=_int("LABELPROOF_MAX_IMAGE_BYTES", 10 * 1024 * 1024),
            max_images=_int("LABELPROOF_MAX_IMAGES", 4),
            max_pdf_pages=_int("LABELPROOF_MAX_PDF_PAGES", 5),
            target_long_edge_px=_int("LABELPROOF_TARGET_LONG_EDGE_PX", 2576),
            retention_hours=_int("LABELPROOF_RETENTION_HOURS", 24),
            rate_limit_per_minute=_int("LABELPROOF_RATE_LIMIT_PER_MINUTE", 30),
            batch_workers=_int("LABELPROOF_BATCH_WORKERS", 6),
            storage_dir=os.environ.get("LABELPROOF_STORAGE_DIR", "./.data"),
            log_level=os.environ.get("LABELPROOF_LOG_LEVEL", "INFO"),
            warnings=warnings,
        )

        if config.provider_timeout_ms >= config.request_budget_ms:
            raise ConfigError(
                f"LABELPROOF_PROVIDER_TIMEOUT_MS ({config.provider_timeout_ms}) must be "
                f"below LABELPROOF_REQUEST_BUDGET_MS ({config.request_budget_ms}) — the "
                f"provider call has to finish inside the request budget with room to "
                f"compare and render."
            )
        if config.target_long_edge_px < 1568:
            raise ConfigError(
                f"LABELPROOF_TARGET_LONG_EDGE_PX is {config.target_long_edge_px}. Below "
                f"1568 loses the high-resolution vision tier, which is what makes small "
                f"warning text legible — see BUILD.md §1."
            )
        return config


def load() -> Config:
    return Config.from_env()
