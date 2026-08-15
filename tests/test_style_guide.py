"""The style guide, enforced (LP-337).

`docs/style-guide.md` names every colour, space, size and voice rule this interface is
allowed to use. This file is what makes that document true tomorrow.

It exists because the stylesheet drifted three separate ways before anything checked it,
and each drift was invisible until someone went looking:

  - `--step: 0.75rem` was declared and referenced NOWHERE, while 23 distinct ad-hoc rem
    values did the spacing. Not one bad number — no system at all.
  - Eleven colours lived as raw hex scattered through the file. `#eef1f7` appeared at
    three call sites as the focused-row ground: a semantic state wearing a literal.
  - The font stack led with two faces the app has never shipped, so every user fell
    through to a system font while the stylesheet implied a design system it was not using.

None of those were caught by review. All three are caught by the tests below in under a
second. A style guide nothing checks is a description of the past.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STYLES = ROOT / "web" / "src" / "styles.css"
GUIDE = ROOT / "docs" / "style-guide.md"
WEB_SRC = ROOT / "web" / "src"

#: `:root` — the only place a literal value may be written.
_ROOT_BLOCK = re.compile(r":root\s*\{(.*?)\n\}", re.S)
_TOKEN = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);", re.M)


def _stylesheet() -> str:
    return STYLES.read_text()


def _root_declarations() -> dict[str, str]:
    match = _ROOT_BLOCK.search(_stylesheet())
    assert match, "no `:root` block found — the token system is the whole style guide"
    return {name: value.strip() for name, value in _TOKEN.findall(match.group(1))}


def _body_after_root() -> str:
    """Everything that is not the token definitions."""
    match = _ROOT_BLOCK.search(_stylesheet())
    assert match
    return _stylesheet()[match.end() :]


# --- colour ---------------------------------------------------------------------------


def test_no_colour_is_written_as_a_literal_outside_root() -> None:
    """A hex in a rule is a colour with no name, and a colour with no name cannot be
    reasoned about, re-themed, or contrast-checked as data.

    `#eef1f7` was the focused-row ground at three separate call sites. Nothing said so.
    """
    body = _body_after_root()
    # The print block flattens every token to black and white on purpose, and the
    # loading skeleton is an animation between two greys — neither is a themeable colour.
    print_at = body.find("@media print")
    screen = body[:print_at] if print_at != -1 else body
    screen = re.sub(r"\.skeleton[^{]*\{[^}]*\}", "", screen)

    literals = sorted(set(re.findall(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", screen)))
    # `#fff` inside an inline SVG data URI is part of an encoded image, not a rule.
    literals = [h for h in literals if f"%23{h.lstrip('#')}" not in screen]

    assert literals == [], (
        f"raw colours outside `:root`: {literals}. Give each one a role name in `:root` "
        f"and reference it with var() — see docs/style-guide.md."
    )


def test_every_colour_token_is_documented_and_every_documented_token_exists() -> None:
    """Both directions. A token with no row is undocumented drift; a row naming a token
    that does not exist is a document describing an app that no longer ships.
    """
    tokens = {name for name in _root_declarations() if not name.startswith("--font")}
    # TABLE ROWS only. The guide's prose names `--step` — the token that was declared and
    # never referenced — as the example of the drift this gate exists to stop. Reading
    # prose as a declaration would make writing down the history impossible.
    rows = [line for line in GUIDE.read_text().splitlines() if line.startswith("|")]
    documented = {
        token
        for line in rows
        for token in re.findall(r"`(--[a-z0-9-]+)`", line)
        if not token.startswith("--font")
    }

    assert tokens - documented == set(), (
        f"tokens missing from docs/style-guide.md: {sorted(tokens - documented)}"
    )
    assert documented - tokens == set(), (
        f"docs/style-guide.md names tokens that do not exist: {sorted(documented - tokens)}"
    )


# --- spacing --------------------------------------------------------------------------

#: The 8px scale, plus 0.25rem for optical nudges inside a component.
#: 2.75rem is the 44px target size. It is on the list because clearing a control that
#: must be 44px is the same measurement, not a new one — the select's chevron gutter.
_ALLOWED_SPACE = {
    "0", "0.25rem", "0.5rem", "0.75rem", "1rem", "1.5rem", "2rem", "2.75rem", "auto",
}
_SPACING_PROP = re.compile(
    r"^\s*(padding|margin|gap|row-gap|column-gap)(?:-(?:top|bottom|left|right))?\s*:\s*([^;]+);",
    re.M,
)


def test_spacing_lands_on_the_scale() -> None:
    """Nothing between the steps. The stylesheet used 23 distinct values — 0.55, 0.85,
    0.9, 1.15, 1.25rem among them, landing on nothing — which is why the page had no
    rhythm to find.
    """
    offenders: list[str] = []
    for prop, value in _SPACING_PROP.findall(_stylesheet()):
        if "var(" in value or "calc(" in value or "%" in value:
            continue
        for part in value.split():
            if part.endswith("rem") and part not in _ALLOWED_SPACE:
                offenders.append(f"{prop}: {value.strip()}")

    assert offenders == [], (
        f"spacing off the 8px scale: {sorted(set(offenders))}. Allowed: "
        f"{sorted(_ALLOWED_SPACE)} — see docs/style-guide.md."
    )


def test_the_spacing_gate_is_actually_reading_declarations() -> None:
    """A regex gate over a hand-written stylesheet fails by matching nothing."""
    found = _SPACING_PROP.findall(_stylesheet())
    assert len(found) >= 100, f"the spacing regex matched {len(found)} declarations"


# --- type -----------------------------------------------------------------------------

_TYPE_SCALE = {"1rem", "1.0625rem", "1.25rem", "1.5rem", "1.875rem", "2.25rem"}


def test_font_sizes_land_on_the_documented_scale() -> None:
    """Twelve sizes, five of them inside a 2.4px band, is twelve decisions buying nothing:
    a 2% step is below the threshold at which a size change is perceivable at all.
    """
    sizes = {
        value.strip()
        for value in re.findall(r"font-size:\s*([^;]+);", _stylesheet())
        if value.strip().endswith("rem")
    }
    off_scale = sorted(sizes - _TYPE_SCALE)

    assert off_scale == [], (
        f"font sizes off the scale: {off_scale}. The scale is {sorted(_TYPE_SCALE)} — "
        f"see docs/style-guide.md. 16px is the floor and has no exceptions."
    )


def test_we_name_no_font_we_do_not_serve() -> None:
    """The stack led with 'Public Sans' and 'Source Sans Pro'. Neither ships with this
    app; neither is installed by default on Windows or macOS. So the app never rendered
    in either, while the stylesheet implied alignment with a federal design system it was
    not using. Nothing in the PRD ever asked for it.

    Either ship the file or drop the name. There is no third option where the claim is
    true and the bytes are absent.
    """
    served = set(re.findall(r"@font-face[^}]*src:[^;]*url\(['\"]?([^'\")]+)", _stylesheet()))
    named: set[str] = set()
    for stack in re.findall(r"--font-[a-z]+:\s*([^;]+);", _stylesheet()):
        for face in stack.split(","):
            named.add(" ".join(face.split()).strip("'\""))

    # Faces that ship with an operating system, plus the generic families.
    installed = {
        "system-ui", "-apple-system", "Segoe UI", "Roboto", "Helvetica Neue", "Helvetica",
        "Arial", "sans-serif", "serif", "Georgia", "Times New Roman", "Iowan Old Style",
        "Charter", "Bitstream Charter", "ui-monospace", "SFMono-Regular", "Menlo",
        "Consolas", "Liberation Mono", "monospace",
    }
    phantom = sorted(name for name in named if name and name not in installed)
    phantom = [name for name in phantom if not any(name in url for url in served)]

    assert phantom == [], (
        f"fonts named but never served and not installed by default: {phantom}. "
        f"Ship an @font-face for them or take them out of the stack."
    )


def test_numbers_are_set_in_a_face_whose_figures_share_a_baseline() -> None:
    """Georgia's figures are OLD-STYLE: 3, 4, 5, 7 and 9 descend below the baseline while
    1 and 2 sit on it. Inside a numbered circle that reads as the 3 being off-centre —
    because it is. Anything showing a number uses the text face and lining figures.
    """
    body = _stylesheet()
    for selector in (".step__number", ".evidence__marker", ".evidence__legend-number", ".row__number"):
        block = re.search(rf"\{re.escape(selector)}\s*\{{([^}}]*)\}}", body)
        if block is None:
            continue
        declarations = block.group(1)
        assert "--font-display" not in declarations, (
            f"{selector} sets the display face on a numeral. Georgia's figures are "
            f"old-style and will not centre — use var(--font-text)."
        )


# --- voice ----------------------------------------------------------------------------

#: Words from the implementation's vocabulary, not the agents'.
_JARGON = (
    "inference",
    "payload",
    "confidence score",
    "adjudicat",
    "tier 1",
    "tier 2",
    "tier 3",
    "LLM",
    "prompt",
)


@pytest.mark.parametrize("word", _JARGON)
def test_ui_copy_uses_the_agents_vocabulary(word: str) -> None:
    """UX-6. The strings a person reads must be in their words, not ours.

    Only rendered text is checked — comments in these files explain the machinery to the
    next developer and are supposed to name it.
    """
    offenders: list[str] = []
    for path in sorted(WEB_SRC.rglob("*.tsx")) + sorted(WEB_SRC.rglob("*.ts")):
        if path.name.endswith((".test.tsx", ".test.ts")):
            continue
        source = path.read_text()
        # Drop comments — a `/* */` block explaining tiers is documentation, not copy.
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        source = re.sub(r"^\s*//.*$", "", source, flags=re.M)

        # Only what a person can READ: quoted strings and JSX text. Grepping the whole
        # file flagged `let payload: unknown` in the fetch wrapper — an identifier, in a
        # module no agent will ever see. A gate that fires on variable names teaches
        # people to rename variables, not to write better copy.
        pieces = [
            part.strip()
            for match in re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"|>([^<>{}]+)<", source)
            for part in match
            if part.strip()
        ]
        # PROSE only: at least two words. A single-token string is an API field name, a
        # CSS class, or an enum value — `adjudicate: nullableNum(timings, 'adjudicate')`
        # is the wire contract, and renaming it to satisfy a copy rule would be the gate
        # driving the code rather than the other way round.
        readable = " ".join(part for part in pieces if " " in part)
        if word.lower() in readable.lower():
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == [], (
        f'"{word}" appears in rendered copy in {offenders}. Say what happened in the '
        f"agent's words — see docs/style-guide.md."
    )


# --- the guide itself -----------------------------------------------------------------


def test_the_style_guide_exists_and_names_its_own_enforcement() -> None:
    """A guide that does not say it is enforced gets read as a suggestion."""
    text = GUIDE.read_text()
    assert "test_style_guide.py" in text
    assert "16px is the floor" in text
