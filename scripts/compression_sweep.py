"""What the client's encode quality costs the warning statement (LP-322, WARN-1, IMG-4).

The browser resizes and re-encodes before upload (`web/src/api.ts`), currently WebP at
quality 0.9. That number decides how many bytes cross the wire and, more importantly, how
much of the smallest type on the label survives the trip. It was picked, not measured.

This measures it. For each quality level, every robustness condition is encoded, decoded,
and run through the same chain the server runs, and three things are reported:

* **upload bytes** — what the setting is supposed to buy
* **structural similarity of the warning region** against its own uncompressed pixels —
  the smallest type on the label, and the one field where being wrong is disqualifying
* **outcome changes** — any condition whose result moved, split into false passes and
  false flags

**What this measures, stated precisely, because the last version overstated it.** It is a
*fidelity* proxy: how much of the warning region the encoder destroyed. It is not
character accuracy and it is not warning-field accuracy, both of which need a model in the
loop that this harness does not have.

It is specifically not a *sharpness* measure any more, and that was a real error rather
than a wording problem. Scoring the compressed image alone with a gradient measure reads
encoder ringing as detail: the previous version rated JPEG q75 and q60 *above* the
uncompressed original, which made compression look like an improvement and produced a
"JPEG is gentler than WebP" conclusion that was an artefact of the metric.

**The asymmetry that decides the answer.** Bytes are a latency cost, paid once, and the
budget already has room. A warning statement compressed past reading is a compliance
failure. There is no exchange rate between those, so the recommendation is the *highest*
quality anyone would call reasonable rather than the lowest that still passes.

    python -m scripts.compression_sweep
    python -m scripts.compression_sweep --json
"""

from __future__ import annotations

import argparse
import io
import json
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from PIL import Image

from api.models import FieldName
from api.pipeline import quality
from fixtures.generator import degrade
from fixtures.generator.layout import FIELD_BANDS
from scripts import robustness_eval

#: The levels the ticket names, plus the shipped one and a lossless control. The control
#: is what every delta is measured against — without it a small drop and a large one are
#: indistinguishable, because there is nothing to be a drop *from*.
QUALITIES: tuple[int, ...] = (100, 95, 90, 85, 75, 60)

#: Formats the browser can produce from an OffscreenCanvas.
FORMATS: tuple[str, ...] = ("WEBP", "JPEG")

#: Structural similarity the warning region must retain against its uncompressed self
#: before a level is called safe. Deliberately tight: this region is the government
#: warning, and 0.98 still allows visible ringing around the strokes.
MIN_WARNING_FIDELITY = 0.98

#: Byte budget used for the format comparison. Roughly what a resized label costs at the
#: shipped setting, so the two encoders are asked the same question.
COMPARISON_BUDGET_BYTES = 45_000


def encode_roundtrip(image: np.ndarray, fmt: str, quality_level: int) -> tuple[np.ndarray, int]:
    """Encode and decode as the browser would, returning the pixels and the byte count."""
    buffer = io.BytesIO()
    Image.fromarray(image).save(buffer, fmt, quality=quality_level)
    payload = buffer.getvalue()
    with Image.open(io.BytesIO(payload)) as decoded:
        return np.array(decoded.convert("RGB")), len(payload)


