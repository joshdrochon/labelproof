"""Model list prices, for the sweep's cost-per-label column (LP-329).

**Why this table exists separately.** `api/provider/anthropic_adapter.estimated_usd` is
hard-coded to Claude Opus 5's list price — correct for the deployed app, which runs one
model, and wrong for a sweep whose entire purpose is comparing cost *across* models. Using
it would price Haiku at Opus rates and recommend the wrong tier. This wave does not own the
adapter, so the table lives here.

TODO: fold into `api/provider/anthropic_adapter.py` as a per-model function once that file
can be edited, and delete this module. Two price tables is one too many.

Prices are USD per million tokens, Anthropic first-party list rates. Cached reads bill at
a tenth of input, and `verify()` does not surface `cache_read_tokens`, so every figure the
sweep reports is an **upper bound** — a warm cache makes the real number lower, never
higher. Stated rather than quietly assumed away, because the cost analysis quotes it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input_per_mtok: float
    output_per_mtok: float

    def usd(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok
        ) / 1_000_000


#: Verified against the Anthropic model catalog. Sonnet 5 carries an introductory
#: $2/$10 rate through 2026-08-31; the standard $3/$15 is used here so the sweep does not
#: recommend a tier on a price that expires before the pilot does.
PRICES: dict[str, Price] = {
    "claude-opus-5": Price(5.00, 25.00),
    "claude-sonnet-5": Price(3.00, 15.00),
    "claude-haiku-4-5": Price(1.00, 5.00),
}

#: The models the sweep runs when none is named on the command line. Ordered cheapest
#: last so the table reads most-expensive-first, matching how the decision is made:
#: start at the top and come down until something fails.
DEFAULT_SWEEP: tuple[str, ...] = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")

#: Rough token counts for the pre-flight spend estimate only — never for a reported cost.
#: One image at the 2576px high-resolution vision tier is ~4,784 tokens; the static
#: system prompt plus schema is ~1,500; the JSON answer runs ~800.
ESTIMATE_IMAGE_TOKENS = 4_784
ESTIMATE_SYSTEM_TOKENS = 1_500
ESTIMATE_OUTPUT_TOKENS = 800


def price_for(model: str) -> Price | None:
    """The list price, or None when the model is not in the table.

    Returning None rather than guessing: a made-up price in a cost comparison is worse
    than a blank cell, because the blank cell cannot be quoted in a procurement document.
    """
    return PRICES.get(model)


def estimate_usd(model: str, images: int) -> float | None:
    """Pre-flight spend estimate for one label. See the token constants above."""
    price = price_for(model)
    if price is None:
        return None
    return price.usd(
        images * ESTIMATE_IMAGE_TOKENS + ESTIMATE_SYSTEM_TOKENS,
        ESTIMATE_OUTPUT_TOKENS,
    )
