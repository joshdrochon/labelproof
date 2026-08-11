"""What the client's encode quality costs the warning statement (LP-322, WARN-1, IMG-4).

The browser resizes and re-encodes before upload (`web/src/api.ts`), currently WebP at
quality 0.9. That number decides how many bytes cross the wire and, more importantly, how
much of the smallest type on the label survives the trip. It was picked, not measured.

This measures it. For each quality level, every robustness condition is encoded, decoded,
and run through the same chain the server runs, and three things are reported:

* **upload bytes** — what the setting is supposed to buy
* **the warning region's legibility score** — the smallest type on the label, and the one
  field where being wrong is disqualifying (WARN-6)
* **outcome changes** — any condition whose result moved, split into false passes and
  false flags

**What this is not.** It is not warning-field *accuracy*, because that needs a model in
the loop and this harness has none. It is a deterministic legibility proxy measured on the
exact region the warning occupies. A proxy named as a proxy is useful; a proxy reported as
accuracy is a lie with a number attached.

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

import numpy as np
from PIL import Image

from api.models import FieldName
from api.pipeline import preprocess, quality
from fixtures.generator import degrade
from fixtures.generator.layout import FIELD_BANDS
from scripts import robustness_eval

#: The levels the ticket names, plus the shipped one and a lossless control. The control
#: is what every delta is measured against — without it a small drop and a large one are
#: indistinguishable, because there is nothing to be a drop *from*.
QUALITIES: tuple[int, ...] = (100, 95, 90, 85, 75, 60)

#: Formats the browser can produce from an OffscreenCanvas.
FORMATS: tuple[str, ...] = ("WEBP", "JPEG")

#: How much warning-region legibility may drop before a level is called lossy for our
#: purposes. Deliberately tight: this region is the government warning.
MAX_WARNING_SCORE_LOSS = 0.02


def encode_roundtrip(image: np.ndarray, fmt: str, quality_level: int) -> tuple[np.ndarray, int]:
    """Encode and decode as the browser would, returning the pixels and the byte count."""
    buffer = io.BytesIO()
    Image.fromarray(image).save(buffer, fmt, quality=quality_level)
    payload = buffer.getvalue()
    with Image.open(io.BytesIO(payload)) as decoded:
        return np.array(decoded.convert("RGB")), len(payload)


def warning_legibility(image: np.ndarray) -> float:
    """The warning region's blur score — the smallest type on the label."""
    return quality.assess_region(image, FIELD_BANDS[FieldName.GOVERNMENT_WARNING]).blur


@dataclass
class Level:
    fmt: str
    quality: int
    median_bytes: int
    warning_score: float
    warning_loss: float
    false_passes: int
    false_flags: int

    @property
    def lossy_for_the_warning(self) -> bool:
        return self.warning_loss > MAX_WARNING_SCORE_LOSS

    @property
    def safe(self) -> bool:
        return self.false_passes == 0 and not self.lossy_for_the_warning


@dataclass
class Sweep:
    levels: list[Level] = field(default_factory=list)

    def for_format(self, fmt: str) -> list[Level]:
        return [level for level in self.levels if level.fmt == fmt]

    def recommended(self, fmt: str) -> Level | None:
        """The cheapest level that is safe — and then one step back from the edge.

        Not the cheapest safe level itself. The measurement is on rendered fixtures; real
        photographs carry sensor noise, which compresses worse. Sitting on the boundary of
        a synthetic measurement and calling it calibrated is how the warning statement
        ends up unreadable on the one upload that mattered.
        """
        safe = [level for level in self.for_format(fmt) if level.safe]
        if not safe:
            return None
        cheapest = min(safe, key=lambda level: level.quality)
        ladder = sorted({level.quality for level in self.for_format(fmt)})
        index = ladder.index(cheapest.quality)
        step_back = ladder[min(index + 1, len(ladder) - 1)]
        return next(
            level
            for level in self.for_format(fmt)
            if level.quality == step_back and level.safe
        )


def sweep() -> Sweep:
    """Run every condition through every quality level."""
    conditions = degrade.CONDITIONS
    originals = {c.name: robustness_eval._degraded(c.name) for c in conditions}
    baseline = {
        name: warning_legibility(preprocess.preprocess(image).image)
        for name, image in originals.items()
    }

    result = Sweep()
    for fmt in FORMATS:
        for level in QUALITIES:
            sizes: list[int] = []
            losses: list[float] = []
            scores: list[float] = []
            false_passes = 0
            false_flags = 0

            spec = robustness_eval.by_name(degrade.BASE_FIXTURE)
            for condition in conditions:
                decoded, size = encode_roundtrip(originals[condition.name], fmt, level)
                sizes.append(size)

                outcome = robustness_eval.run_condition(spec, decoded, condition)
                false_passes += int(outcome.false_pass)
                false_flags += int(outcome.false_flag)

                score = warning_legibility(preprocess.preprocess(decoded).image)
                scores.append(score)
                losses.append(baseline[condition.name] - score)

            result.levels.append(
                Level(
                    fmt=fmt,
                    quality=level,
                    median_bytes=int(np.median(sizes)),
                    warning_score=round(float(np.mean(scores)), 4),
                    warning_loss=round(float(max(losses)), 4),
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
        "Legibility is a deterministic proxy on the warning region, not model accuracy.",
        "Bytes are a latency cost paid once; a warning compressed past reading is a",
        "compliance failure. There is no exchange rate between the two.",
        "",
    ]
    for fmt in FORMATS:
        lines.append(f"{fmt}")
        lines.append("-" * 84)
        lines.append(
            f"{'quality':>8s} {'median bytes':>13s} {'warning score':>14s} "
            f"{'worst loss':>11s} {'false pass':>11s} {'false flag':>11s}"
        )
        for level in result.for_format(fmt):
            flag = ""
            if level.false_passes:
                flag = "  <- FALSE PASS"
            elif level.lossy_for_the_warning:
                flag = "  <- warning degraded"
            lines.append(
                f"{level.quality:8d} {level.median_bytes:13d} {level.warning_score:14.3f} "
                f"{level.warning_loss:11.3f} {level.false_passes:11d} "
                f"{level.false_flags:11d}{flag}"
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

    lines.append(
        "Measured on rendered fixtures. Real photographs carry sensor noise, which "
        "compresses worse — re-run against Tier B before lowering anything."
    )
    return "\n".join(lines)


def as_dict(result: Sweep) -> dict[str, Any]:
    return {
        "max_warning_score_loss": MAX_WARNING_SCORE_LOSS,
        "recommended": {
            fmt: (result.recommended(fmt).quality if result.recommended(fmt) else None)
            for fmt in FORMATS
        },
        "levels": [
            {
                "format": level.fmt,
                "quality": level.quality,
                "median_bytes": level.median_bytes,
                "warning_score": level.warning_score,
                "warning_loss": level.warning_loss,
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
