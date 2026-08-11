"""The live-model path — the only place in `eval/` that can spend money.

Kept in its own module for one reason: importing the Anthropic SDK from the gating path
would make `python -m eval.run` require the SDK to be installed and would blur the line
that ENG-3 draws. `tests/test_eval.py` asserts that no `anthropic.*` module is imported by
the CI run, and that assertion is only meaningful while this import stays isolated here.

Nothing calls into this module unless the operator asked for it: `--tier b` or `--model`.
With no credentials configured, callers **skip** rather than fail — an offline machine has
not regressed, it has simply not measured this.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from api.config import Config, ConfigError
from api.provider.base import ExtractionProvider

#: The message every caller prints when it declines to spend. One wording, so a skipped
#: run reads the same whether it came from the sweep or from Tier B.
NO_CREDENTIALS = (
    "SKIPPED — ANTHROPIC_API_KEY is not set, so there is no way to read a real image. "
    "This is not a failure: nothing was measured."
)


def has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


@dataclass
class LiveProvider:
    """A live extractor pinned to one model, built once and reused across labels.

    Built once on purpose: the adapter holds a circuit breaker and an HTTP connection
    pool, and rebuilding it per label would throw away both — the breaker would learn
    nothing across a 25-label run, which is exactly when it matters.
    """

    model: str
    provider: ExtractionProvider

    def __call__(self, _label: object, _images: object) -> ExtractionProvider:
        """Usable directly as a provider factory for either tier."""
        return self.provider


def build(model: str, *, timeout_ms: int | None = None) -> LiveProvider:
    """Construct the real vision adapter for one model.

    Raises `ConfigError` when the environment cannot support it — callers translate that
    into a skip, never into a crash.
    """
    if not has_credentials():
        raise ConfigError(NO_CREDENTIALS)

    # Imported here, not at module scope: see the module docstring.
    from api.provider.anthropic_adapter import AnthropicVisionProvider

    config = replace(Config.from_env(), extraction_model=model)
    if timeout_ms is not None:
        # An eval run is not serving a request, so it is not bound by the 5s budget. Give
        # a slow model room to finish rather than recording a timeout as a wrong answer.
        config = replace(
            config, provider_timeout_ms=timeout_ms, request_budget_ms=timeout_ms + 1000
        )

    return LiveProvider(model=model, provider=AnthropicVisionProvider(config))
