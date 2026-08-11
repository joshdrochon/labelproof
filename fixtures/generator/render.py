"""Render a LabelSpec to a PNG.

Drawn with Pillow rather than SVG so bold, size, and contrast are set directly per text
run — those three are the whole point of the warning fixtures, and going through an SVG
rasterizer would put a font-substitution step between the spec and the pixels.

Deterministic: the same spec always renders byte-identical output, which LP-123 requires.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from fixtures.generator.spec import LabelSpec

#: Font candidates, in preference order. Regular and bold must come from the same family
#: or the bold checks would be testing a family change rather than a weight change.
_FONT_CANDIDATES: list[tuple[str, str]] = [
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/System/Library/Fonts/Helvetica.ttc",
     "/System/Library/Fonts/Helvetica.ttc"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
]


def _font_paths() -> tuple[str, str]:
    for regular, bold in _FONT_CANDIDATES:
        if Path(regular).exists() and Path(bold).exists():
            return regular, bold
    raise RuntimeError(
        "No usable font family found. The generator needs a regular and a bold face "
        "from the same family — bold detection fixtures are meaningless otherwise. "
        f"Looked in: {[c[0] for c in _FONT_CANDIDATES]}"
    )


def _load(size: int, *, bold: bool) -> ImageFont.FreeTypeFont:
    regular, bold_path = _font_paths()
    return ImageFont.truetype(bold_path if bold else regular, size)


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render(spec: LabelSpec) -> Image.Image:
    """Render the spec to an RGB image."""
    img = Image.new("RGB", (spec.width, spec.height), spec.background)
    draw = ImageDraw.Draw(img)

    margin = int(spec.width * 0.08)
    content_width = spec.width - 2 * margin
    y = margin
    ink = (20, 18, 16)

    def centered(text: str, font: ImageFont.FreeTypeFont, colour: tuple[int, int, int]) -> None:
        nonlocal y
        for line in _wrap(draw, text, font, content_width):
            width = draw.textlength(line, font=font)
            draw.text(((spec.width - width) / 2, y), line, font=font, fill=colour)
            y += int(font.size * 1.35)

    show_front = spec.face in ("front", "single")
    show_back = spec.face in ("back", "single")

    if show_front:
        centered(spec.brand_name, _load(int(spec.width * 0.072), bold=True), ink)
        y += margin // 2
        centered(spec.class_type, _load(int(spec.width * 0.034), bold=False), ink)
        y += margin

    if show_front or spec.face == "back":
        y += margin // 3
        body = _load(int(spec.width * 0.028), bold=False)
        if spec.alcohol_text:
            centered(spec.alcohol_text, body, ink)
        centered(spec.net_contents, body, ink)
        if spec.country_of_origin:
            centered(spec.country_of_origin, body, ink)

    if show_back:
        y += margin
        centered(spec.producer, _load(int(spec.width * 0.024), bold=False), ink)

    if spec.include_warning and show_back:
        y += margin
        _draw_warning(draw, spec, margin, content_width, y, ink)

    return img


def _draw_warning(
    draw: ImageDraw.ImageDraw,
    spec: LabelSpec,
    margin: int,
    content_width: int,
    y: int,
    ink: tuple[int, int, int],
) -> None:
    """Draw the warning as two runs so header and body carry independent weight.

    Header and body are separate text runs precisely so `warning_header_bold` and
    `warning_body_bold` can differ — that difference is what TC-04 tests.
    """
    size = max(8, int(spec.width * 0.021 * spec.warning_scale))
    header_font = _load(size, bold=spec.warning_header_bold)
    body_font = _load(size, bold=spec.warning_body_bold)

    # Contrast below 1.0 lifts the ink toward the background (TC-06, buried text).
    colour = tuple(
        int(i + (b - i) * (1.0 - spec.warning_contrast))
        for i, b in zip(ink, spec.background, strict=True)
    )

    full = spec.rendered_warning()
    header = full.split(" ", 2)[0] + " " + full.split(" ", 2)[1]
    body = full[len(header) :].strip()

    # Lay out header and body as one continuous flow, wrapping across the boundary.
    x, line_height = margin, int(size * 1.4)
    header_width = draw.textlength(header + " ", font=header_font)
    draw.text((x, y), header, font=header_font, fill=colour)
    x += int(header_width)

    for word in body.split():
        word_width = draw.textlength(word + " ", font=body_font)
        if x + word_width > margin + content_width:
            x, y = margin, y + line_height
        draw.text((x, y), word, font=body_font, fill=colour)
        x += int(word_width)


def render_to(spec: LabelSpec, directory: Path) -> list[Path]:
    """Render and write. Two-faced specs produce `<name>_front.png` and `<name>_back.png`."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if spec.face == "single":
        path = directory / f"{spec.name}.png"
        render(spec).save(path, "PNG", optimize=True)
        written.append(path)
    else:
        for face in ("front", "back"):
            path = directory / f"{spec.name}_{face}.png"
            render(spec.with_(face=face)).save(path, "PNG", optimize=True)
            written.append(path)

    return written
