"""Reading what a PERSON TYPED into a form (LP-336).

This is deliberately not `api/rules/abv.py` or `api/rules/fills.py`. Those read what is
PRINTED ON A LABEL, where the job is to find the statement in whatever surrounds it —
`.search()`, first match wins, because a label states its alcohol content once and the
rest is packaging copy.

An application entry is a different problem with the opposite failure mode. A person
typing into a box can type two answers, and taking the first one is a guess. That guess
was live: the browser ran `raw.match(/-?\\d+/)` on the entry, so

    45% BY VOL. (Front label) / 43% BY VOL. (Back label)

was silently filed as 45.0 and checked against the label as though the applicant had
declared it. No message, no flag — the one thing this product is not allowed to do,
sitting in the first box on the first screen.

So these parsers are generous about DECORATION and strict about AMBIGUITY:

    "45"  "45%"  "45% ABV"  "alc. 45% by vol."  "90 proof"   -> 45.0
    "45% (Front) / 43% (Back)"                                -> refused, two values
    "about forty-five"                                        -> refused, no number

Refusing is not a hardship here. The agent is holding the application; they know which
number is on it. What they cannot recover from is the tool quietly picking one.
"""

from __future__ import annotations

import re

from api import canon

#: `45%`, `45.5 %`, `45 percent`
_PERCENT = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:%|percent\b)", re.IGNORECASE)
#: `(90 Proof)`, `90 proof`, `90°`
_PROOF = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:proof\b|°)", re.IGNORECASE)
#: Any number at all, for the plain `45` case.
_NUMBER = re.compile(r"\d{1,3}(?:[.,]\d{1,2})?")

#: `750 mL`, `1,75 L`, `25.4 fl oz`
_QUANTITY = re.compile(
    r"(\d{1,5}(?:[.,]\d{1,3})?)\s*"
    r"(ml|milliliters?|millilitres?|cl|centiliters?|centilitres?|l|liters?|litres?|"
    r"fl\.?\s*oz\.?|fluid\s+ounces?|oz\.?)\b",
    re.IGNORECASE,
)


class EntryError(ValueError):
    """What the person typed cannot be read as one answer.

    A `ValueError` on purpose: raised inside a pydantic `mode="before"` validator it
    becomes a normal validation error carrying this message, so the form, the JSON API
    and a batch manifest row all report the same sentence.
    """


def _number(text: str) -> float:
    """European decimal commas are ordinary on imported labels, so `45,5` is 45.5."""
    return float(text.replace(",", "."))


def read_alcohol_content(raw: str) -> float | None:
    """One ABV out of a typed entry, or an `EntryError` saying why not."""
    text = raw.strip()
    if not text:
        # Genuinely optional. Wine under 14% and malt beverages may omit it entirely
        # (27 CFR 4.36(b)), and an empty box means "the application does not state it" —
        # which is a fact about the filing, not a mistake by the typist.
        return None

    percents = {round(_number(m), 3) for m in _PERCENT.findall(text)}
    proofs = {round(_number(m) / canon.PROOF_PER_ABV_POINT, 3) for m in _PROOF.findall(text)}

    # Precedence, NOT union. `45% Alc./Vol. (90 Proof)` states one value twice, and
    # unioning would call a correct entry ambiguous. A percent that disagrees with its
    # own proof is an internal inconsistency on the LABEL, which `rules/abv.py` reports
    # against the artwork — it is not this function's argument to have.
    values = percents or proofs or {round(_number(m), 3) for m in _NUMBER.findall(text)}

    if not values:
        raise EntryError(
            "alcohol content needs a number, for example 45 — write it as a percent by "
            "volume"
        )
    if len(values) > 1:
        listed = ", ".join(f"{v:g}" for v in sorted(values))
        raise EntryError(
            f"alcohol content has more than one value ({listed}). Enter only the figure "
            f"the application declares — this tool will not choose between them"
        )

    value = values.pop()
    if not 0.0 <= value <= 100.0:
        raise EntryError(
            f"alcohol content of {value:g} is not a percentage by volume; it must be "
            f"between 0 and 100"
        )
    return value


def check_net_contents(raw: str) -> str:
    """Pass the entry through, or refuse it for stating two different sizes.

    Returned unchanged rather than normalised: the comparison against the label is a
    string comparison that already normalises, and rewriting what the agent typed would
    make the row they read differ from the row they filed.

    Only a repeat of the SAME unit is ambiguous. `750 mL (25.4 fl oz)` is one quantity
    declared twice, which is ordinary and correct on a real label, so units are compared
    within themselves rather than converted and pooled.
    """
    text = raw.strip()
    if not text:
        return text

    by_unit: dict[str, set[float]] = {}
    for amount, unit in _QUANTITY.findall(text):
        key = re.sub(r"[.\s]", "", unit).lower().rstrip("s")
        by_unit.setdefault(key, set()).add(round(_number(amount), 3))

    for unit, amounts in by_unit.items():
        if len(amounts) > 1:
            listed = ", ".join(f"{a:g}" for a in sorted(amounts))
            raise EntryError(
                f"net contents has more than one {unit} value ({listed}). Enter only the "
                f"size the application declares"
            )
    return text
