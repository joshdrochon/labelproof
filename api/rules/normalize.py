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
        # Invisible characters are written as escapes on purpose: a literal U+00A0
        # in source looks exactly like a space, so a reviewer cannot see what this
        # line does. The visible glyphs above stay literal for the same reason.
        "\u00a0": " ",  # no-break space
        "\u2007": " ",  # figure space
        "\u202f": " ",  # narrow no-break space
        "\u200b": "",  # zero-width space - deleted, never turned into a space
    }
)

_HYPHEN_LINEBREAK = re.compile(r"(\w)-\s*\n\s*(\w)")
_WHITESPACE = re.compile(r"\s+")
_TERMINAL_PUNCT = re.compile(r"[.,;:!?\s]+$")

#: The same set with the whitespace left out, for `classify_variation`'s whitespace probe
#: and nowhere else. See `_strip_terminal_punctuation_only` below.
_TERMINAL_PUNCT_NO_SPACE = re.compile(r"[.,;:!?]+$")


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


def _strip_terminal_punctuation_only(value: str) -> str:
    """Drop trailing sentence punctuation and leave every space where it was.

    Exists for one caller: `classify_variation`'s whitespace probe. That probe answers
    "was whitespace load-bearing here?" by running every fold *except* the whitespace
    one and seeing whether the strings still differ — which only works if nothing else
    quietly removes whitespace on the way past.

    `strip_terminal_punctuation` does exactly that now, and it has to: taking punctuation
    and trailing spaces in one bite is what makes `normalize` reach its fixed point on
    the first pass. But reusing it inside the probe meant the probe erased the very
    difference it was measuring, so `"0 "` against `"0   "` folded to equal and reported
    *no variation at all* — a Tier-1 Match on two strings that are not the same string,
    with nothing shown to the agent. That is the silent pass MATCH-9 exists to prevent,
    and two property files caught it.
    """
    return _TERMINAL_PUNCT_NO_SPACE.sub("", value)


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


def contains_after_normalization(haystack: str, needle: str) -> bool:
    """Does the label's text carry the application's value inside a longer statement?

    Real labels do not print a bare producer or a bare country. They print "BOTTLED BY
    FOUND NORTH WHISKY, CAMBRIDGE, WI" and "DISTILLED IN CANADA" — the required
    information wrapped in the lead-in phrasing the regulation itself uses. An
    application record holds the bare value, so demanding equality reports a mismatch on
    every compliant import, which is what three real photographs each did.

    Matched on TOKEN BOUNDARIES, not as a raw substring: "Canada" must not be found
    inside "Canadaville", and a bare substring test would do exactly that.

    This is deliberately not offered for every field. A brand name buried inside a longer
    string is not the same claim as a brand name — see `compare_brand_name`, which does
    not use it.
    """
    hay = _bare_tokens(haystack)
    need = _bare_tokens(needle)
    if not need or len(need) > len(hay):
        return False
    return any(
        hay[start : start + len(need)] == need for start in range(len(hay) - len(need) + 1)
    )


#: Punctuation that attaches to a word and is not part of it. Kept deliberately short:
#: an apostrophe is never stripped (STONE'S THROW is one token and the apostrophe is the
#: whole point of TC-02), and neither is a hyphen.
_EDGE_PUNCTUATION = ',.;:!?()[]"“”'


def _bare_tokens(text: str) -> list[str]:
    """Normalized tokens with edge punctuation removed.

    A label prints `TIDEWAY DISTILLING, PORTLAND, ME.` and an application stores the
    producer name and the address in separate columns. Matching the name alone failed,
    because the label's token is `distilling,` and the application's is `distilling` —
    so a compliant label came back Mismatch over a comma the printer put between two
    fields the applicant filed apart.

    This is the same defect as the Courtyard one, mirrored. That was a comma the
    application's JOINED form inserted and the label did not print; this is a comma the
    label prints and the application's individual parts do not. Both were found on real
    artwork, both after the surrounding-text allowance was believed finished, and both
    because a fixture happened to carry punctuation that made the code look right.
    """
    return [
        stripped
        for token in normalize(text).split()
        if (stripped := token.strip(_EDGE_PUNCTUATION))
    ]


def surrounding_words(
    haystack: str, needle: str, *, matched_on: tuple[str, str] | None = None
) -> str:
    """What the label says around the application's value, for the agent to read.

    An acceptable variation still has to show its working: the row says the label agrees
    AND shows the extra words, so a reviewer can see it was "Bottled by" and not
    something that changes the meaning.
    """
    # Locating and quoting are different jobs. `matched_on` carries the pair the
    # caller actually matched with (possibly rewritten by a field normalizer); the
    # words returned always come from the label's own text.
    located_hay, located_need = matched_on or (haystack, needle)
    hay_tokens = collapse_whitespace(haystack).split()
    hay = normalize(located_hay).split()
    need = normalize(located_need).split()
    for start in range(len(hay) - len(need) + 1):
        if hay[start : start + len(need)] == need:
            before = " ".join(hay_tokens[:start])
            after = " ".join(hay_tokens[start + len(need) :])
            return " … ".join(part for part in (before, after) if part)
    return ""


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
                # The whitespace probe must not have its subject removed underneath it.
                # See `_strip_terminal_punctuation_only`.
                value = (
                    _strip_terminal_punctuation_only(value)
                    if keep_space
                    else strip_terminal_punctuation(value)
                )
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
