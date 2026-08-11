"""Text normalization and variation classification — Tiers 1 and 2 of the matching policy.

Dave's case is the specification: `STONE'S THROW` on the label versus `Stone's Throw` in
the application is "technically a mismatch, but obviously the same thing." Tier 1 folds
away differences that carry no meaning. Tier 2 identifies *which* class of difference was
folded, so the agent sees the judgment call rather than a silent pass (MATCH-9).

Pure functions, no I/O. Unit-testable in milliseconds.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

# Curly quotes, primes, and the rest of the apostrophe zoo, mapped to ASCII.
# U+2019 RIGHT SINGLE QUOTATION MARK is the STONE'S THROW character specifically.
_QUOTE_MAP = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "′": "'", "ʼ": "'", "´": "'", "`": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "″": '"',
        "‐": "-", "‑": "-", "‒": "-", "–": "-",
        "—": "-", "―": "-", "−": "-",
        " ": " ", " ": " ", " ": " ", "​": "",
    }
)

_HYPHEN_LINEBREAK = re.compile(r"(\w)-\s*\n\s*(\w)")
_WHITESPACE = re.compile(r"\s+")
_TERMINAL_PUNCT = re.compile(r"[.,;:!?\s]+$")


class Variation(StrEnum):
    """A class of difference that normalization can explain."""

    CASE = "case"
    PUNCTUATION = "punctuation"
    WHITESPACE = "whitespace"
    DIACRITICS = "diacritics"
    HYPHENATION = "hyphenation"


#: Human-readable notes, shown verbatim to the agent. Written in an agent's vocabulary,
#: not an engineer's (UX-6).
VARIATION_NOTES: dict[Variation, str] = {
    Variation.CASE: "Label uses different capitalization; same text.",
    Variation.PUNCTUATION: "Punctuation or apostrophe style differs; same text.",
    Variation.WHITESPACE: "Spacing differs; same text.",
    Variation.DIACRITICS: "Accent marks differ; same text.",
    Variation.HYPHENATION: "Text is hyphenated across lines on the label; same text.",
}


def rejoin_hyphenation(value: str) -> str:
    """Rejoin a word split across lines by a hyphen: `DISTIL-\\nLERY` -> `DISTILLERY`."""
    return _HYPHEN_LINEBREAK.sub(r"\1\2", value)


def unify_punctuation(value: str) -> str:
    """Map curly quotes, primes, dashes, and exotic spaces onto their ASCII forms."""
    return value.translate(_QUOTE_MAP)


def fold_diacritics(value: str) -> str:
    """Strip combining marks: `Rhône` -> `Rhone`.

    Recomposes afterwards. NFD decomposition splits Hangul syllables into jamo, which are
    not combining marks and so survive the filter — leaving text that is no longer
    NFKC-stable. Imported labels carry non-Latin scripts, so this is load-bearing rather
    than theoretical. Caught by a property test.
    """
    decomposed = unicodedata.normalize("NFD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return unicodedata.normalize("NFKC", stripped)


def collapse_whitespace(value: str) -> str:
    """Collapse runs of whitespace to a single space and trim the ends."""
    return _WHITESPACE.sub(" ", value).strip()


def strip_terminal_punctuation(value: str) -> str:
    """Drop trailing sentence punctuation, and any whitespace tangled up in it.

    The whitespace matters for idempotency. Stripping punctuation alone leaves `". !"`
    as `". "`, which the following whitespace collapse turns into `"."` — trailing
    punctuation again, so a second pass through `normalize` returns something different
    from the first. Taking punctuation and spaces together in one bite reaches the same
    fixed point on the first pass.
    """
    return _TERMINAL_PUNCT.sub("", value)


def normalize(value: str) -> str:
    """Full Tier-1 normalization. Idempotent: normalize(normalize(x)) == normalize(x)."""
    value = unicodedata.normalize("NFKC", value)
    value = rejoin_hyphenation(value)
    value = unify_punctuation(value)
    value = fold_diacritics(value)
    value = collapse_whitespace(value)
    value = strip_terminal_punctuation(value)
    value = collapse_whitespace(value)
    return value.casefold()


def equal_after_normalization(left: str, right: str) -> bool:
    """Tier 1: exact match once meaningless differences are folded away."""
    return normalize(left) == normalize(right)


def classify_variation(left: str, right: str) -> list[Variation]:
    """Tier 2: which classes of difference account for two strings being equal?

    Returns the variation classes present, in a stable order, or an empty list when the
    strings are byte-identical. Only meaningful for strings that pass Tier 1 — callers
    check `equal_after_normalization` first.

    Each class is probed by applying *only* the other transforms and seeing whether the
    strings still differ. If they do, this class was load-bearing.
    """
    if left == right:
        return []

    found: list[Variation] = []

    def differs_without(*, keep_case: bool = False, keep_punct: bool = False,
                        keep_space: bool = False, keep_marks: bool = False,
                        keep_hyphen: bool = False) -> bool:
        def partial(value: str) -> str:
            value = unicodedata.normalize("NFKC", value)
            if not keep_hyphen:
                value = rejoin_hyphenation(value)
            if not keep_punct:
                value = unify_punctuation(value)
                value = strip_terminal_punctuation(value)
            if not keep_marks:
                value = fold_diacritics(value)
            if not keep_space:
                value = collapse_whitespace(value)
            return value if keep_case else value.casefold()

        return partial(left) != partial(right)

    if differs_without(keep_case=True):
        found.append(Variation.CASE)
    if differs_without(keep_punct=True):
        found.append(Variation.PUNCTUATION)
    if differs_without(keep_space=True):
        found.append(Variation.WHITESPACE)
    if differs_without(keep_marks=True):
        found.append(Variation.DIACRITICS)
    if differs_without(keep_hyphen=True):
        found.append(Variation.HYPHENATION)

    return found


def variation_note(variations: list[Variation]) -> str:
    """Compose the agent-facing note for one or more variation classes."""
    if not variations:
        return "Values are identical."
    if len(variations) == 1:
        return VARIATION_NOTES[variations[0]]
    joined = ", ".join(v.value for v in variations)
    return f"Differences in {joined} only; same text."