def warning_crop(image: np.ndarray) -> np.ndarray:
    """The government warning's region — the smallest type on the label."""
    return quality.crop(image, FIELD_BANDS[FieldName.GOVERNMENT_WARNING])


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Structural similarity between two crops, 1.0 for identical.

    **Why not the blur score, which is what this used to use.** A sharpness measure is the
    wrong tool for a fidelity question and it fails in the worst direction: JPEG and WebP
    ringing puts *new* high-frequency edges around text, so a gradient measure reads a
    compressed image as sharper than its own source. Measured on this set, the old proxy
    scored JPEG q75 (0.921) and q60 (0.920) above the uncompressed original (0.917), which
    made compression look like an improvement and made JPEG look gentler than WebP.

    SSIM compares against the original instead of scoring the compressed image alone, so
    invented edges count against it rather than for it. It is still a proxy for what
    actually matters — whether a model can read the warning — and it is a proxy for a
    different thing than character accuracy would be. Said plainly here because the last
    version of this file did not say it.
    """
    x = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY).astype(np.float64)
    y = cv2.cvtColor(b, cv2.COLOR_RGB2GRAY).astype(np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2

    mu_x = cv2.GaussianBlur(x, (11, 11), 1.5)
    mu_y = cv2.GaussianBlur(y, (11, 11), 1.5)
    xx = cv2.GaussianBlur(x * x, (11, 11), 1.5) - mu_x * mu_x
    yy = cv2.GaussianBlur(y * y, (11, 11), 1.5) - mu_y * mu_y
    xy = cv2.GaussianBlur(x * y, (11, 11), 1.5) - mu_x * mu_y

    numerator = (2 * mu_x * mu_y + c1) * (2 * xy + c2)
    denominator = (mu_x**2 + mu_y**2 + c1) * (xx + yy + c2)
    return float(np.mean(numerator / denominator))


@dataclass
class Level:
    fmt: str
    quality: int
    median_bytes: int
    warning_fidelity: float
    worst_fidelity: float
    false_passes: int
    false_flags: int

    @property
    def lossy_for_the_warning(self) -> bool:
        """Judged on the *worst* condition, not the average.

        An encoder that is kind to thirteen fixtures and destroys the fourteenth has
        destroyed a government warning, and averaging that away is how the number stops
        meaning anything.
        """
        return self.worst_fidelity < MIN_WARNING_FIDELITY

    @property
    def safe(self) -> bool:
        return self.false_passes == 0 and not self.lossy_for_the_warning


@dataclass
class Sweep:
    levels: list[Level] = field(default_factory=list)

    def for_format(self, fmt: str) -> list[Level]:
        return [level for level in self.levels if level.fmt == fmt]

    def recommended(self, fmt: str) -> Level | None:
        """The cheapest safe level, then one step back from the edge.

        Not the cheapest safe level itself. The measurement is on rendered fixtures; real
        photographs carry sensor noise, which compresses worse. Sitting on the boundary of
        a synthetic measurement and calling it calibrated is how the warning statement
        ends up unreadable on the one upload that mattered.

        Returns None when nothing is safe, rather than raising. The previous version
        assumed the safe levels formed a contiguous run from the top and did a bare
        `next()` over them — with a non-monotone fidelity column, which is exactly what a
        real encoder produces, that raised StopIteration out of `main`.
        """
        levels = self.for_format(fmt)
        safe = [level for level in levels if level.safe]
        if not safe:
            return None

        ladder = sorted({level.quality for level in levels})
        cheapest = min(safe, key=lambda level: level.quality)
        index = ladder.index(cheapest.quality)

        for candidate in ladder[index + 1 :]:
            stepped = next(
                (lv for lv in safe if lv.quality == candidate), None
            )
            if stepped is not None:
                return stepped
        return cheapest

    def at_budget(self, fmt: str, budget: int) -> Level | None:
        """The level closest to a byte budget, for comparing formats at equal cost.

        Comparing WebP q90 against JPEG q90 compares two numbers that share a name and
        nothing else — the quality scales are not the same scale. The only comparison that
        answers "which format should we ship" is at equal bytes.
        """
        levels = self.for_format(fmt)
        return min(levels, key=lambda lv: abs(lv.median_bytes - budget)) if levels else None


def sweep() -> Sweep:
    """Run every condition through every quality level."""
    conditions = degrade.CONDITIONS
    originals = {c.name: robustness_eval._degraded(c.name) for c in conditions}

    result = Sweep()
    for fmt in FORMATS:
        for level in QUALITIES:
            sizes: list[int] = []
            fidelities: list[float] = []
            false_passes = 0
            false_flags = 0

            spec = robustness_eval.by_name(degrade.BASE_FIXTURE)
            for condition in conditions:
                source = originals[condition.name]
                decoded, size = encode_roundtrip(source, fmt, level)
                sizes.append(size)

                outcome = robustness_eval.run_condition(spec, decoded, condition)
                false_passes += int(outcome.false_pass)
                false_flags += int(outcome.false_flag)

                # Compared against this condition's own uncompressed pixels, so the only
                # thing measured is what the encoder destroyed.
                fidelities.append(ssim(warning_crop(source), warning_crop(decoded)))

            result.levels.append(
                Level(
                    fmt=fmt,
                    quality=level,
                    median_bytes=int(np.median(sizes)),
                    warning_fidelity=round(float(np.mean(fidelities)), 4),
                    worst_fidelity=round(float(min(fidelities)), 4),
                    false_passes=false_passes,
                    false_flags=false_flags,
                )
            )
    return result


def render(result: Sweep) -> str:
    lines = [
        "Client encode quality vs the government warning",
        "=" * 84,
        "",
        "Structural similarity of the warning region against its own uncompressed pixels.",
        "A fidelity measure, not a sharpness one, and not character accuracy — an earlier",
        "version of this scored the compressed image alone with a gradient measure, which",
        "reads encoder ringing as sharpness and rated JPEG q60 above the original.",
        "",
        "Bytes are a latency cost paid once; a warning compressed past reading is a",
        "compliance failure. There is no exchange rate between the two.",
        "",
    ]
    for fmt in FORMATS:
        lines.append(f"{fmt}")
        lines.append("-" * 84)
        lines.append(
            f"{'quality':>8s} {'median bytes':>13s} {'mean SSIM':>11s} "
            f"{'worst SSIM':>11s} {'false pass':>11s} {'false flag':>11s}"
        )
        for level in result.for_format(fmt):
            flag = ""
            if level.false_passes:
                flag = "  <- FALSE PASS"
            elif level.lossy_for_the_warning:
                flag = "  <- warning degraded"
            lines.append(
                f"{level.quality:8d} {level.median_bytes:13d} "
                f"{level.warning_fidelity:11.4f} {level.worst_fidelity:11.4f} "
                f"{level.false_passes:11d} {level.false_flags:11d}{flag}"
            )
        recommended = result.recommended(fmt)
        lines.append("")
        if recommended is None:
            lines.append(f"  No {fmt} level leaves the warning region intact.")
        else:
            lines.append(
                f"  Safe with a step of margin: {fmt} quality {recommended.quality} "
                f"({recommended.median_bytes} bytes median)"
            )
        lines.append("")

    lines.append(f"At an equal budget of ~{COMPARISON_BUDGET_BYTES:,} bytes:")
    for fmt in FORMATS:
        level = result.at_budget(fmt, COMPARISON_BUDGET_BYTES)
        if level is not None:
            lines.append(
                f"  {fmt:5s} q{level.quality:<4d} {level.median_bytes:>7,} bytes  "
                f"worst SSIM {level.worst_fidelity:.4f}"
            )
    lines.append(
        "  Quality numbers are not comparable across formats — they are different scales "
        "sharing a name. Equal bytes is the only comparison that answers which to ship."
    )
    lines.append("")
    lines.append(
        "Measured on rendered fixtures, which are sharp text on flat ground and the "
        "easiest possible case for any encoder. Real photographs carry sensor noise, "
        "which compresses worse — re-run against Tier B before lowering anything."
    )
    return "\n".join(lines)


def as_dict(result: Sweep) -> dict[str, Any]:
    return {
        "min_warning_fidelity": MIN_WARNING_FIDELITY,
        "at_equal_bytes": {
            fmt: (
                {
                    "quality": lv.quality,
                    "median_bytes": lv.median_bytes,
                    "worst_fidelity": lv.worst_fidelity,
                }
                if (lv := result.at_budget(fmt, COMPARISON_BUDGET_BYTES))
                else None
            )
            for fmt in FORMATS
        },
        "recommended": {
            fmt: (result.recommended(fmt).quality if result.recommended(fmt) else None)
            for fmt in FORMATS
        },
        "levels": [
            {
                "format": level.fmt,
                "quality": level.quality,
                "median_bytes": level.median_bytes,
                "warning_fidelity": level.warning_fidelity,
                "worst_fidelity": level.worst_fidelity,
                "false_passes": level.false_passes,
                "false_flags": level.false_flags,
                "safe": level.safe,
            }
            for level in result.levels
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    result = sweep()
    print(json.dumps(as_dict(result), indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
